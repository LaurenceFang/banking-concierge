"""Persist model continuity and user-visible threads independently of the ledger."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from threading import RLock
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import Boolean, Column, Integer, String, Text, create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Session

OWNER = "demo-user-001"
MAIN_ID = str(uuid5(NAMESPACE_URL, "banking-concierge:demo-user-001:main"))
SCOPES = frozenset(
    {
        "accounts/overview",
        "accounts/accounts",
        "accounts/cards",
        "bills/entries",
        "bills/payments",
        "bills/analysis",
        "bills/import",
        "wealth/positions",
        "wealth/products",
        "wealth/orders",
        "wealth/risk",
        "life/subscriptions",
        "life/arrangements",
    }
)


class ConversationError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "chat_conversations"
    id = Column(String(80), primary_key=True)
    messages = Column(Text, nullable=False)
    updated_at = Column(String(40), nullable=False)


class ThreadMeta(Base):
    __tablename__ = "chat_thread_metadata"
    id = Column(String(80), primary_key=True)
    owner_id = Column(String(80), nullable=False)
    title = Column(String(120), nullable=False)
    kind = Column(String(24), nullable=False)
    created_at = Column(String(40), nullable=False)
    unread_count = Column(Integer, nullable=False, default=0)


class ViewMessage(Base):
    __tablename__ = "chat_view_messages"
    id = Column(String(80), primary_key=True)
    conversation_id = Column(String(80), nullable=False, index=True)
    payload = Column(Text, nullable=False)
    created_at = Column(String(40), nullable=False)


class Notification(Base):
    __tablename__ = "chat_notifications"
    id = Column(String(160), primary_key=True)
    payload = Column(Text, nullable=False)
    created_at = Column(String(40), nullable=False)
    read = Column(Boolean, nullable=False, default=False)


class ConversationScope(Base):
    __tablename__ = "chat_conversation_scopes"
    conversation_id = Column(String(80), primary_key=True)
    scope = Column(String(80), primary_key=True)
    source = Column(String(24), nullable=False)
    is_primary = Column(Boolean, nullable=False, default=False)
    user_override = Column(Boolean, nullable=False, default=False)


class ConversationPin(Base):
    __tablename__ = "chat_conversation_pins"
    conversation_id = Column(String(80), primary_key=True)
    pinned_at = Column(String(40), nullable=False)


class ClientRequestReceipt(Base):
    __tablename__ = "chat_client_request_receipts"
    id = Column(String(160), primary_key=True)
    request_type = Column(String(16), nullable=False)
    fingerprint = Column(String(64), nullable=False)
    conversation_id = Column(String(80), nullable=False)
    status = Column(String(16), nullable=False)
    response = Column(Text, nullable=True)
    pending_action = Column(Text, nullable=True)
    error_code = Column(String(80), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(String(40), nullable=False)
    updated_at = Column(String(40), nullable=False)


def _now():
    return datetime.now(UTC).isoformat()


class ConversationStore:
    def __init__(self, url: str):
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        options = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, **options)
        Base.metadata.create_all(self.engine)
        # create_all does not add fields to an existing table from early v2 builds.
        with self.engine.begin() as connection:
            columns = {column["name"] for column in inspect(connection).get_columns("chat_client_request_receipts")}
            if "pending_action" not in columns:
                optional = "IF NOT EXISTS " if connection.dialect.name == "postgresql" else ""
                connection.execute(text(
                    f"ALTER TABLE chat_client_request_receipts ADD COLUMN {optional}pending_action TEXT"
                ))
        self._lock = RLock()
        with self._lock, Session(self.engine) as session, session.begin():
            if session.get(Conversation, MAIN_ID) is None:
                session.add(Conversation(id=MAIN_ID, messages="[]", updated_at=_now()))
            if session.get(ThreadMeta, MAIN_ID) is None:
                session.add(
                    ThreadMeta(
                        id=MAIN_ID,
                        owner_id=OWNER,
                        title="日常助手",
                        kind="main",
                        created_at=_now(),
                        unread_count=0,
                    )
                )
            for row in session.scalars(select(Conversation)).all():
                if session.get(ThreadMeta, row.id) is None:
                    history = json.loads(row.messages)
                    title = next(
                        (m.get("content", "") for m in history if m.get("role") == "user"), "临时事项"
                    )
                    session.add(
                        ThreadMeta(
                            id=row.id,
                            owner_id=OWNER,
                            title=title[:40] or "临时事项",
                            kind="temporary",
                            created_at=row.updated_at,
                            unread_count=0,
                        )
                    )
                    self._migrate_views(session, row, history)
            self._backfill_action_scopes(session)

    @staticmethod
    def validate_scope(scope: str | None) -> str | None:
        if scope is not None and scope not in SCOPES:
            raise ConversationError("INVALID_SCOPE", "这个页面范围暂不支持，请刷新页面后重试。")
        return scope

    @staticmethod
    def request_conversation_id(conversation_id: str | None, client_request_id: str | None) -> str | None:
        if conversation_id:
            return conversation_id
        if client_request_id:
            return str(uuid5(NAMESPACE_URL, f"banking-concierge:first-chat:{OWNER}:{client_request_id}"))
        return None

    @staticmethod
    def _request_fingerprint(request_type: str, values: dict) -> str:
        encoded = json.dumps({"type": request_type, **values}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def scope_for_action(action: dict | None) -> str | None:
        if not isinstance(action, dict):
            return None
        kind = action.get("kind")
        if kind == "transfer":
            return "bills/payments"
        if kind == "card":
            return "accounts/cards"
        if kind == "subscription":
            return "life/subscriptions"
        if kind == "task":
            return "bills/payments"
        if kind == "workflow":
            return "life/arrangements"
        if kind == "wealth":
            return "wealth/orders"
        return None

    @classmethod
    def _backfill_action_scopes(cls, session: Session) -> None:
        # Existing histories are classified only from persisted, server-grounded
        # action records. Titles and free-text messages are not used as labels.
        for view in session.scalars(select(ViewMessage)).all():
            try:
                payload = json.loads(view.payload)
            except (ValueError, TypeError):
                continue
            scope = cls.scope_for_action(payload.get("action") or payload.get("plan"))
            if scope:
                cls._add_scope(session, view.conversation_id, scope, source="existing_action")

    @staticmethod
    def _add_scope(
        session: Session,
        identity: str,
        scope: str,
        *,
        source: str,
        user_override: bool = False,
    ) -> None:
        if identity == MAIN_ID:
            # The global main thread stays global; page-originated ordinary
            # conversations carry page scopes instead.
            return
        current = session.get(ConversationScope, (identity, scope))
        if current:
            if user_override:
                current.user_override = True
                current.source = source
            return
        has_primary = session.scalar(
            select(ConversationScope.conversation_id)
            .where(ConversationScope.conversation_id == identity, ConversationScope.is_primary.is_(True))
            .limit(1)
        ) is not None
        session.add(
            ConversationScope(
                conversation_id=identity,
                scope=scope,
                source=source,
                is_primary=not has_primary,
                user_override=user_override,
            )
        )

    def associate_scope(
        self, identity: str, scope: str, *, source: str = "explicit"
    ) -> None:
        self.validate_scope(scope)
        with self._lock, Session(self.engine) as session, session.begin():
            self._require(session, identity)
            self._add_scope(session, identity, scope, source=source)

    @staticmethod
    def _request_error(receipt: ClientRequestReceipt) -> dict:
        return {"code": receipt.error_code or "CHAT_FAILED", "message": receipt.error_message or "这条对话请求没有完成，请重新发送。"}

    def start_chat_turn(
        self,
        message: str,
        *,
        conversation_id: str | None = None,
        scope: str | None = None,
        client_request_id: str | None = None,
        retry_failed: bool = False,
    ) -> dict:
        """Atomically claim a chat request and persist its first user message."""
        scope = self.validate_scope(scope)
        if retry_failed and not client_request_id:
            raise ConversationError("CHAT_RETRY_NOT_AVAILABLE", "请从失败消息旁的重试按钮重新发送。")
        identity = self.request_conversation_id(conversation_id, client_request_id) or str(uuid4())
        fingerprint = self._request_fingerprint("chat", {"message": message, "scope": scope})
        with self._lock:
            try:
                with Session(self.engine) as session, session.begin():
                    if client_request_id:
                        receipt = session.get(ClientRequestReceipt, client_request_id)
                        if receipt:
                            if (
                                receipt.request_type != "chat"
                                or receipt.fingerprint != fingerprint
                                or (conversation_id and receipt.conversation_id != conversation_id)
                            ):
                                raise ConversationError(
                                    "REQUEST_ID_REUSED", "这次发送标识已经用于另一条请求，请生成新的发送标识。"
                                )
                            if receipt.status == "completed":
                                return {
                                    "status": "completed",
                                    "conversation_id": receipt.conversation_id,
                                    "result": json.loads(receipt.response or "{}"),
                                }
                            if receipt.status == "failed":
                                if retry_failed:
                                    row, _ = self._require(session, receipt.conversation_id)
                                    raw = json.loads(row.messages)
                                    request_positions = [
                                        index
                                        for index, item in enumerate(raw)
                                        if item.get("role") == "user"
                                        and item.get("_client_request_id") == client_request_id
                                    ]
                                    if len(request_positions) != 1:
                                        raise ConversationError(
                                            "CHAT_RETRY_NOT_AVAILABLE",
                                            "无法安全定位这条失败消息，请在原对话中重新发送。",
                                        )
                                    position = request_positions[0]
                                    if any(
                                        item.get("role") in {"user", "assistant"}
                                        for item in raw[position + 1 :]
                                    ):
                                        raise ConversationError(
                                            "CHAT_RETRY_NOT_AVAILABLE",
                                            "这条对话已有更新消息，请从最新消息继续。",
                                        )
                                    receipt.status = "processing"
                                    receipt.error_code = receipt.error_message = None
                                    receipt.updated_at = _now()
                                    return {
                                        "status": "started",
                                        "conversation_id": receipt.conversation_id,
                                        "history": raw,
                                        "retry_action": json.loads(receipt.pending_action)
                                        if receipt.pending_action
                                        else None,
                                    }
                                return {
                                    "status": "failed",
                                    "conversation_id": receipt.conversation_id,
                                    "error": self._request_error(receipt),
                                }
                            return {"status": "processing", "conversation_id": receipt.conversation_id}

                        if retry_failed:
                            raise ConversationError(
                                "CHAT_RETRY_NOT_AVAILABLE", "没有找到可重试的失败消息，请重新发送你的需求。"
                            )

                        now = _now()
                        session.add(
                            ClientRequestReceipt(
                                id=client_request_id,
                                request_type="chat",
                                fingerprint=fingerprint,
                                conversation_id=identity,
                                status="processing",
                                created_at=now,
                                updated_at=now,
                            )
                        )
                        session.flush()

                    row, meta = session.get(Conversation, identity), session.get(ThreadMeta, identity)
                    if row is None and meta is None:
                        now = _now()
                        row = Conversation(id=identity, messages="[]", updated_at=now)
                        meta = ThreadMeta(
                            id=identity,
                            owner_id=OWNER,
                            title="临时事项",
                            kind="temporary",
                            created_at=now,
                            unread_count=0,
                        )
                        session.add_all([row, meta])
                    elif row is None or meta is None or meta.owner_id != OWNER:
                        raise ConversationError("CONVERSATION_NOT_FOUND", "没有找到这条对话，请从对话栏重新选择。")

                    created_at = _now()
                    raw = json.loads(row.messages)
                    raw_user = {"role": "user", "content": message}
                    if client_request_id:
                        raw_user["_client_request_id"] = client_request_id
                    raw.append(raw_user)
                    row.messages = json.dumps(raw, ensure_ascii=False)
                    row.updated_at = created_at
                    if meta.kind == "temporary" and meta.title == "临时事项":
                        meta.title = message[:40] or "对话"
                    payload = {
                        "id": str(uuid4()),
                        "role": "user",
                        "text": message,
                        "created_at": created_at,
                        "sources": [],
                    }
                    self._insert_view(session, identity, payload)
                    if scope:
                        self._add_scope(session, identity, scope, source="page")
                    return {"status": "started", "conversation_id": identity, "history": raw}
            except IntegrityError:
                # A second process may have claimed this stable key first. Read
                # its durable receipt and return a safe state instead of replaying.
                if client_request_id:
                    with Session(self.engine) as session:
                        receipt = session.get(ClientRequestReceipt, client_request_id)
                        if receipt and receipt.request_type == "chat" and receipt.fingerprint == fingerprint:
                            if conversation_id and receipt.conversation_id != conversation_id:
                                raise ConversationError("REQUEST_ID_REUSED", "这次发送标识已经用于另一条对话。")
                            if receipt.status == "completed":
                                return {"status": "completed", "conversation_id": receipt.conversation_id,
                                        "result": json.loads(receipt.response or "{}")}
                            if receipt.status == "failed":
                                return {"status": "failed", "conversation_id": receipt.conversation_id,
                                        "error": self._request_error(receipt)}
                            return {"status": "processing", "conversation_id": receipt.conversation_id}
                raise

    def finish_chat_request(self, client_request_id: str | None, result: dict) -> None:
        if not client_request_id:
            return
        with self._lock, Session(self.engine) as session, session.begin():
            receipt = session.get(ClientRequestReceipt, client_request_id)
            if receipt and receipt.request_type == "chat" and receipt.status != "completed":
                receipt.status = "completed"
                receipt.response = json.dumps(result, ensure_ascii=False)
                receipt.error_code = receipt.error_message = None
                receipt.updated_at = _now()

    def note_chat_action(self, client_request_id: str | None, action: dict) -> None:
        if not client_request_id:
            return
        with self._lock, Session(self.engine) as session, session.begin():
            receipt = session.get(ClientRequestReceipt, client_request_id)
            if receipt and receipt.request_type == "chat" and receipt.status == "processing":
                receipt.pending_action = json.dumps(action, ensure_ascii=False)
                receipt.updated_at = _now()

    def fail_chat_request(self, client_request_id: str | None, code: str, message: str) -> None:
        if not client_request_id:
            return
        with self._lock, Session(self.engine) as session, session.begin():
            receipt = session.get(ClientRequestReceipt, client_request_id)
            if receipt and receipt.request_type == "chat" and receipt.status == "processing":
                receipt.status = "failed"
                receipt.error_code, receipt.error_message = code, message
                receipt.updated_at = _now()

    @staticmethod
    def _migrate_views(session, row, history):
        plan, sources = None, []
        for index, item in enumerate(history):
            if item.get("role") == "tool":
                try:
                    data = json.loads(item.get("content") or "null")
                    if isinstance(data, dict) and isinstance(data.get("plan"), dict):
                        plan = data["plan"]
                    if isinstance(data, list) and data and isinstance(data[0], dict) and "doc_id" in data[0]:
                        sources = [{k: d[k] for k in ("doc_id", "title", "excerpt", "version")} for d in data]
                except (ValueError, KeyError, TypeError):
                    pass
            if item.get("role") not in {"user", "assistant"} or not item.get("content"):
                continue
            payload = {
                "id": str(uuid5(NAMESPACE_URL, f"legacy:{row.id}:{index}")),
                "role": item["role"],
                "text": item["content"],
                "created_at": row.updated_at,
                "sources": sources if item["role"] == "assistant" else [],
                "_legacy_index": index,
            }
            if item["role"] == "assistant" and plan:
                payload["plan"] = plan
                payload["action"] = {**plan, "kind": "transfer", "title": "转账确认"}
                plan = None
            session.add(
                ViewMessage(
                    id=payload["id"],
                    conversation_id=row.id,
                    payload=json.dumps(payload, ensure_ascii=False),
                    created_at=row.updated_at,
                )
            )

    def create(
        self,
        title: str = "临时事项",
        *,
        scope: str | None = None,
        client_request_id: str | None = None,
    ) -> dict:
        title = title.strip() or "临时事项"
        scope = self.validate_scope(scope)
        fingerprint = self._request_fingerprint("create", {"title": title, "scope": scope})
        with self._lock:
            try:
                with Session(self.engine) as session, session.begin():
                    if client_request_id:
                        receipt = session.get(ClientRequestReceipt, client_request_id)
                        if receipt:
                            if receipt.request_type != "create" or receipt.fingerprint != fingerprint:
                                raise ConversationError(
                                    "REQUEST_ID_REUSED", "这次操作标识已经用于另一条请求，请重新开始。"
                                )
                            if receipt.response:
                                return json.loads(receipt.response)
                            raise ConversationError("CHAT_IN_PROGRESS", "这条对话正在创建，请稍后刷新。")
                    identity, now = str(uuid4()), _now()
                    row = Conversation(id=identity, messages="[]", updated_at=now)
                    meta = ThreadMeta(
                        id=identity,
                        owner_id=OWNER,
                        title=title[:80],
                        kind="temporary",
                        created_at=now,
                        unread_count=0,
                    )
                    session.add_all([row, meta])
                    if scope:
                        self._add_scope(session, identity, scope, source="page")
                    summary = self._summary(session, row, meta)
                    if client_request_id:
                        session.add(
                            ClientRequestReceipt(
                                id=client_request_id,
                                request_type="create",
                                fingerprint=fingerprint,
                                conversation_id=identity,
                                status="completed",
                                response=json.dumps(summary, ensure_ascii=False),
                                created_at=now,
                                updated_at=now,
                            )
                        )
                    return summary
            except IntegrityError:
                if client_request_id:
                    with Session(self.engine) as session:
                        receipt = session.get(ClientRequestReceipt, client_request_id)
                        if receipt and receipt.request_type == "create" and receipt.fingerprint == fingerprint and receipt.response:
                            return json.loads(receipt.response)
                raise

    @staticmethod
    def _summary(session: Session, row, meta):
        scopes = session.scalars(
            select(ConversationScope.scope)
            .where(ConversationScope.conversation_id == row.id)
            .order_by(ConversationScope.is_primary.desc(), ConversationScope.scope)
        ).all()
        pin = session.get(ConversationPin, row.id)
        return {
            "id": row.id,
            "title": meta.title,
            "kind": meta.kind,
            "updated_at": row.updated_at,
            "unread_count": meta.unread_count,
            "scopes": scopes,
            "pinned": pin is not None,
            "pinned_at": pin.pinned_at if pin else None,
        }

    @staticmethod
    def _require(session, identity):
        row, meta = session.get(Conversation, identity), session.get(ThreadMeta, identity)
        if row is None or meta is None or meta.owner_id != OWNER:
            raise ConversationError("CONVERSATION_NOT_FOUND", "没有找到这条会话，请从会话栏重新选择。")
        return row, meta

    def list(self, scope: str | None = None) -> list[dict]:
        scope = self.validate_scope(scope)
        with self._lock, Session(self.engine) as session:
            statement = (
                select(Conversation, ThreadMeta)
                .join(ThreadMeta, Conversation.id == ThreadMeta.id)
                .where(ThreadMeta.owner_id == OWNER)
            )
            if scope:
                scoped_ids = select(ConversationScope.conversation_id).where(ConversationScope.scope == scope)
                statement = statement.where((ThreadMeta.kind == "main") | Conversation.id.in_(scoped_ids))
            rows = session.execute(statement).all()
            items = [self._summary(session, row, meta) for row, meta in rows]
            return sorted(items, key=lambda x: (x["kind"] == "main", x["updated_at"]), reverse=True)

    def detail(self, identity: str) -> dict:
        with self._lock, Session(self.engine) as session:
            row, meta = self._require(session, identity)
            views = session.scalars(
                select(ViewMessage)
                .where(ViewMessage.conversation_id == identity)
                .order_by(ViewMessage.created_at, ViewMessage.id)
            ).all()
            messages = [json.loads(v.payload) for v in views]
            messages.sort(key=lambda m: (m["created_at"], m.get("_legacy_index", 1_000_000), m["id"]))
            for message in messages:
                message.pop("_legacy_index", None)
            return {"conversation": self._summary(session, row, meta), "messages": messages, "pending_action": None}

    def rename(self, identity: str, title: str) -> dict:
        if not title.strip():
            raise ConversationError("EMPTY_TITLE", "会话名称不能为空。")
        with self._lock, Session(self.engine) as session, session.begin():
            row, meta = self._require(session, identity)
            meta.title, row.updated_at = title.strip()[:80], _now()
            return self._summary(session, row, meta)

    def set_pinned(self, identity: str, pinned: bool) -> dict:
        if type(pinned) is not bool:
            raise ConversationError("INVALID_PIN", "请选择置顶或取消置顶。")
        with self._lock, Session(self.engine) as session, session.begin():
            if self.engine.dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            # Serialize simultaneous pin toggles without changing activity time.
            session.scalar(select(Conversation).where(Conversation.id == identity).with_for_update())
            row, meta = self._require(session, identity)
            if meta.kind == "main":
                raise ConversationError("CANNOT_PIN_MAIN", "主对话已有固定入口，无需置顶。")
            pin = session.get(ConversationPin, identity)
            if pinned and pin is None:
                session.add(ConversationPin(conversation_id=identity, pinned_at=_now()))
            elif not pinned and pin is not None:
                session.delete(pin)
            session.flush()
            return self._summary(session, row, meta)

    def mark_read(self, identity: str):
        with self._lock, Session(self.engine) as session, session.begin():
            _, meta = self._require(session, identity)
            meta.unread_count = 0
            if identity == MAIN_ID:
                for item in session.scalars(select(Notification).where(Notification.read.is_(False))):
                    item.read = True

    def load(self, conversation_id: str | None) -> tuple[str, list[dict]]:
        if not conversation_id:
            conversation_id = self.create()["id"]
        with self._lock, Session(self.engine) as session:
            row, _ = self._require(session, conversation_id)
            return conversation_id, json.loads(row.messages)

    def save(self, identity: str, messages: list[dict]):
        with self._lock, Session(self.engine) as session, session.begin():
            row, _ = self._require(session, identity)
            ids = {m.get("_event_id") for m in messages if m.get("_event_id")}
            current = {m["_event_id"]: m for m in json.loads(row.messages) if m.get("_event_id")}
            events = [m for event_id, m in current.items() if event_id not in ids]
            fresh = [current.get(m.get("_event_id"), m) for m in messages]
            row.messages = json.dumps(fresh + events, ensure_ascii=False)
            row.updated_at = _now()

    def append_user(self, identity: str, text: str) -> str:
        payload = {"id": str(uuid4()), "role": "user", "text": text, "created_at": _now(), "sources": []}
        with self._lock, Session(self.engine) as session, session.begin():
            row, meta = self._require(session, identity)
            if meta.kind == "temporary" and meta.title == "临时事项":
                meta.title = text[:40]
            row.updated_at = payload["created_at"]
            self._insert_view(session, identity, payload)
        return payload["id"]

    @staticmethod
    def _insert_view(session, identity, payload):
        session.add(
            ViewMessage(
                id=payload["id"],
                conversation_id=identity,
                payload=json.dumps(payload, ensure_ascii=False),
                created_at=payload["created_at"],
            )
        )

    def append_assistant(self, identity: str, result: dict) -> str:
        payload = {
            "id": str(uuid4()),
            "role": "assistant",
            "text": result["reply"],
            "created_at": _now(),
            "sources": result.get("sources", []),
            "plan": result.get("plan"),
            "action": result.get("action"),
        }
        with self._lock, Session(self.engine) as session, session.begin():
            row, meta = self._require(session, identity)
            row.updated_at = payload["created_at"]
            meta.unread_count += 1
            self._insert_view(session, identity, payload)
        return payload["id"]

    def note_decision(self, plan_id: str, message: str, receipt: dict | None = None):
        with self._lock, Session(self.engine) as session, session.begin():
            targets = set()
            changed = set()
            for view in session.scalars(select(ViewMessage)).all():
                payload = json.loads(view.payload)
                action = payload.get("action") or payload.get("plan")
                if not action or action.get("id") != plan_id:
                    continue
                old_receipt = payload.get("receipt") or {}
                if (receipt and receipt.get("task_id") and old_receipt.get("task_id") == receipt["task_id"]
                        and old_receipt.get("state_version", -1) > receipt.get("state_version", -1)):
                    continue
                if (
                    receipt
                    and old_receipt.get("order_id") == receipt.get("order_id")
                    and old_receipt.get("order_status") == "settled"
                    and receipt.get("order_status") == "pending_settlement"
                ):
                    # A late acceptance snapshot must not undo a newer settlement.
                    continue
                effective_receipt = receipt
                if receipt and receipt.get("task_id"):
                    # Durable task events report task_status, not confirmation status.
                    # Receiving such an event implies the creation/cancellation was
                    # already confirmed; it does not mean the task itself is complete.
                    effective_receipt = {
                        **old_receipt,
                        **receipt,
                        "status": receipt.get("status", old_receipt.get("status", "completed")),
                        "message": receipt.get("message", message),
                    }
                targets.add(view.conversation_id)
                if effective_receipt and old_receipt != effective_receipt:
                    changed.add(view.conversation_id)
                    payload["receipt"] = effective_receipt
                    if payload.get("action") and effective_receipt.get("status"):
                        payload["action"]["status"] = effective_receipt["status"]
                    if payload.get("plan") and effective_receipt.get("status"):
                        payload["plan"]["status"] = effective_receipt["status"]
                    view.payload = json.dumps(payload, ensure_ascii=False)
            for identity in targets:
                row, _ = self._require(session, identity)
                history = json.loads(row.messages)
                event = f"decision:{plan_id}"
                existing = next((m for m in history if m.get("_event_id") == event), None)
                content = f"银行执行结果：{message}。已受理或已完成操作不能重复创建。"
                if existing and existing["content"] == content and identity not in changed:
                    continue
                if existing:
                    existing["content"] = content
                else:
                    history.append(
                        {
                            "role": "system",
                            "content": content,
                            "_event_id": event,
                        }
                    )
                row.messages = json.dumps(history, ensure_ascii=False)
                row.updated_at = _now()

    def publish(self, event: dict) -> bool:
        identity, created = event["id"], event.get("created_at") or _now()
        with self._lock:
            try:
                with Session(self.engine) as session, session.begin():
                    if session.get(Notification, identity):
                        return False
                    notice = {**event, "created_at": created, "read": False}
                    session.add(
                        Notification(
                            id=identity,
                            payload=json.dumps(notice, ensure_ascii=False),
                            created_at=created,
                            read=False,
                        )
                    )
                    row, meta = self._require(session, MAIN_ID)
                    meta.unread_count += 1
                    row.updated_at = max(row.updated_at, created)
                    payload = {
                        "id": str(uuid5(NAMESPACE_URL, f"notice:{identity}")),
                        "role": "notification",
                        "text": event["text"],
                        "title": event["title"],
                        "created_at": created,
                        "sources": [],
                        "notification": notice,
                        "receipt": event.get("receipt"),
                    }
                    self._insert_view(session, MAIN_ID, payload)
                    history = json.loads(row.messages)
                    history.append(
                        {
                            "role": "system",
                            "content": f"已核验的业务通知：{event['title']}。{event['text']}",
                            "_event_id": identity,
                        }
                    )
                    row.messages = json.dumps(history, ensure_ascii=False)
                    return True
            except IntegrityError:
                with Session(self.engine) as session:
                    if session.get(Notification, identity):
                        return False
                raise

    def notifications(self) -> list[dict]:
        with self._lock, Session(self.engine) as session:
            return [
                {**json.loads(n.payload), "read": n.read}
                for n in session.scalars(
                    select(Notification).order_by(Notification.created_at.desc(), Notification.id)
                )
            ]

    def close(self):
        self.engine.dispose()
