"""Persistence models for AA requests, scheduled transfers, and task plans."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Task metadata is kept separate from the bank ledger metadata."""


class AATask(Base):
    __tablename__ = "task_aa_requests"
    __table_args__ = (
        CheckConstraint("total_minor > 0", name="ck_task_aa_total_positive"),
        CheckConstraint("collectable_minor >= 0", name="ck_task_aa_collectable_nonnegative"),
        CheckConstraint("collected_minor >= 0", name="ck_task_aa_collected_nonnegative"),
        CheckConstraint("state_version >= 1", name="ck_task_aa_version_positive"),
        CheckConstraint(
            "status IN ('awaiting_payment', 'partial', 'completed', 'cancelled')",
            name="ck_task_aa_status",
        ),
        Index("ix_task_aa_owner_created", "owner_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    collectable_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    collected_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    message: Mapped[str] = mapped_column(Text, nullable=False)


class AAParticipant(Base):
    __tablename__ = "task_aa_participants"
    __table_args__ = (
        CheckConstraint("share_minor >= 0", name="ck_task_aa_participant_share_nonnegative"),
        CheckConstraint(
            "status IN ('self', 'pending', 'paid', 'cancelled')",
            name="ck_task_aa_participant_status",
        ),
        UniqueConstraint("task_id", "participant_id", name="uq_task_aa_participant"),
        Index("ix_task_aa_participants_task_order", "task_id", "position"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(80), nullable=False)
    participant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    destination_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    share_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ScheduledTransfer(Base):
    __tablename__ = "task_scheduled_transfers"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_task_schedule_amount_positive"),
        CheckConstraint("reserved_minor >= 0", name="ck_task_schedule_reserved_nonnegative"),
        CheckConstraint("state_version >= 1", name="ck_task_schedule_version_positive"),
        CheckConstraint(
            "status IN ('scheduled', 'completed', 'cancelled', 'expired', 'failed')",
            name="ck_task_schedule_status",
        ),
        UniqueConstraint("operation_id", name="uq_task_schedule_operation"),
        Index("ix_task_schedule_due", "status", "execute_at", "deadline_at"),
        Index("ix_task_schedule_owner_created", "owner_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    recipient_id: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    recipient_name: Mapped[str] = mapped_column(String(120), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    execute_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    hold_id: Mapped[str] = mapped_column(String(80), nullable=False)
    reserved_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class TaskActionPlan(Base):
    __tablename__ = "task_action_plans"
    __table_args__ = (
        CheckConstraint(
            "action IN ('create_aa', 'cancel_aa', 'schedule_transfer', 'cancel_schedule')",
            name="ck_task_plan_action",
        ),
        CheckConstraint(
            "status IN ('pending', 'completed', 'declined', 'invalidated', 'expired')",
            name="ck_task_plan_status",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN ('approve', 'decline')",
            name="ck_task_plan_decision",
        ),
        UniqueConstraint("operation_id", name="uq_task_plan_operation"),
        Index("ix_task_plan_owner_conversation", "owner_id", "conversation_id", "status"),
        Index(
            "ux_task_plan_pending_conversation",
            "owner_id",
            "conversation_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    task_id: Mapped[str] = mapped_column(String(80), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    total_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    collectable_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    participants: Mapped[list[dict[str, object]] | None] = mapped_column(JSON, nullable=True)
    recipient_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    destination_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recipient_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    execute_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reserved_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    task_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    outcome_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    outcome_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    outcome_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskEvent(Base):
    __tablename__ = "task_events"
    __table_args__ = (
        UniqueConstraint("task_id", "state_version", name="uq_task_event_version"),
        Index("ix_task_events_owner_created", "owner_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="task")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="tasks")
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    plan_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    receipt: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
