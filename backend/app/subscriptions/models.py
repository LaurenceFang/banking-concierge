"""Persistence models for fictional demo subscriptions and their actions."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Separate metadata keeps subscription setup independent from the bank ledger."""


class SubscriptionContract(Base):
    __tablename__ = "subscription_contracts"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    merchant: Mapped[str] = mapped_column(String(160), nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    cycle: Mapped[str] = mapped_column(String(24), nullable=False)
    next_charge_on: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    autopay_enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    cancel_supported: Mapped[bool] = mapped_column(nullable=False, default=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="demo_contract")


class SubscriptionActionPlan(Base):
    __tablename__ = "subscription_action_plans"
    __table_args__ = (
        Index("ix_subscription_plans_conversation_status", "conversation_id", "status"),
        Index("ix_subscription_plans_subscription_status", "subscription_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    subscription_id: Mapped[str] = mapped_column(
        ForeignKey("subscription_contracts.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    conversation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    outcome_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    outcome_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    outcome_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(80), nullable=True, unique=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RenewalNotification(Base):
    __tablename__ = "subscription_renewal_notifications"
    __table_args__ = (
        UniqueConstraint("subscription_id", "next_charge_on", name="uq_subscription_renewal_date"),
        Index("ix_subscription_renewal_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    subscription_id: Mapped[str] = mapped_column(
        ForeignKey("subscription_contracts.id"), nullable=False
    )
    next_charge_on: Mapped[date] = mapped_column(Date, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="renewal")
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PersonalSubscriptionRecord(Base):
    """A user's reminder record; it has no merchant execution capabilities."""

    __tablename__ = "personal_subscription_records"
    __table_args__ = (
        UniqueConstraint("owner_id", "client_request_id", name="uq_personal_subscription_request"),
        Index("ix_personal_subscription_due", "owner_id", "active", "next_charge_on"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    cycle: Mapped[str] = mapped_column(String(24), nullable=False)
    next_charge_on: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    note: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    reminder_enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    lead_days: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    active: Mapped[bool] = mapped_column(nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    client_request_id: Mapped[str] = mapped_column(String(120), nullable=False)
    candidate_merchant_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    candidate_cycle: Mapped[str | None] = mapped_column(String(24), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SubscriptionCandidateFeedback(Base):
    """A correction anchored to stable merchant + cadence identity, not row ids."""

    __tablename__ = "subscription_candidate_feedback"
    __table_args__ = (Index("ix_candidate_feedback_updated", "updated_at"),)

    merchant_key: Mapped[str] = mapped_column(String(180), primary_key=True)
    cycle: Mapped[str] = mapped_column(String(24), primary_key=True)
    merchant: Mapped[str] = mapped_column(String(160), nullable=False)
    feedback: Mapped[str] = mapped_column(String(24), nullable=False)
    personal_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("personal_subscription_records.id", ondelete="SET NULL"), nullable=True
    )
    client_request_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    request_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PersonalRenewalNotification(Base):
    __tablename__ = "personal_subscription_notifications"
    __table_args__ = (
        UniqueConstraint("subscription_id", "next_charge_on", name="uq_personal_renewal_date"),
        Index("ix_personal_renewal_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(180), primary_key=True)
    subscription_id: Mapped[str] = mapped_column(
        ForeignKey("personal_subscription_records.id", ondelete="CASCADE"), nullable=False
    )
    next_charge_on: Mapped[date] = mapped_column(Date, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="personal_renewal")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="personal_subscription")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
