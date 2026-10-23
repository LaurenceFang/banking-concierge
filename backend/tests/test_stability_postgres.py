"""Focused PostgreSQL persistence and concurrency regressions for SPEC30."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from app.bank.errors import BankError
from app.bank.models import Account, User
from app.bank.service import BankService
from app.billing.service import BillingService
from app.workflows.models import WorkflowMerchant, WorkflowPlan
from app.workflows.service import WorkflowService
from sqlalchemy import create_engine, delete, inspect, select, text
from sqlalchemy.engine import make_url

TEST_DATABASE_ENV = "BANK_TEST_DATABASE_URL"


@pytest.fixture
def pg_url() -> str:
    raw = os.environ.get(TEST_DATABASE_ENV)
    if not raw:
        pytest.skip(f"set {TEST_DATABASE_ENV} to run isolated PostgreSQL stability tests")
    url = make_url(raw)
    if not (
        url.drivername == "postgresql+psycopg"
        and url.host == "127.0.0.1"
        and url.port == 55432
        and url.database == "concierge_test"
        and url.password == "local-demo-only"
    ):
        pytest.fail("Refusing stability test outside the dedicated local concierge_test database")
    return raw


def _seed_bank_demo(pg_url: str) -> None:
    bank = BankService(pg_url)
    try:
        bank.seed()
    finally:
        bank.dispose()


def test_postgres_category_rule_table_crud_and_restart(pg_url: str) -> None:
    client_request_id = f"pg-stability-rule-{uuid4().hex}"
    service = BillingService(pg_url)
    rule_id = None
    try:
        rule = service.create_category_rule(
            client_request_id=client_request_id,
            merchant=f"PG验证商户{uuid4().hex[:8]}",
            category="工作餐",
            effective_from="2026-10-24",
            transaction_kind="expense",
        )
        rule_id = rule["id"]
        assert service.update_category_rule(rule_id, 1, category="软件服务")["version"] == 2
    finally:
        service.close()

    restarted = BillingService(pg_url)
    try:
        assert any(row["id"] == rule_id and row["category"] == "软件服务" for row in restarted.list_category_rules())
        assert restarted.delete_category_rule(rule_id, 2)["deleted"] is True
    finally:
        restarted.close()


def test_postgres_immediate_plan_revision_persists_after_restart(pg_url: str) -> None:
    conversation_id = f"pg-immediate-{uuid4().hex}"
    task_id = f"pg-immediate-task-{uuid4().hex}"
    service = WorkflowService(pg_url)
    plan_ids: set[str] = set()
    try:
        _seed_bank_demo(pg_url)
        service.seed_demo()
        original = service.create_action_plan(
            "arrange",
            conversation_id,
            account_id="demo-checking-001",
            recipient_label="PG验收对象",
            budget_minor=50_000,
            items=[{"product_id": "flowers-basic", "quantity": 1}],
            task_id=task_id,
        )
        plan_ids.add(original["id"])
        assert original["execute_immediately"] is True
        revised = service.revise_action_plan(
            original["id"],
            conversation_id,
            {"budget_minor": 30_000, "items": [{"product_id": "cake-basic", "quantity": 1}]},
        )
        plan_ids.add(revised["id"])
        assert revised["execute_immediately"] is True
        assert {change["field"] for change in revised["revision"]["changes"]} == {"budget_minor", "items"}
    finally:
        service.close()

    restarted = WorkflowService(pg_url)
    try:
        restored = restarted.get_plan(revised["id"])
        assert restored["execute_immediately"] is True
        assert restored["revision"] == revised["revision"]
    finally:
        with restarted._transaction() as session:
            session.execute(delete(WorkflowPlan).where(WorkflowPlan.id.in_(plan_ids)))
            session.execute(delete(WorkflowPlan).where(WorkflowPlan.task_id == task_id))
        restarted.close()


def test_postgres_legacy_workflow_schema_adds_immediate_flag_and_restarts(pg_url: str) -> None:
    """Exercise the real additive migration while restoring test DB baseline."""
    _seed_bank_demo(pg_url)
    service = WorkflowService(pg_url)
    conversation_id = f"pg-legacy-{uuid4().hex}"
    plan_id = None
    try:
        service.seed_demo()
        scheduled = service.create_action_plan(
            "arrange",
            conversation_id,
            account_id="demo-checking-001",
            recipient_label="PG旧表迁移",
            budget_minor=50_000,
            items=[{"product_id": "flowers-basic", "quantity": 1}],
            execute_at=service.now() + timedelta(hours=1),
            task_id=f"pg-legacy-task-{uuid4().hex}",
        )
        plan_id = scheduled["id"]
    finally:
        service.close()

    engine = create_engine(pg_url)
    before: dict[str, bool] = {}
    try:
        with engine.begin() as connection:
            before = dict(connection.execute(text("SELECT id, execute_immediately FROM workflow_plans")).all())
            connection.exec_driver_sql("ALTER TABLE workflow_plans DROP COLUMN execute_immediately")

        migrated = WorkflowService(pg_url)
        try:
            assert migrated.get_plan(plan_id)["execute_immediately"] is False
        finally:
            migrated.close()

        restarted = WorkflowService(pg_url)
        try:
            assert restarted.get_plan(plan_id)["execute_immediately"] is False
        finally:
            with restarted._transaction() as session:
                session.execute(delete(WorkflowPlan).where(WorkflowPlan.id == plan_id))
            restarted.close()
    finally:
        # Keep this shared dedicated test DB's pre-test plan values intact.
        with engine.begin() as connection:
            columns = {row["name"] for row in inspect(connection).get_columns("workflow_plans")}
            if "execute_immediately" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE workflow_plans ADD COLUMN execute_immediately BOOLEAN NOT NULL DEFAULT FALSE"
                )
            if plan_id:
                connection.execute(
                    text("DELETE FROM workflow_plans WHERE id = :identity"), {"identity": plan_id}
                )
            for identity, value in before.items():
                connection.execute(
                    text("UPDATE workflow_plans SET execute_immediately = :value WHERE id = :identity"),
                    {"value": value, "identity": identity},
                )
        engine.dispose()


def test_postgres_concurrent_revision_of_same_plan_has_one_winner(pg_url: str) -> None:
    conversation_id = f"pg-race-{uuid4().hex}"
    service = WorkflowService(pg_url)
    plan_ids: set[str] = set()
    try:
        _seed_bank_demo(pg_url)
        service.seed_demo()
        source = service.create_action_plan(
            "arrange",
            conversation_id,
            account_id="demo-checking-001",
            recipient_label="PG并发验收对象",
            budget_minor=50_000,
            items=[{"product_id": "flowers-basic", "quantity": 1}],
            execute_at=service.now() + timedelta(hours=1),
            task_id=f"pg-race-task-{uuid4().hex}",
        )
        plan_ids.add(source["id"])
        barrier = Barrier(2)

        def revise(recipient: str):
            barrier.wait(timeout=5)
            try:
                return service.revise_action_plan(
                    source["id"], conversation_id, {"recipient_label": recipient}
                )
            except BankError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(revise, ("并发甲", "并发乙")))

        winners = [result for result in results if isinstance(result, dict)]
        rejected = [result for result in results if isinstance(result, BankError)]
        assert len(winners) == len(rejected) == 1
        assert rejected[0].code == "PLAN_INVALIDATED"
        plan_ids.add(winners[0]["id"])
        with service._session_factory() as session:
            source_after = session.get(WorkflowPlan, source["id"])
            assert source_after is not None and source_after.status == "invalidated"
            assert session.scalar(
                select(WorkflowPlan.id).where(
                    WorkflowPlan.conversation_id == conversation_id,
                    WorkflowPlan.status == "pending",
                )
            ) == winners[0]["id"]
    finally:
        with service._transaction() as session:
            session.execute(delete(WorkflowPlan).where(WorkflowPlan.id.in_(plan_ids)))
            session.execute(delete(WorkflowPlan).where(WorkflowPlan.conversation_id == conversation_id))
        service.close()


def test_postgres_seed_repairs_missing_merchant_account_and_preserves_balance(pg_url: str) -> None:
    service = WorkflowService(pg_url)
    suffix = uuid4().hex
    merchant_id = f"pg-seed-merchant-{suffix}"
    owner_id = f"pg-seed-owner-{suffix}"
    account_id = f"pg-seed-account-{suffix}"
    merchant_name = f"PG种子回归{suffix[:8]}"
    try:
        with service._transaction() as session:
            session.add(User(id=owner_id, name=f"{merchant_name}（模拟）"))
            session.flush()
            session.add(WorkflowMerchant(id=merchant_id, name=merchant_name, owner_id=owner_id, account_id=account_id))
            session.add(Account(id=account_id, owner_id=owner_id, name="余额应保留", kind="workflow_merchant", balance_minor=123456, currency="CNY"))

        with service._transaction() as session:
            service._ensure_merchant(session, merchant_name, fixed={"id": merchant_id, "owner_id": owner_id, "account_id": account_id})
            assert session.get(Account, account_id).balance_minor == 123456
            session.delete(session.get(Account, account_id))

        with service._transaction() as session:
            service._ensure_merchant(session, merchant_name, fixed={"id": merchant_id, "owner_id": owner_id, "account_id": account_id})
            repaired = session.get(Account, account_id)
            assert repaired is not None and repaired.balance_minor == 0
    finally:
        with service._transaction() as session:
            session.execute(delete(Account).where(Account.id == account_id))
            session.execute(delete(WorkflowMerchant).where(WorkflowMerchant.id == merchant_id))
            session.execute(delete(User).where(User.id == owner_id))
        service.close()
