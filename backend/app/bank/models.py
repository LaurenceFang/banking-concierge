"""SQLAlchemy models for the synthetic Banking Concierge ledger."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "bank_users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)


class Account(Base):
    __tablename__ = "bank_accounts"
    __table_args__ = (
        CheckConstraint("balance_minor >= 0", name="ck_bank_account_nonnegative_balance"),
        CheckConstraint("currency = 'CNY'", name="ck_bank_account_currency"),
        Index("ix_bank_accounts_owner", "owner_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("bank_users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")


class Recipient(Base):
    __tablename__ = "bank_recipients"
    __table_args__ = (
        UniqueConstraint("owner_id", "destination_account_id", name="uq_bank_recipient_destination"),
        Index("ix_bank_recipients_owner_name", "owner_id", "name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("bank_users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    bank_name: Mapped[str] = mapped_column(String(120), nullable=False)
    account_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    destination_account_id: Mapped[str] = mapped_column(ForeignKey("bank_accounts.id"), nullable=False)


class LedgerTransaction(Base):
    __tablename__ = "bank_transactions"
    __table_args__ = (
        CheckConstraint("amount_minor != 0", name="ck_bank_transaction_nonzero_amount"),
        CheckConstraint("currency = 'CNY'", name="ck_bank_transaction_currency"),
        Index("ix_bank_transactions_account_created", "account_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("bank_accounts.id"), nullable=False)
    description: Mapped[str] = mapped_column(String(240), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)


class TransferPlan(Base):
    __tablename__ = "bank_transfer_plans"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_bank_plan_positive_amount"),
        CheckConstraint("fee_minor >= 0", name="ck_bank_plan_nonnegative_fee"),
        CheckConstraint("total_minor = amount_minor + fee_minor", name="ck_bank_plan_total"),
        Index("ix_bank_transfer_plans_conversation", "conversation_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("bank_users.id"), nullable=False)
    source_account_id: Mapped[str] = mapped_column(ForeignKey("bank_accounts.id"), nullable=False)
    recipient_id: Mapped[str] = mapped_column(ForeignKey("bank_recipients.id"), nullable=False)
    destination_account_id: Mapped[str] = mapped_column(ForeignKey("bank_accounts.id"), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fee_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
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


class FundHold(Base):
    __tablename__ = "bank_fund_holds"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_hold_positive_amount"),
        CheckConstraint("remaining_minor >= 0 AND remaining_minor <= amount_minor", name="ck_hold_remaining"),
        Index("ix_hold_account_status", "account_id", "status"),
    )
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("bank_accounts.id"), nullable=False)
    reference: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    remaining_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
