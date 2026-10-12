"""Persistence models for the fictional, fixed-NAV wealth simulation.

Wealth rows share the bank package's SQLAlchemy metadata and engine so that
cash movements, bank ledger entries, orders, and positions can commit atomically.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.bank.models import Base


class WealthProduct(Base):
    __tablename__ = "wealth_products"
    __table_args__ = (
        CheckConstraint("risk_level BETWEEN 1 AND 5", name="ck_wealth_product_risk_level"),
        CheckConstraint("min_purchase_minor > 0", name="ck_wealth_product_minimum"),
        CheckConstraint("purchase_fee_bps >= 0", name="ck_wealth_product_purchase_fee"),
        CheckConstraint("redemption_fee_bps >= 0", name="ck_wealth_product_redemption_fee"),
        CheckConstraint("lock_days >= 0", name="ck_wealth_product_lock_days"),
        CheckConstraint("settlement_business_days = 1", name="ck_wealth_product_settlement_days"),
        CheckConstraint("nav_minor = 100", name="ck_wealth_product_fixed_nav"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    risk_level: Mapped[int] = mapped_column(Integer, nullable=False)
    min_purchase_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    purchase_fee_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    redemption_fee_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    lock_days: Mapped[int] = mapped_column(Integer, nullable=False)
    settlement_business_days: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    nav_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    rule_version: Mapped[str] = mapped_column(String(48), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)


class WealthRiskProfile(Base):
    __tablename__ = "wealth_risk_profiles"

    owner_id: Mapped[str] = mapped_column(ForeignKey("bank_users.id"), primary_key=True)
    answers_json: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_label: Mapped[str] = mapped_column(String(8), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(48), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)


class WealthPosition(Base):
    __tablename__ = "wealth_positions"
    __table_args__ = (
        CheckConstraint("principal_minor >= 0", name="ck_wealth_position_principal"),
        CheckConstraint("available_minor >= 0", name="ck_wealth_position_available"),
        CheckConstraint("redemption_pending_minor >= 0", name="ck_wealth_position_pending"),
        CheckConstraint(
            "principal_minor = available_minor + redemption_pending_minor",
            name="ck_wealth_position_share_conservation",
        ),
        Index("ix_wealth_positions_owner_product", "owner_id", "product_id"),
    )

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("bank_users.id"), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("wealth_products.id"), nullable=False)
    principal_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    available_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    redemption_pending_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    settlement_date: Mapped[date] = mapped_column(Date, nullable=False)
    unlock_on: Mapped[date] = mapped_column(Date, nullable=False)
    risk_level: Mapped[int] = mapped_column(Integer, nullable=False)
    source_order_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)


class WealthOrder(Base):
    __tablename__ = "wealth_orders"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_wealth_order_amount"),
        CheckConstraint("fee_minor >= 0", name="ck_wealth_order_fee"),
        CheckConstraint(
            "(action = 'purchase' AND total_minor = amount_minor + fee_minor AND payout_minor = 0) "
            "OR (action = 'redeem' AND total_minor = amount_minor "
            "AND payout_minor = amount_minor - fee_minor)",
            name="ck_wealth_order_amounts_by_action",
        ),
        CheckConstraint("payout_minor >= 0", name="ck_wealth_order_payout"),
        Index("ix_wealth_orders_due", "status", "settles_on"),
        Index("ix_wealth_orders_owner_date", "owner_id", "requested_on"),
    )

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("bank_users.id"), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("wealth_products.id"), nullable=False)
    position_id: Mapped[str | None] = mapped_column(ForeignKey("wealth_positions.id"), nullable=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("bank_accounts.id"), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fee_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payout_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending_settlement")
    lock_days: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_level: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_on: Mapped[date] = mapped_column(Date, nullable=False)
    settles_on: Mapped[date] = mapped_column(Date, nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    operation_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    conversation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class WealthActionPlan(Base):
    __tablename__ = "wealth_action_plans"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_wealth_plan_amount"),
        CheckConstraint("fee_minor >= 0", name="ck_wealth_plan_fee"),
        CheckConstraint(
            "(action = 'purchase' AND total_minor = amount_minor + fee_minor AND payout_minor = 0) "
            "OR (action = 'redeem' AND total_minor = amount_minor "
            "AND payout_minor = amount_minor - fee_minor)",
            name="ck_wealth_plan_amounts_by_action",
        ),
        CheckConstraint("payout_minor >= 0", name="ck_wealth_plan_payout"),
        Index("ix_wealth_plans_conversation_status", "conversation_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("bank_users.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("wealth_products.id"), nullable=False)
    product_rule_version: Mapped[str] = mapped_column(String(48), nullable=False)
    risk_profile_rule_version: Mapped[str | None] = mapped_column(String(48), nullable=True)
    risk_profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    risk_level: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[str] = mapped_column(ForeignKey("bank_accounts.id"), nullable=False)
    position_id: Mapped[str | None] = mapped_column(ForeignKey("wealth_positions.id"), nullable=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fee_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payout_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    requested_on: Mapped[date] = mapped_column(Date, nullable=False)
    settles_on: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    conversation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    outcome_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    outcome_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    outcome_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WealthSimulationClock(Base):
    __tablename__ = "wealth_simulation_clock"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    current_date: Mapped[date] = mapped_column(Date, nullable=False)
