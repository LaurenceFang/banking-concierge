"""Database models for the independent imported and demo billing ledger."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
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


class BillingAnnotation(BillingBase):
    """User corrections and labels overlay either ledger without rewriting bank facts."""

    __tablename__ = "billing_annotations"

    entry_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    date_override: Mapped[str | None] = mapped_column(String(10), nullable=True)
    merchant_override: Mapped[str | None] = mapped_column(String(160), nullable=True)
    amount_minor_override: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    kind_override: Mapped[str | None] = mapped_column(String(16), nullable=True)
    category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BillingCategoryRule(BillingBase):
    """An explicit, user-owned merchant classification preference."""

    __tablename__ = "billing_category_rules"
    __table_args__ = (
        UniqueConstraint("client_request_id", name="uq_billing_category_rule_request"),
        UniqueConstraint(
            "merchant_key",
            "kind_match_key",
            "note_condition_key",
            "effective_from",
            name="uq_billing_category_rule_condition",
        ),
        Index("ix_billing_category_rule_merchant", "merchant_key", "effective_from"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    client_request_id: Mapped[str] = mapped_column(String(160), nullable=False)
    request_payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    merchant: Mapped[str] = mapped_column(String(160), nullable=False)
    merchant_key: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    transaction_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    kind_match_key: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    note_contains: Mapped[str | None] = mapped_column(String(500), nullable=True)
    note_condition_key: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BillingManualRecord(BillingBase):
    """A user's bookkeeping entry, deliberately separate from bank cash and imports."""

    __tablename__ = "billing_manual_records"
    __table_args__ = (
        CheckConstraint(
            "transaction_kind IN ('expense', 'refund', 'income', 'transfer', 'opening')",
            name="ck_manual_billing_kind",
        ),
        CheckConstraint("currency = 'CNY'", name="ck_manual_billing_currency"),
        CheckConstraint("amount_minor != 0", name="ck_manual_billing_nonzero"),
        Index("ix_manual_billing_date", "date"),
        Index("ix_manual_billing_merchant_date", "merchant", "date"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    client_request_id: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    merchant: Mapped[str] = mapped_column(String(160), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    account_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    transaction_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BillingImportBatch(BillingBase):
    """One uploaded CSV and only the rows it actually added."""

    __tablename__ = "billing_import_batches"
    __table_args__ = (UniqueConstraint("client_request_id", name="uq_billing_batch_request"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    client_request_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    imported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BillingImportBatchEntry(BillingBase):
    """Tracks rows actually added by a batch, including rows later removed individually."""

    __tablename__ = "billing_import_batch_entries"
    __table_args__ = (Index("ix_billing_batch_entries_batch", "batch_id"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(80), nullable=False)
    entry_id: Mapped[str] = mapped_column(String(80), nullable=False)
    removed_individually: Mapped[bool] = mapped_column(nullable=False, default=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
