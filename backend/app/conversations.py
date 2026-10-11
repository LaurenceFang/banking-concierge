"""Persist chat continuity separately from the authoritative banking ledger."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from threading import RLock
from uuid import uuid4

from sqlalchemy import Column, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Session


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "chat_conversations"
    id = Column(String(80), primary_key=True)
    messages = Column(Text, nullable=False)
    updated_at = Column(String(40), nullable=False)


class ConversationStore:
    def __init__(self, url: str) -> None:
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        options = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, **options)
        Base.metadata.create_all(self.engine)
        self._lock = RLock()

    def load(self, conversation_id: str | None) -> tuple[str, list[dict]]:
        if not conversation_id:
            return str(uuid4()), []
        with self._lock, Session(self.engine) as session:
            value = session.get(Conversation, conversation_id)
            return conversation_id, json.loads(value.messages) if value else []

    def save(self, conversation_id: str, messages: list[dict]) -> None:
        # Keep complete assistant/tool groups; never retain an orphan tool result.
        with self._lock, Session(self.engine) as session, session.begin():
            value = session.get(Conversation, conversation_id)
            if value is None:
                value = Conversation(id=conversation_id)
                session.add(value)
            value.messages = json.dumps(messages, ensure_ascii=False)
            value.updated_at = datetime.now(UTC).isoformat()

    def note_decision(self, plan_id: str, message: str) -> None:
        with self._lock, Session(self.engine) as session, session.begin():
            for value in session.scalars(select(Conversation)):
                messages = json.loads(value.messages)
                if any(plan_id in str(item.get("content", "")) for item in messages):
                    messages.append({"role": "system", "content": f"银行执行结果：{message}。后续不得重复创建已完成交易。"})
                    value.messages = json.dumps(messages, ensure_ascii=False)
                    value.updated_at = datetime.now(UTC).isoformat()

    def close(self) -> None:
        self.engine.dispose()
