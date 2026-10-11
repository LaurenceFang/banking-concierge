"""FastAPI application for the first usable local banking slice."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import BankingAgent
from .bank.service import BankError, BankService
from .billing import BillingError, BillingService, sample_csv
from .config import MODEL, ROOT, database_url
from .conversations import ConversationError, ConversationStore
from .knowledge.service import KnowledgeService
from .llm import DeepSeekClient, ModelError
from .subscriptions import SubscriptionService

logger = logging.getLogger("concierge")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None


class DecisionRequest(BaseModel):
    decision: Literal["approve", "decline"]
    conversation_id: str | None = None


class ThreadRequest(BaseModel):
    title: str = Field(default="临时事项", max_length=80)


class ImportRequest(BaseModel):
    filename: str = Field(default="账单.csv", max_length=200)
    content: str = Field(max_length=2 * 1024 * 1024)


class SubscriptionRequest(BaseModel):
    action: Literal["cancel_subscription", "stop_autopay"]
    conversation_id: str


def create_app(db_url: str | None = None, model=None, knowledge_dir: Path | None = None) -> FastAPI:
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
        client = model or DeepSeekClient()
        app.state.bank, app.state.knowledge, app.state.store, app.state.model = bank, knowledge, store, client
        app.state.billing, app.state.subscriptions = billing, subscriptions
        sync_events()
        app.state.agent = BankingAgent(bank, knowledge, store, client, billing, subscriptions)
        yield
        await client.close()
        store.close()
        bank.dispose()
        billing.close()
        subscriptions.close()

    app = FastAPI(title="银小二 · Banking Concierge", version="0.2.0", lifespan=lifespan)
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
        return JSONResponse(
            status_code=422 if error.code == "EMPTY_TITLE" else 404,
            content={"detail": {"code": error.code, "message": error.message}},
        )

    def sync_events():
        store, subscriptions = app.state.store, app.state.subscriptions
        subscriptions.renewal_notifications(app.state.billing.entries())
        events = app.state.bank.completed_events()
        events += subscriptions.completed_events()
        events += subscriptions.persisted_notifications()
        for event in events:
            store.publish(event)
            if event.get("plan_id") and event.get("receipt"):
                store.note_decision(event["plan_id"], event["text"], event["receipt"])

    def check_scope(plan, identity):
        if identity and plan and plan.get("conversation_id") != identity:
            raise BankError("CONVERSATION_MISMATCH", "这张确认单属于另一条会话，请回到原事项办理。")

    @app.get("/api/conversations")
    def conversations():
        sync_events()
        return {"conversations": app.state.store.list()}

    @app.post("/api/conversations")
    def create_conversation(request: ThreadRequest):
        return app.state.store.create(request.title)

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
            subscription = action.get("kind") == "subscription"
            current = (app.state.subscriptions if subscription else app.state.bank).get_plan(action["id"])
            if current:
                message["action"] = (
                    current if subscription else {**current, "kind": "transfer", "title": "转账确认"}
                )
                if not subscription:
                    message["plan"] = current
        bank_plan = app.state.bank.pending_for_conversation(identity)
        result["pending_action"] = app.state.subscriptions.pending_for_conversation(identity)
        if bank_plan:
            result["pending_action"] = {**bank_plan, "kind": "transfer", "title": "转账确认"}
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
        app.state.bank.invalidate_pending_plans(conversation_id)
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
        }

    @app.get("/api/overview")
    def overview():
        return app.state.bank.overview()

    def validate_request(request: ChatRequest) -> str:
        message = request.message.strip()
        if not message:
            raise HTTPException(422, detail={"code": "EMPTY_MESSAGE", "message": "请先输入你的需求。"})
        if request.conversation_id:
            try:
                UUID(request.conversation_id)
            except ValueError as error:
                raise HTTPException(
                    422, detail={"code": "INVALID_CONVERSATION", "message": "对话标识不正确，请新建对话。"}
                ) from error
            app.state.store.load(request.conversation_id)
        return message

    @app.post("/api/chat")
    async def chat(request: ChatRequest):
        message = validate_request(request)
        try:
            return await app.state.agent.chat(message, request.conversation_id)
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
                result = await app.state.agent.chat(message, request.conversation_id, emit=emit)
                await emit("result", result)
            except ModelError as error:
                await emit("error", {"code": error.code, "message": error.message})
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


app = create_app()
