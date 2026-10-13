"""FastAPI application for the first usable local banking slice."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .agent import BankingAgent
from .bank.service import BankError, BankService
from .billing import BillingError, BillingService, sample_csv
from .cards import CardService
from .config import MODEL, ROOT, database_url
from .conversations import ConversationError, ConversationStore
from .knowledge.service import KnowledgeService
from .llm import DeepSeekClient, ModelError
from .subscriptions import SubscriptionService
from .task_clock import TaskClock
from .task_routes import task_router
from .tasks import TasksService
from .wealth import WealthService
from .workflows import WorkflowService

logger = logging.getLogger("concierge")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None
    scope: str | None = Field(default=None, min_length=1, max_length=80)
    client_request_id: str | None = Field(default=None, min_length=1, max_length=160)
    retry_failed: bool = False


class DecisionRequest(BaseModel):
    decision: Literal["approve", "decline"]
    conversation_id: str | None = None


class ThreadRequest(BaseModel):
    title: str = Field(default="临时事项", max_length=80)
    scope: str | None = Field(default=None, min_length=1, max_length=80)
    client_request_id: str | None = Field(default=None, min_length=1, max_length=160)


class ImportRequest(BaseModel):
    filename: str = Field(default="账单.csv", max_length=200)
    content: str = Field(max_length=2 * 1024 * 1024)


class SubscriptionRequest(BaseModel):
    action: Literal["cancel_subscription", "stop_autopay"]
    conversation_id: str


class CardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["freeze", "unfreeze", "report_lost", "set_daily_limit", "set_credit_limit", "apply_card"]
    conversation_id: str
    card_id: str | None = None
    amount_minor: int | None = Field(default=None, strict=True)
    card_kind: Literal["debit", "credit"] | None = None


class WealthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["purchase", "redeem"]
    conversation_id: str
    product_id: str | None = None
    account_id: str | None = None
    position_id: str | None = None
    amount_minor: int | None = Field(default=None, strict=True)


class RiskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: dict


class ClockRequest(BaseModel):
    business_days: int = Field(ge=1, le=365, strict=True)


def create_app(
    db_url: str | None = None,
    model=None,
    knowledge_dir: Path | None = None,
    *,
    enable_p3_actions: bool = False,
    enable_tasks_actions: bool = False,
    task_poll_seconds: float = 2.0,
    workflow_fault_mode: str | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        url = db_url or database_url()
        bank = BankService(url)
        bank.seed()
        knowledge = KnowledgeService(knowledge_dir or ROOT / "knowledge")
        store = ConversationStore(url)
        billing = BillingService(url)
        billing.seed_demo()
        subscriptions = SubscriptionService(url)
        subscriptions.seed_demo()
        cards = CardService(url)
        cards.seed_demo()
        wealth = WealthService(url)
        wealth.seed_demo()
        if enable_tasks_actions:
            task_clock = TaskClock(url)
            tasks = TasksService(url)
            tasks.seed_demo()
            workflows = WorkflowService(url, fault_mode=workflow_fault_mode)
            workflows.seed_demo()
        else:
            task_clock = tasks = workflows = None
        client = model or DeepSeekClient()
        app.state.bank, app.state.knowledge, app.state.store, app.state.model = bank, knowledge, store, client
        app.state.billing, app.state.subscriptions = billing, subscriptions
        app.state.cards, app.state.wealth = cards, wealth
        app.state.p3_actions_enabled = enable_p3_actions
        app.state.tasks, app.state.workflows, app.state.task_clock = tasks, workflows, task_clock
        app.state.tasks_actions_enabled = enable_tasks_actions
        app.state.sync_events = sync_events
        app.state.task_tick = task_tick
        app.state.task_tick_lock = RLock()
        if enable_tasks_actions:
            task_tick()
        sync_events()
        app.state.agent = BankingAgent(
            bank,
            knowledge,
            store,
            client,
            billing,
            subscriptions,
            cards,
            wealth,
            enable_p3_actions=enable_p3_actions,
        )
        stop = asyncio.Event()
        async def background():
            while not stop.is_set():
                try:
                    await asyncio.to_thread(task_tick)
                except Exception:
                    logger.exception("Background task processing failed; durable tasks remain recoverable")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=max(0.02, task_poll_seconds))
                except TimeoutError:
                    pass
        worker = asyncio.create_task(background()) if enable_tasks_actions else None
        try:
            yield
        finally:
            if worker:
                stop.set()
                await worker
        await client.close()
        store.close()
        bank.dispose()
        billing.close()
        subscriptions.close()
        cards.close()
        wealth.close()
        if tasks:
            tasks.close()
            workflows.close()
            task_clock.close()

    app = FastAPI(title="银小二 · Banking Concierge", version="0.3.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Content-Type"],
    )

    @app.exception_handler(BankError)
    async def bank_error(_, error):
        return JSONResponse(
            status_code=409, content={"detail": {"code": error.code, "message": error.message}}
        )

    @app.exception_handler(BillingError)
    async def billing_error(_, error):
        return JSONResponse(
            status_code=422, content={"detail": {"code": error.code, "message": error.message}}
        )

    @app.exception_handler(ConversationError)
    async def conversation_error(_, error):
        if error.code == "EMPTY_TITLE" or error.code == "INVALID_SCOPE":
            status_code = 422
        elif error.code in {"CONVERSATION_NOT_FOUND"}:
            status_code = 404
        elif error.code in {"PROVIDER_TIMEOUT", "PROVIDER_UNAVAILABLE", "PROVIDER_ERROR"}:
            status_code = 503
        else:
            status_code = 409
        return JSONResponse(
            status_code=status_code,
            content={"detail": {"code": error.code, "message": error.message}},
        )

    def sync_events():
        store, subscriptions = app.state.store, app.state.subscriptions
        app.state.wealth.process_due()
        subscriptions.renewal_notifications(app.state.billing.entries())
        events = app.state.bank.completed_events()
        events += subscriptions.completed_events()
        events += subscriptions.persisted_notifications()
        events += app.state.cards.completed_events()
        events += app.state.wealth.completed_events()
        if app.state.tasks_actions_enabled:
            events += app.state.tasks.completed_events()
            events += app.state.workflows.completed_events()
        for event in events:
            store.publish(event)
            if event.get("plan_id") and event.get("receipt"):
                store.note_decision(event["plan_id"], event["text"], event["receipt"])
                for related in event.get("related_plan_ids", []):
                    if related != event["plan_id"]:
                        store.note_decision(related, event["text"], event["receipt"])

    def task_tick():
        if not app.state.tasks_actions_enabled:
            return
        with app.state.task_tick_lock:
            app.state.tasks.process_due()
            app.state.workflows.process_due()
            sync_events()

    def check_scope(plan, identity):
        if identity and plan and plan.get("conversation_id") != identity:
            raise BankError("CONVERSATION_MISMATCH", "这张确认单属于另一条会话，请回到原事项办理。")

    def services():
        result = {
            "transfer": app.state.bank,
            "subscription": app.state.subscriptions,
            "card": app.state.cards,
            "wealth": app.state.wealth,
        }
        if app.state.tasks_actions_enabled:
            result.update({"task": app.state.tasks, "workflow": app.state.workflows})
        return result

    def invalidate_other_plans(identity, keep):
        for service in services().values():
            if service is not keep:
                service.invalidate_pending_plans(identity)

    def visible_action(action):
        if not action:
            return None
        if action.get("kind") in {"card", "wealth"} and not app.state.p3_actions_enabled:
            return None
        if action.get("kind") in {"task", "workflow"} and not app.state.tasks_actions_enabled:
            return None
        return action

    @app.get("/api/conversations")
    def conversations(scope: str | None = None):
        sync_events()
        return {"conversations": app.state.store.list(scope)}

    @app.post("/api/conversations")
    def create_conversation(request: ThreadRequest):
        return app.state.store.create(
            request.title,
            scope=request.scope,
            client_request_id=request.client_request_id,
        )

    @app.patch("/api/conversations/{identity}")
    def rename_conversation(identity: str, request: ThreadRequest):
        return app.state.store.rename(identity, request.title)

    @app.get("/api/conversations/{identity}")
    def conversation(identity: str):
        sync_events()
        result = app.state.store.detail(identity)
        for message in result["messages"]:
            action = message.get("action") or message.get("plan")
            if not action:
                continue
            kind = action.get("kind", "transfer")
            service = services().get(kind)
            current = service.get_plan(action["id"]) if service else None
            if current:
                message["action"] = (
                    {**current, "kind": "transfer", "title": "转账确认"} if kind == "transfer" else current
                )
                if kind == "transfer":
                    message["plan"] = current
            message["action"] = visible_action(message.get("action"))
        pending = []
        for kind, service in services().items():
            candidate = service.pending_for_conversation(identity)
            if candidate and kind == "transfer":
                candidate = {**candidate, "kind": "transfer", "title": "转账确认"}
            candidate = visible_action(candidate)
            if candidate:
                pending.append(candidate)
        result["pending_action"] = max(pending, key=lambda item: item["expires_at"]) if pending else None
        return result

    @app.post("/api/conversations/{identity}/read")
    def read_conversation(identity: str):
        app.state.store.mark_read(identity)
        return {"status": "ok"}

    @app.get("/api/notifications")
    def notifications():
        sync_events()
        return {"notifications": app.state.store.notifications()}

    @app.get("/api/billing/report")
    def billing_report(start: str | None = None, end: str | None = None):
        return app.state.billing.report(start, end)

    @app.get("/api/billing/entries")
    def billing_entries(start: str | None = None, end: str | None = None):
        return {"entries": app.state.billing.entries(start, end)}

    @app.post("/api/billing/import")
    def billing_import(request: ImportRequest):
        if len(request.content.encode("utf-8")) > 2 * 1024 * 1024:
            raise HTTPException(413, detail={"code": "FILE_TOO_LARGE", "message": "账单请控制在 2MB 以内。"})
        return app.state.billing.import_csv(request.content, request.filename)

    @app.get("/api/billing/sample")
    def billing_sample():
        return Response(
            sample_csv(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="billing-sample.csv"'},
        )

    @app.get("/api/subscriptions")
    def subscriptions_list():
        return app.state.subscriptions.list_subscriptions(app.state.billing.entries())

    @app.post("/api/subscriptions/{identity}/preview")
    async def subscription_preview(identity: str, request: SubscriptionRequest):
        async with app.state.agent.conversation_lock(request.conversation_id):
            return await asyncio.to_thread(make_subscription_preview, identity, request)

    def make_subscription_preview(identity: str, request: SubscriptionRequest):
        store = app.state.store
        conversation_id, history = store.load(request.conversation_id)
        # Validate first; an unsupported request must not replace an existing valid preview.
        action = app.state.subscriptions.create_action_plan(identity, request.action, conversation_id)
        invalidate_other_plans(conversation_id, app.state.subscriptions)
        prompt = (
            f"请{'取消订阅' if request.action == 'cancel_subscription' else '停止代扣'}：{action['merchant']}"
        )
        store.append_user(conversation_id, prompt)
        history.extend(
            [{"role": "user", "content": prompt}, {"role": "assistant", "content": action["description"]}]
        )
        store.save(conversation_id, history)
        store.append_assistant(conversation_id, {"reply": action["description"], "action": action})
        return action

    async def preview_domain(identity, service, create):
        async with app.state.agent.conversation_lock(identity):

            def persist():
                conversation_id, history = app.state.store.load(identity)
                action = create()
                invalidate_other_plans(conversation_id, service)
                prompt = f"请预览：{action['title']}"
                app.state.store.append_user(conversation_id, prompt)
                history.extend(
                    [
                        {"role": "user", "content": prompt},
                        {"role": "assistant", "content": action["description"]},
                    ]
                )
                app.state.store.save(conversation_id, history)
                app.state.store.append_assistant(
                    conversation_id, {"reply": action["description"], "action": action}
                )
                return action

            return await asyncio.to_thread(persist)

    def decide_domain(identity, service, request):
        plan = service.get_plan(identity)
        check_scope(plan, request.conversation_id)
        result = service.decide_plan(identity, request.decision)
        if plan and plan.get("conversation_id"):
            result["conversation_id"] = plan["conversation_id"]
        app.state.store.note_decision(identity, result["message"], result)
        sync_events()
        return result

    if enable_tasks_actions:
        app.include_router(task_router(app, preview_domain, decide_domain))

    @app.get("/api/cards")
    def cards_list():
        return {"cards": app.state.cards.list_cards(), "applications": app.state.cards.list_applications()}

    @app.post("/api/cards/preview")
    async def cards_preview(request: CardRequest):
        return await preview_domain(
            request.conversation_id,
            app.state.cards,
            lambda: app.state.cards.create_action_plan(**request.model_dump()),
        )

    @app.post("/api/card-plans/{identity}/decision")
    def cards_decision(identity: str, request: DecisionRequest):
        return decide_domain(identity, app.state.cards, request)

    @app.get("/api/wealth")
    def wealth_overview():
        sync_events()
        return {
            "products": app.state.wealth.list_products(),
            "risk_profile": app.state.wealth.get_risk_profile(),
            "positions": app.state.wealth.list_positions(),
            "orders": app.state.wealth.list_orders(),
            "as_of": app.state.wealth.current_date(),
        }

    @app.get("/api/wealth/orders/{identity}")
    def wealth_order(identity: str):
        sync_events()
        result = app.state.wealth.get_order(identity)
        if result is None:
            raise HTTPException(404, detail={"code": "ORDER_NOT_FOUND", "message": "没有找到这笔理财订单。"})
        return result

    @app.post("/api/wealth/risk-profile")
    def risk_profile(request: RiskRequest):
        return app.state.wealth.set_risk_profile(request.answers)

    @app.post("/api/wealth/preview")
    async def wealth_preview(request: WealthRequest):
        return await preview_domain(
            request.conversation_id,
            app.state.wealth,
            lambda: app.state.wealth.create_action_plan(**request.model_dump()),
        )

    @app.post("/api/wealth-plans/{identity}/decision")
    def wealth_decision(identity: str, request: DecisionRequest):
        return decide_domain(identity, app.state.wealth, request)

    @app.post("/api/wealth/demo/advance")
    def advance_clock(request: ClockRequest):
        result = app.state.wealth.advance_demo_clock(request.business_days)
        sync_events()
        return result

    @app.post("/api/subscription-plans/{identity}/decision")
    def subscription_decision(identity: str, request: DecisionRequest):
        check_scope(app.state.subscriptions.get_plan(identity), request.conversation_id)
        result = app.state.subscriptions.decide_plan(identity, request.decision)
        app.state.store.note_decision(identity, result["message"], result)
        sync_events()
        return result

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "model": MODEL,
            "model_configured": app.state.model.configured,
            "storage": app.state.bank.dialect_name,
            "knowledge_documents": app.state.knowledge.document_count,
            "p3_actions_enabled": app.state.p3_actions_enabled,
            "tasks_actions_enabled": app.state.tasks_actions_enabled,
        }

    @app.get("/api/overview")
    def overview():
        return app.state.bank.overview()

    def validate_request(request: ChatRequest) -> str:
        message = request.message.strip()
        if not message:
            raise HTTPException(422, detail={"code": "EMPTY_MESSAGE", "message": "请先输入你的需求。"})
        app.state.store.validate_scope(request.scope)
        if request.conversation_id:
            try:
                UUID(request.conversation_id)
            except ValueError as error:
                raise HTTPException(
                    422, detail={"code": "INVALID_CONVERSATION", "message": "对话标识不正确，请新建对话。"}
                ) from error
            app.state.store.load(request.conversation_id)
        return message

    async def run_chat(request: ChatRequest, message: str, emit=None) -> dict:
        try:
            return await app.state.agent.chat(
                message,
                request.conversation_id,
                emit=emit,
                scope=request.scope,
                client_request_id=request.client_request_id,
                retry_failed=request.retry_failed,
            )
        except ModelError as error:
            app.state.store.fail_chat_request(request.client_request_id, error.code, error.message)
            raise
        except ConversationError:
            raise
        except asyncio.CancelledError:
            app.state.store.fail_chat_request(
                request.client_request_id, "REQUEST_INTERRUPTED", "这次回复中断了；对话和已发送消息已保存。"
            )
            raise
        except Exception:
            app.state.store.fail_chat_request(
                request.client_request_id, "INTERNAL_ERROR", "请求暂时未完成；对话和已发送消息已保存。"
            )
            raise

    @app.post("/api/chat")
    async def chat(request: ChatRequest):
        message = validate_request(request)
        try:
            return await run_chat(request, message)
        except ModelError as error:
            raise HTTPException(503, detail={"code": error.code, "message": error.message}) from error

    @app.post("/api/chat/stream")
    async def chat_stream(request: ChatRequest):
        message = validate_request(request)
        queue: asyncio.Queue = asyncio.Queue()

        async def emit(event, data):
            await queue.put((event, data))

        async def run():
            try:
                result = await run_chat(request, message, emit=emit)
                await emit("result", result)
            except ModelError as error:
                await emit("error", {"code": error.code, "message": error.message})
            except ConversationError as error:
                await emit("error", {"code": error.code, "message": error.message})
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Chat stream failed")
                await emit("error", {"code": "INTERNAL_ERROR", "message": "请求暂时未完成，请重试。"})
            finally:
                await emit("done", {})

        async def stream():
            task = asyncio.create_task(run())
            try:
                while True:
                    event, data = await queue.get()
                    yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    if event == "done":
                        break
            finally:
                if not task.done():
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/plans/{plan_id}/decision")
    def decide(plan_id: str, request: DecisionRequest):
        try:
            plan = app.state.bank.get_plan(plan_id)
            check_scope(plan, request.conversation_id)
            result = app.state.bank.decide_plan(plan_id, request.decision)
            if plan and plan.get("conversation_id"):
                result["conversation_id"] = plan["conversation_id"]
            app.state.store.note_decision(plan_id, result["message"], result)
            sync_events()
            return result
        except BankError as error:
            raise HTTPException(409, detail={"code": error.code, "message": error.message}) from error

    built = ROOT / "web" / "dist"
    if (built / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=built / "assets"), name="assets")
        if (built / "fonts").exists():
            app.mount("/fonts", StaticFiles(directory=built / "fonts"), name="fonts")

        @app.get("/")
        def index():
            return FileResponse(built / "index.html")

    return app


app = create_app(enable_p3_actions=True)
