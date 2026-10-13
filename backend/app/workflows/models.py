"""Persistence models for the fictional cross-merchant birthday workflow.

These tables intentionally use their own metadata and store bank identifiers as
plain values. The bank ledger and shared reservation tables remain owned by the
bank package; all coordinated writes still use the same database transaction.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class WorkflowBase(DeclarativeBase):
    pass


class WorkflowCatalogItem(WorkflowBase):
    __tablename__ = "workflow_catalog_items"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(String(80), nullable=False)
    merchant_name: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    cancel_supported: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class WorkflowPlan(WorkflowBase):
    __tablename__ = "workflow_plans"
    __table_args__ = (
        CheckConstraint("action IN ('arrange', 'cancel')", name="ck_workflow_plan_action"),
        Index("ix_workflow_plans_conversation_status", "conversation_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    recipient_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    budget_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    items_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    execute_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    conversation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    outcome_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    outcome_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    outcome_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkflowTask(WorkflowBase):
    __tablename__ = "workflow_tasks"
    __table_args__ = (
        CheckConstraint("budget_minor > 0", name="ck_workflow_task_budget_positive"),
        CheckConstraint("total_minor > 0", name="ck_workflow_task_total_positive"),
        CheckConstraint("paid_minor >= refunded_minor", name="ck_workflow_task_paid_refund_order"),
        Index("ix_workflow_tasks_due", "status", "execute_at"),
    )

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    account_id: Mapped[str] = mapped_column(String(80), nullable=False)
    recipient_label: Mapped[str] = mapped_column(String(120), nullable=False)
    budget_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    paid_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    refunded_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    refund_pending_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    net_spent_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="scheduled")
    execute_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    hold_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fault_response_lost_done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    related_plan_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkflowStep(WorkflowBase):
    __tablename__ = "workflow_steps"
    __table_args__ = (
        UniqueConstraint("task_id", "step_index", name="uq_workflow_step_task_index"),
        CheckConstraint("step_index >= 0", name="ck_workflow_step_index_nonnegative"),
        CheckConstraint("quantity BETWEEN 1 AND 5", name="ck_workflow_step_quantity"),
        CheckConstraint("total_minor > 0", name="ck_workflow_step_total_positive"),
        Index("ix_workflow_steps_task_order", "task_id", "step_index"),
    )

    id: Mapped[str] = mapped_column(String(140), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(100), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[str] = mapped_column(String(80), nullable=False)
    merchant_id: Mapped[str] = mapped_column(String(80), nullable=False)
    merchant_name: Mapped[str] = mapped_column(String(120), nullable=False)
    product_name: Mapped[str] = mapped_column(String(160), nullable=False)
    catalog_version: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cancel_supported: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    merchant_account_id: Mapped[str] = mapped_column(String(80), nullable=False)
    order_id: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="待执行")
    refund_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkflowMerchantOrder(WorkflowBase):
    __tablename__ = "workflow_merchant_orders"
    __table_args__ = (
        UniqueConstraint("task_id", "step_index", name="uq_workflow_order_business_key"),
        CheckConstraint(
            "status IN ('PENDING', 'CONFIRMED', 'CANCELLED', 'FULFILLED', 'FAILED', 'REFUND_PENDING', 'REFUNDED')",
            name="ck_workflow_order_status",
        ),
        CheckConstraint("amount_minor > 0", name="ck_workflow_order_amount_positive"),
        CheckConstraint("refunded_minor >= 0", name="ck_workflow_order_refunded_nonnegative"),
        CheckConstraint("refund_pending_minor >= 0", name="ck_workflow_order_refund_pending_nonnegative"),
        Index("ix_workflow_orders_task", "task_id"),
    )

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    business_key: Mapped[str] = mapped_column(String(220), nullable=False, unique=True)
    task_id: Mapped[str] = mapped_column(String(100), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[str] = mapped_column(String(80), nullable=False)
    merchant_id: Mapped[str] = mapped_column(String(80), nullable=False)
    merchant_name: Mapped[str] = mapped_column(String(120), nullable=False)
    account_id: Mapped[str] = mapped_column(String(80), nullable=False)
    merchant_account_id: Mapped[str] = mapped_column(String(80), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="confirmed")
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    refund_operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    refunded_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    refund_pending_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    receipt_lost: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkflowEvent(WorkflowBase):
    __tablename__ = "workflow_events"
    __table_args__ = (
        Index("ix_workflow_events_task_version", "task_id", "state_version"),
        Index("ix_workflow_events_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(140), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(100), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="workflow")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="workflow")
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(100), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    receipt_json: Mapped[str] = mapped_column(Text, nullable=False)
