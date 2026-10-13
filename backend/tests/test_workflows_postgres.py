"""PostgreSQL row-locking and atomic-write tests for simulated workflows.

These tests only use 127.0.0.1:55432/concierge_test with the local demo-only
credential. Cleanup is limited to task IDs created by this fixture, their holds
and ledger operations, and merchant accounts created by this fixture. Bank tables
are never dropped or truncated.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from app.bank.models import Account, FundHold, LedgerTransaction, User
from app.bank.reservations import available_balance, lock_accounts, reserved_balance
from app.workflows import WorkflowService
from app.workflows.models import (
    WorkflowCatalogItem,
    WorkflowEvent,
    WorkflowMerchantOrder,
    WorkflowPlan,
    WorkflowStep,
    WorkflowTask,
)
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url

TEST_DATABASE_ENV = "BANK_TEST_DATABASE_URL"
DEMO_USER_ID = "demo-user-001"
SOURCE_ACCOUNT_ID = "demo-checking-001"
MERCHANT_ACCOUNT_IDS = {
    "workflow-merchant-flowers-account",
    "workflow-merchant-cake-account",
}
OWNED_ACCOUNT_IDS = MERCHANT_ACCOUNT_IDS | {SOURCE_ACCOUNT_ID}
USER_IDS = {
    DEMO_USER_ID,
    "workflow-merchant-flowers-owner",
    "workflow-merchant-cake-owner",
}
PRODUCT_IDS = {"flowers-basic", "cake-basic"}
ITEMS = [
    {"product_id": "flowers-basic", "quantity": 1},
    {"product_id": "cake-basic", "quantity": 1},
]
QUOTE_MINOR = 32_800


@pytest.fixture
def postgres_workflows() -> Iterator[tuple[WorkflowService, list[str]]]:
    raw_url = os.environ.get(TEST_DATABASE_ENV)
    if not raw_url:
        pytest.skip(f"set {TEST_DATABASE_ENV} to run isolated PostgreSQL workflow tests")
    url = make_url(raw_url)
    if not (
        url.drivername == "postgresql+psycopg"
        and url.host == "127.0.0.1"
        and url.port == 55432
        and url.database == "concierge_test"
        and url.password == "local-demo-only"
    ):
        pytest.fail(
            "Refusing PostgreSQL workflow test outside 127.0.0.1:55432/concierge_test "
            "with the local demo-only credential."
        )

    service = WorkflowService(raw_url)
    task_ids: list[str] = []
    with service._session_factory() as session:
        original_users = {
            row.id: row.name for row in session.scalars(select(User).where(User.id.in_(USER_IDS))).all()
        }
        original_accounts = {
            row.id: {
                "owner_id": row.owner_id,
                "name": row.name,
                "kind": row.kind,
                "balance_minor": row.balance_minor,
                "currency": row.currency,
            }
            for row in session.scalars(select(Account).where(Account.id.in_(OWNED_ACCOUNT_IDS))).all()
        }
        original_catalog_ids = set(
            session.scalars(
                select(WorkflowCatalogItem.id).where(WorkflowCatalogItem.id.in_(PRODUCT_IDS))
            ).all()
        )
        original_hold_ids = set(session.scalars(select(FundHold.id)).all())
        original_transaction_ids = set(session.scalars(select(LedgerTransaction.id)).all())

    try:
        with service._transaction() as session:
            if session.get(User, DEMO_USER_ID) is None:
                session.add(User(id=DEMO_USER_ID, name="林小满"))
                session.flush()
            source = session.get(Account, SOURCE_ACCOUNT_ID)
            if source is None:
                session.add(
                    Account(
                        id=SOURCE_ACCOUNT_ID,
                        owner_id=DEMO_USER_ID,
                        name="跨场景PG测试付款账户",
                        kind="checking",
                        balance_minor=1_000_000,
                        currency="CNY",
                    )
                )
            elif source.owner_id != DEMO_USER_ID or source.currency != "CNY":
                pytest.fail("demo-checking-001 is not a CNY account owned by demo-user-001")

        service.seed_demo()
        with service._transaction() as session:
            source = lock_accounts(session, SOURCE_ACCOUNT_ID)[SOURCE_ACCOUNT_ID]
            held = reserved_balance(session, SOURCE_ACCOUNT_ID)
            if source.balance_minor < held:
                pytest.fail("demo-checking-001 balance is below its existing active holds")
            # Give this fixture enough available cash while preserving every
            # pre-existing hold. The original row is restored in finally.
            source.balance_minor = max(source.balance_minor, held + 1_000_000)
            session.flush()
        yield service, task_ids
    finally:
        try:
            _cleanup_fixture(
                service,
                task_ids,
                original_users=original_users,
                original_accounts=original_accounts,
                original_catalog_ids=original_catalog_ids,
                original_hold_ids=original_hold_ids,
                original_transaction_ids=original_transaction_ids,
            )
        finally:
            service.close()


def _cleanup_fixture(
    service: WorkflowService,
    task_ids: list[str],
    *,
    original_users: dict[str, str],
    original_accounts: dict[str, dict[str, object]],
    original_catalog_ids: set[str],
    original_hold_ids: set[str],
    original_transaction_ids: set[str],
) -> None:
    task_id_set = set(task_ids)
    operation_ids = _operation_ids(task_id_set)
    with service._transaction() as session:
        tasks = session.scalars(select(WorkflowTask).where(WorkflowTask.id.in_(task_id_set or {""}))).all()
        plan_ids = {task.plan_id for task in tasks}
        hold_ids = {task.hold_id for task in tasks if task.hold_id and task.hold_id not in original_hold_ids}
        plans = session.scalars(
            select(WorkflowPlan).where(
                (WorkflowPlan.task_id.in_(task_id_set or {""})) | (WorkflowPlan.id.in_(plan_ids or {""}))
            )
        ).all()
        plan_ids.update(plan.id for plan in plans)

        session.execute(delete(WorkflowEvent).where(WorkflowEvent.task_id.in_(task_id_set or {""})))
        session.execute(
            delete(WorkflowMerchantOrder).where(WorkflowMerchantOrder.task_id.in_(task_id_set or {""}))
        )
        session.execute(delete(WorkflowStep).where(WorkflowStep.task_id.in_(task_id_set or {""})))
        session.execute(delete(WorkflowTask).where(WorkflowTask.id.in_(task_id_set or {""})))
        session.execute(
            delete(WorkflowPlan).where(
                (WorkflowPlan.task_id.in_(task_id_set or {""})) | (WorkflowPlan.id.in_(plan_ids or {""}))
            )
        )

        if hold_ids:
            session.execute(delete(FundHold).where(FundHold.id.in_(hold_ids)))
        if operation_ids:
            session.execute(
                delete(LedgerTransaction).where(
                    LedgerTransaction.id.not_in(original_transaction_ids or {""}),
                    LedgerTransaction.operation_id.in_(operation_ids),
                    LedgerTransaction.account_id.in_(OWNED_ACCOUNT_IDS),
                )
            )

        for account_id in OWNED_ACCOUNT_IDS:
            account = session.get(Account, account_id)
            snapshot = original_accounts.get(account_id)
            if snapshot is None:
                if account is not None:
                    session.delete(account)
            elif account is not None:
                for field, value in snapshot.items():
                    setattr(account, field, value)

        for product_id in PRODUCT_IDS - original_catalog_ids:
            session.delete(session.get(WorkflowCatalogItem, product_id))

        for user_id in USER_IDS:
            user = session.get(User, user_id)
            original_name = original_users.get(user_id)
            if user is None:
                continue
            if original_name is None:
                session.delete(user)
            else:
                user.name = original_name


def _new_task_id(task_ids: list[str]) -> str:
    task_id = f"pgw-{uuid4().hex}"
    task_ids.append(task_id)
    return task_id


def _plan(
    service: WorkflowService,
    task_ids: list[str],
    conversation: str,
    *,
    execute_at,
    items: list[dict[str, object]] | None = None,
):
    return service.create_action_plan(
        "arrange",
        conversation,
        account_id=SOURCE_ACCOUNT_ID,
        recipient_label="PG模拟生日收件人",
        budget_minor=40_000,
        items=items or ITEMS,
        execute_at=execute_at,
        task_id=_new_task_id(task_ids),
    )


def _balance(service: WorkflowService, account_id: str) -> int:
    with service._session_factory() as session:
        account = session.get(Account, account_id)
        assert account is not None
        return account.balance_minor


def _operation_ids(task_ids: set[str]) -> set[str]:
    operations: set[str] = set()
    for task_id in task_ids:
        digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:40]
        for index in range(2):
            operations.add(f"wf-pay-{digest}-{index}")
            operations.add(f"wf-refund-{digest}-{index}")
    return operations


def _assert_no_unrelated_due_tasks(service: WorkflowService, task_ids: list[str], as_of) -> None:
    with service._session_factory() as session:
        due_ids = session.scalars(
            select(WorkflowTask.id).where(
                WorkflowTask.id.not_in(set(task_ids) or {""}),
                WorkflowTask.status.in_(["scheduled", "running", "unknown"]),
                WorkflowTask.execute_at <= as_of,
            )
        ).all()
    if due_ids:
        pytest.skip("isolated PG test DB contains another due workflow; avoiding unrelated task execution")


def test_postgres_two_workers_pay_each_step_of_one_task_once(postgres_workflows):
    service, task_ids = postgres_workflows
    execute_at = service.now() + timedelta(minutes=1)
    plan = _plan(service, task_ids, "pgw-two-workers", execute_at=execute_at)
    source_before = _balance(service, SOURCE_ACCOUNT_ID)
    flower_before = _balance(service, "workflow-merchant-flowers-account")
    cake_before = _balance(service, "workflow-merchant-cake-account")
    accepted = service.decide_plan(plan["id"], "approve")
    assert accepted["status"] == "completed"
    assert accepted["task_status"] == "scheduled"

    as_of = execute_at + timedelta(seconds=1)
    _assert_no_unrelated_due_tasks(service, task_ids, as_of)
    worker = WorkflowService(service.database_url)
    try:
        barrier = Barrier(2)

        def process(worker_service: WorkflowService) -> list[dict]:
            barrier.wait(timeout=5)
            return worker_service.process_due(as_of=as_of)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(process, (service, worker)))
    finally:
        worker.close()

    assert any(row["id"] == plan["task_id"] for batch in results for row in batch)
    task = service.get_task(plan["task_id"])
    assert task["status"] == "completed"
    assert task["paid_minor"] == QUOTE_MINOR
    assert task["reserved_minor"] == 0
    assert _balance(service, SOURCE_ACCOUNT_ID) == source_before - QUOTE_MINOR
    assert _balance(service, "workflow-merchant-flowers-account") == flower_before + 19_900
    assert _balance(service, "workflow-merchant-cake-account") == cake_before + 12_900
    with service._session_factory() as session:
        orders = session.scalars(
            select(WorkflowMerchantOrder).where(WorkflowMerchantOrder.task_id == task["id"])
        ).all()
        ledger = session.scalars(
            select(LedgerTransaction).where(LedgerTransaction.operation_id.in_(_operation_ids({task["id"]})))
        ).all()
    assert len(orders) == 2
    assert len({order.business_key for order in orders}) == 2
    assert {order.status for order in orders} == {"CONFIRMED"}
    assert len(ledger) == 4
    assert sum(row.amount_minor for row in ledger) == 0


def test_postgres_competing_arrangements_reserve_only_available_cash(postgres_workflows):
    service, task_ids = postgres_workflows
    with service._transaction() as session:
        source = lock_accounts(session, SOURCE_ACCOUNT_ID)[SOURCE_ACCOUNT_ID]
        held_before = reserved_balance(session, SOURCE_ACCOUNT_ID)
        source.balance_minor = held_before + QUOTE_MINOR
        session.flush()
    source_before = _balance(service, SOURCE_ACCOUNT_ID)
    execute_at = service.now() + timedelta(hours=1)
    first = _plan(service, task_ids, "pgw-reserve-one", execute_at=execute_at)
    second = _plan(service, task_ids, "pgw-reserve-two", execute_at=execute_at)
    barrier = Barrier(2)

    def approve(plan_id: str) -> dict:
        barrier.wait(timeout=5)
        return service.decide_plan(plan_id, "approve")

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(approve, (first["id"], second["id"])))

    assert sorted(result["status"] for result in outcomes) == ["completed", "declined"]
    assert _balance(service, SOURCE_ACCOUNT_ID) == source_before
    with service._session_factory() as session:
        source = session.get(Account, SOURCE_ACCOUNT_ID)
        assert reserved_balance(session, SOURCE_ACCOUNT_ID) == held_before + QUOTE_MINOR
        assert available_balance(session, source) == 0
        own_tasks = session.scalars(
            select(WorkflowTask).where(WorkflowTask.id.in_({first["task_id"], second["task_id"]}))
        ).all()
        assert len(own_tasks) == 1
        task = own_tasks[0]
        hold = session.get(FundHold, task.hold_id)
        assert hold is not None and hold.status == "active"
        assert hold.remaining_minor == QUOTE_MINOR
        assert (
            session.scalar(
                select(WorkflowMerchantOrder.id).where(WorkflowMerchantOrder.task_id == task.id).limit(1)
            )
            is None
        )
        assert (
            session.scalar(
                select(LedgerTransaction.id)
                .where(
                    LedgerTransaction.operation_id.in_(_operation_ids({first["task_id"], second["task_id"]}))
                )
                .limit(1)
            )
            is None
        )


def test_postgres_payment_order_and_ledger_roll_back_together(postgres_workflows, monkeypatch):
    service, task_ids = postgres_workflows
    execute_at = service.now() + timedelta(minutes=1)
    plan = _plan(
        service,
        task_ids,
        "pgw-consume-rollback",
        execute_at=execute_at,
        items=[{"product_id": "flowers-basic", "quantity": 1}],
    )
    accepted = service.decide_plan(plan["id"], "approve")
    assert accepted["task_status"] == "scheduled"
    source_before = _balance(service, SOURCE_ACCOUNT_ID)
    merchant_before = _balance(service, "workflow-merchant-flowers-account")
    _assert_no_unrelated_due_tasks(service, task_ids, execute_at + timedelta(seconds=1))
    original_transition = service._transition

    def interrupt_after_consume(session, task, status, message, title, operation_id=None):
        if operation_id and operation_id.startswith("wf-pay-"):
            raise RuntimeError("forced PostgreSQL post-consume interruption")
        return original_transition(session, task, status, message, title, operation_id)

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(service, "_transition", interrupt_after_consume)
        with pytest.raises(RuntimeError, match="forced PostgreSQL post-consume interruption"):
            service.process_due(as_of=execute_at + timedelta(seconds=1))

    task = service.get_task(plan["task_id"])
    assert task["status"] == "scheduled"
    assert task["paid_minor"] == 0
    assert task["reserved_minor"] == 19_900
    assert _balance(service, SOURCE_ACCOUNT_ID) == source_before
    assert _balance(service, "workflow-merchant-flowers-account") == merchant_before
    with service._session_factory() as session:
        hold = session.get(FundHold, task["hold_id"])
        assert hold is not None and hold.status == "active" and hold.remaining_minor == 19_900
        assert (
            session.scalar(
                select(WorkflowMerchantOrder.id).where(WorkflowMerchantOrder.task_id == task["id"]).limit(1)
            )
            is None
        )
        assert (
            session.scalar(
                select(LedgerTransaction.id)
                .where(LedgerTransaction.operation_id.in_(_operation_ids({task["id"]})))
                .limit(1)
            )
            is None
        )

    result = service.process_due(as_of=execute_at + timedelta(seconds=1))
    assert service.get_task(task["id"])["status"] == "completed"
    assert result


def test_postgres_refund_and_ledger_roll_back_together_then_recover(
    postgres_workflows, monkeypatch: pytest.MonkeyPatch
):
    service, task_ids = postgres_workflows
    service.fault_mode = "cake_out_of_stock"
    execute_at = service.now() + timedelta(minutes=1)
    plan = _plan(service, task_ids, "pgw-refund-rollback", execute_at=execute_at)
    service.decide_plan(plan["id"], "approve")
    source_before = _balance(service, SOURCE_ACCOUNT_ID)
    flower_before = _balance(service, "workflow-merchant-flowers-account")
    _assert_no_unrelated_due_tasks(service, task_ids, execute_at + timedelta(seconds=1))

    from app.bank import reservations

    original_transfer = reservations.transfer_funds

    def interrupt_after_refund(*args, **kwargs):
        operation = original_transfer(*args, **kwargs)
        description = args[5] if len(args) > 5 else kwargs["description"]
        if description.startswith("模拟商户退款·"):
            raise RuntimeError("forced PostgreSQL post-refund interruption")
        return operation

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(reservations, "transfer_funds", interrupt_after_refund)
        with pytest.raises(RuntimeError, match="forced PostgreSQL post-refund interruption"):
            service.process_due(as_of=execute_at + timedelta(seconds=1))

    task = service.get_task(plan["task_id"])
    assert task["paid_minor"] == 19_900
    assert task["refunded_minor"] == 0
    assert task["refund_pending_minor"] == 0
    assert _balance(service, SOURCE_ACCOUNT_ID) == source_before - 19_900
    assert _balance(service, "workflow-merchant-flowers-account") == flower_before + 19_900
    with service._session_factory() as session:
        order = session.scalar(
            select(WorkflowMerchantOrder).where(
                WorkflowMerchantOrder.task_id == task["id"],
                WorkflowMerchantOrder.product_id == "flowers-basic",
            )
        )
        assert order is not None and order.status == "CONFIRMED"
        assert order.refunded_minor == 0 and order.refund_operation_id is None
        rows = session.scalars(
            select(LedgerTransaction).where(LedgerTransaction.operation_id.in_(_operation_ids({task["id"]})))
        ).all()
    assert len(rows) == 2
    assert {row.description for row in rows} == {"模拟商户消费·花间花坊"}

    recovered = service.recover(task["id"])
    assert recovered["status"] == "failed"
    assert recovered["refunded_minor"] == 19_900
    assert recovered["refund_pending_minor"] == 0
    assert recovered["net_spent_minor"] == 0
    assert _balance(service, SOURCE_ACCOUNT_ID) == source_before
    assert _balance(service, "workflow-merchant-flowers-account") == flower_before
