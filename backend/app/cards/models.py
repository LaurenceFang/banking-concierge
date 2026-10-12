"""Persistence models for fictional cards and their confirmation plans."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Separate metadata keeps card setup independent from the bank ledger."""


class Card(Base):
    __tablename__ = "card_records"
    __table_args__ = (
        CheckConstraint("kind IN ('debit', 'credit')", name="ck_card_kind"),
        CheckConstraint("status IN ('active', 'frozen', 'lost', 'closed')", name="ck_card_status"),
        CheckConstraint("daily_limit_minor > 0", name="ck_card_daily_limit_positive"),
        CheckConstraint("outstanding_minor >= 0", name="ck_card_outstanding_nonnegative"),
        CheckConstraint("version >= 1", name="ck_card_version_positive"),
        CheckConstraint(
            "(kind = 'debit' AND credit_limit_minor IS NULL) OR "
            "(kind = 'credit' AND credit_limit_minor IS NOT NULL)",
            name="ck_card_credit_limit_matches_kind",
        ),
        Index("ix_card_records_owner", "owner_id"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    last4: Mapped[str] = mapped_column(String(4), nullable=False)
    account_id: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    daily_limit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credit_limit_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    outstanding_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class CardApplication(Base):
    __tablename__ = "card_applications"
    __table_args__ = (
        CheckConstraint("card_kind IN ('debit', 'credit')", name="ck_card_application_kind"),
        CheckConstraint("status = 'submitted'", name="ck_card_application_status"),
        UniqueConstraint("operation_id", name="uq_card_application_operation"),
        Index("ix_card_applications_owner_submitted", "owner_id", "submitted_at"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    card_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="submitted")
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(96), nullable=False)


class CardActionPlan(Base):
    __tablename__ = "card_action_plans"
    __table_args__ = (
        CheckConstraint(
            "action IN ('freeze', 'unfreeze', 'report_lost', 'set_daily_limit', "
            "'set_credit_limit', 'apply_card')",
            name="ck_card_action_plan_action",
        ),
        CheckConstraint(
            "status IN ('pending', 'completed', 'declined', 'invalidated', 'expired')",
            name="ck_card_action_plan_status",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN ('approve', 'decline')", name="ck_card_action_plan_decision"
        ),
        UniqueConstraint("operation_id", name="uq_card_action_plan_operation"),
        Index("ix_card_action_plans_owner_conversation", "owner_id", "conversation_id", "status"),
        Index(
            "ux_card_action_plans_pending_conversation",
            "conversation_id",
            unique=True,
            sqlite_where=text("conversation_id IS NOT NULL AND status = 'pending'"),
            postgresql_where=text("conversation_id IS NOT NULL AND status = 'pending'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    card_id: Mapped[str | None] = mapped_column(String(80), ForeignKey("card_records.id"), nullable=True)
    card_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    card_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before_state: Mapped[dict[str, object]] = mapped_column("before_state", JSON, nullable=False)
    after_state: Mapped[dict[str, object]] = mapped_column("after_state", JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    outcome_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    outcome_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    outcome_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
