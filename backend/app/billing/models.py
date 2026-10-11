"""Database models for the independent imported and demo billing ledger."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Float, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class BillingBase(DeclarativeBase):
    pass


class BillingRecord(BillingBase):
    """A bill imported from a file or seeded as fictional demo activity.

    Bank transactions intentionally stay in the bank package's tables and are
    only projected into the shared Entry shape when queried.
    """

    __tablename__ = "billing_records"
    __table_args__ = (
        CheckConstraint("source IN ('import', 'demo')", name="ck_billing_record_source"),
        CheckConstraint(
            "transaction_kind IN ('expense', 'refund', 'income', 'transfer', 'opening')",
            name="ck_billing_record_kind",
        ),
        CheckConstraint("currency = 'CNY'", name="ck_billing_record_currency"),
        UniqueConstraint("source", "source_key", name="uq_billing_record_source_key"),
        UniqueConstraint("source", "content_hash", name="uq_billing_record_content"),
        Index("ix_billing_records_date", "date"),
        Index("ix_billing_records_merchant_date", "merchant", "date"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    source: Mapped[str] = mapped_column(String(12), nullable=False)
    source_key: Mapped[str] = mapped_column(String(160), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    date: Mapped[str] = mapped_column(String(10), nullable=False)
    merchant: Mapped[str] = mapped_column(String(160), nullable=False)
    raw_merchant: Mapped[str] = mapped_column(Text, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    account_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    transaction_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_row_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
