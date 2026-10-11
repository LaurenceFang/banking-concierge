"""Persist model continuity and user-visible threads independently of the ledger."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from threading import RLock
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import Boolean, Column, Integer, String, Text, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Session

OWNER = "demo-user-001"
MAIN_ID = str(uuid5(NAMESPACE_URL, "banking-concierge:demo-user-001:main"))


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


def _now():
    return datetime.now(UTC).isoformat()


class ConversationStore:
    def __init__(self, url: str):
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        options = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, **options)
        Base.metadata.create_all(self.engine)
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

    def create(self, title: str = "临时事项") -> dict:
        title = title.strip() or "临时事项"
        identity, now = str(uuid4()), _now()
        with self._lock, Session(self.engine) as session, session.begin():
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
            return self._summary(row, meta)

    @staticmethod
    def _summary(row, meta):
        return {
            "id": row.id,
            "title": meta.title,
            "kind": meta.kind,
            "updated_at": row.updated_at,
            "unread_count": meta.unread_count,
        }

    @staticmethod
    def _require(session, identity):
        row, meta = session.get(Conversation, identity), session.get(ThreadMeta, identity)
        if row is None or meta is None or meta.owner_id != OWNER:
            raise ConversationError("CONVERSATION_NOT_FOUND", "没有找到这条会话，请从会话栏重新选择。")
        return row, meta

    def list(self) -> list[dict]:
        with self._lock, Session(self.engine) as session:
            rows = session.execute(
                select(Conversation, ThreadMeta)
                .join(ThreadMeta, Conversation.id == ThreadMeta.id)
                .where(ThreadMeta.owner_id == OWNER)
            ).all()
            items = [self._summary(row, meta) for row, meta in rows]
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
            return {"conversation": self._summary(row, meta), "messages": messages, "pending_action": None}

    def rename(self, identity: str, title: str) -> dict:
        if not title.strip():
            raise ConversationError("EMPTY_TITLE", "会话名称不能为空。")
        with self._lock, Session(self.engine) as session, session.begin():
            row, meta = self._require(session, identity)
            meta.title, row.updated_at = title.strip()[:80], _now()
            return self._summary(row, meta)

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
            events = [m for m in json.loads(row.messages) if m.get("_event_id") and m["_event_id"] not in ids]
            row.messages = json.dumps(messages + events, ensure_ascii=False)
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
            for view in session.scalars(select(ViewMessage)).all():
                payload = json.loads(view.payload)
                action = payload.get("action") or payload.get("plan")
                if not action or action.get("id") != plan_id:
                    continue
                targets.add(view.conversation_id)
                if receipt:
                    payload["receipt"] = receipt
                    if payload.get("action"):
                        payload["action"]["status"] = receipt["status"]
                    if payload.get("plan"):
                        payload["plan"]["status"] = receipt["status"]
                    view.payload = json.dumps(payload, ensure_ascii=False)
            for identity in targets:
                row, _ = self._require(session, identity)
                history = json.loads(row.messages)
                event = f"decision:{plan_id}"
                if not any(m.get("_event_id") == event for m in history):
                    history.append(
                        {
                            "role": "system",
                            "content": f"银行执行结果：{message}。已完成操作不能重复创建。",
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
