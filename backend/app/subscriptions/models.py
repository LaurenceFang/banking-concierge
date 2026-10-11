"""Persistence models for fictional demo subscriptions and their actions."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
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
