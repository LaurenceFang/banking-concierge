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
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import BankingAgent
from .bank.service import BankError, BankService
from .config import MODEL, ROOT, database_url
from .conversations import ConversationStore
from .knowledge.service import KnowledgeService
from .llm import DeepSeekClient, ModelError

logger = logging.getLogger("concierge")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None


class DecisionRequest(BaseModel):
    decision: Literal["approve", "decline"]


def create_app(db_url: str | None = None, model=None, knowledge_dir: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        url = db_url or database_url()
        bank = BankService(url)
        bank.seed()
        knowledge = KnowledgeService(knowledge_dir or ROOT / "knowledge")
        store = ConversationStore(url)
        client = model or DeepSeekClient()
        app.state.bank, app.state.knowledge, app.state.store, app.state.model = bank, knowledge, store, client
        app.state.agent = BankingAgent(bank, knowledge, store, client)
        yield
        await client.close()
        store.close()
        bank.dispose()

    app = FastAPI(title="银小二 · Banking Concierge", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type"])

    @app.get("/api/health")
    def health():
        return {"status": "ok", "model": MODEL, "model_configured": app.state.model.configured,
                "storage": app.state.bank.dialect_name, "knowledge_documents": app.state.knowledge.document_count}

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
                raise HTTPException(422, detail={"code": "INVALID_CONVERSATION", "message": "对话标识不正确，请新建对话。"}) from error
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

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/plans/{plan_id}/decision")
    def decide(plan_id: str, request: DecisionRequest):
        try:
            result = app.state.bank.decide_plan(plan_id, request.decision)
            app.state.store.note_decision(plan_id, result["message"])
            return result
        except BankError as error:
            raise HTTPException(409, detail={"code": error.code, "message": error.message}) from error

    built = ROOT / "web" / "dist"
    if (built / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=built / "assets"), name="assets")

        @app.get("/")
        def index():
            return FileResponse(built / "index.html")

    return app


app = create_app()
