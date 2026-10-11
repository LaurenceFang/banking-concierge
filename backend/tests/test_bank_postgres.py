"""Integration checks for PostgreSQL locking and atomic ledger writes.

Set BANK_TEST_DATABASE_URL to the dedicated local concierge_test URL to run these.
The guard intentionally rejects every database except that isolated test target.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Barrier, Event
from time import monotonic, sleep

import pytest
from app.bank import BankError, BankService
from app.bank.models import Account, Base, LedgerTransaction, TransferPlan
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import make_url

TEST_DATABASE_ENV = "BANK_TEST_DATABASE_URL"


@pytest.fixture
def postgres_bank() -> Iterator[BankService]:
    raw_url = os.environ.get(TEST_DATABASE_ENV)
    if not raw_url:
        pytest.skip(f"set {TEST_DATABASE_ENV} to run isolated PostgreSQL integration tests")
    url = make_url(raw_url)
    if not (
        url.drivername == "postgresql+psycopg"
        and url.host == "127.0.0.1"
        and url.port == 55432
        and url.database == "concierge_test"
    ):
        pytest.fail(
            "Refusing to reset bank tables: PostgreSQL integration tests are restricted "
            "to 127.0.0.1:55432/concierge_test."
        )

    service = BankService(raw_url)
    try:
        # Only bank tables described by this package are reset, and the URL guard
        # above ensures they are in the dedicated test database.
        Base.metadata.drop_all(service.engine)
        Base.metadata.create_all(service.engine)
        service.seed()
        yield service
    finally:
        Base.metadata.drop_all(service.engine)
        service.dispose()


def _balance(service: BankService, account_id: str) -> int:
    return next(row["balance_minor"] for row in service.list_accounts() if row["id"] == account_id)


def _plan(service: BankService, amount: int, conversation: str) -> dict:
    return service.create_transfer_plan(
        "demo-checking-001", "demo-recipient-chen", amount, conversation_id=conversation
    )


def test_postgres_approval_commits_balanced_debit_and_credit(postgres_bank: BankService):
    plan = _plan(postgres_bank, 125_075, "pg-atomic")
    source_before = _balance(postgres_bank, "demo-checking-001")
    destination_before = 5_000_000

    result = postgres_bank.decide_plan(plan["id"], "approve")

    assert result["status"] == "completed"
    assert _balance(postgres_bank, "demo-checking-001") == source_before - 125_075
    with postgres_bank._session_factory() as session:
        destination = session.get(Account, "demo-destination-chen")
        assert destination.balance_minor == destination_before + 125_075
        rows = session.scalars(
            select(LedgerTransaction).where(
                LedgerTransaction.operation_id == result["operation_id"]
            )
        ).all()
        assert sorted(row.amount_minor for row in rows) == [-125_075, 125_075]
        assert sum(row.amount_minor for row in rows) == 0
        assert len(rows) == 2


def test_postgres_duplicate_approval_returns_saved_operation_once(postgres_bank: BankService):
    plan = _plan(postgres_bank, 77_777, "pg-idempotent")
    source_before = _balance(postgres_bank, "demo-checking-001")

    first = postgres_bank.decide_plan(plan["id"], "approve")
    retry = postgres_bank.decide_plan(plan["id"], "approve")

    assert retry == first
    assert _balance(postgres_bank, "demo-checking-001") == source_before - 77_777
    with postgres_bank._session_factory() as session:
        assert session.scalar(
            select(func.count(LedgerTransaction.id)).where(
                LedgerTransaction.operation_id == first["operation_id"]
            )
        ) == 2


def test_postgres_competing_approvals_serialize_on_account_rows(postgres_bank: BankService):
    source_before = _balance(postgres_bank, "demo-checking-001")
    amount = source_before // 2 + 1
    first = _plan(postgres_bank, amount, "pg-race-one")
    second = _plan(postgres_bank, amount, "pg-race-two")
    barrier = Barrier(2)

    def approve(plan_id: str) -> dict:
        barrier.wait(timeout=5)
        return postgres_bank.decide_plan(plan_id, "approve")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(approve, first["id"])
        second_future = pool.submit(approve, second["id"])
        outcomes = [first_future.result(timeout=10), second_future.result(timeout=10)]

    assert sorted(result["status"] for result in outcomes) == ["completed", "declined"]
    assert sum(result.get("code") == "INSUFFICIENT_BALANCE" for result in outcomes) == 1
    assert _balance(postgres_bank, "demo-checking-001") == source_before - amount
    with postgres_bank._session_factory() as session:
        operation_count = session.scalar(
            select(func.count(func.distinct(LedgerTransaction.operation_id))).where(
                LedgerTransaction.operation_id.is_not(None)
            )
        )
        assert operation_count == 1
        assert session.scalar(
            select(func.count(LedgerTransaction.id)).where(
                LedgerTransaction.operation_id.is_not(None)
            )
        ) == 2


def test_postgres_insufficient_balance_at_plan_creation_has_no_effect(postgres_bank: BankService):
    source_before = _balance(postgres_bank, "demo-checking-001")
    with postgres_bank._session_factory() as session:
        plan_count_before = session.scalar(select(func.count(TransferPlan.id)))
        transaction_count_before = session.scalar(select(func.count(LedgerTransaction.id)))

    with pytest.raises(BankError) as error:
        _plan(postgres_bank, source_before + 1, "pg-insufficient")

    assert error.value.code == "INSUFFICIENT_BALANCE"
    assert _balance(postgres_bank, "demo-checking-001") == source_before
    with postgres_bank._session_factory() as session:
        assert session.scalar(select(func.count(TransferPlan.id))) == plan_count_before
        assert session.scalar(select(func.count(LedgerTransaction.id))) == transaction_count_before


def test_postgres_row_locks_block_a_second_approval_until_balance_is_rechecked(
    postgres_bank: BankService,
):
    source_before = _balance(postgres_bank, "demo-checking-001")
    plan = _plan(postgres_bank, 8_500_000, "pg-explicit-row-lock")
    lock_conn = postgres_bank.engine.connect()
    lock_tx = lock_conn.begin()
    lock_conn.execute(
        text("SELECT id FROM bank_accounts WHERE id = :account_id FOR UPDATE"),
        {"account_id": "demo-checking-001"},
    )
    query_started: Queue[int] = Queue()
    worker_started = Event()

    def observe_locked_account_query(
        connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        normalized = statement.upper()
        if "BANK_ACCOUNTS" in normalized and "FOR UPDATE" in normalized:
            query_started.put(connection.connection.driver_connection.info.backend_pid)

    event.listen(postgres_bank.engine, "before_cursor_execute", observe_locked_account_query)

    def approve() -> dict:
        worker_started.set()
        return postgres_bank.decide_plan(plan["id"], "approve")

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(approve)
    try:
        assert worker_started.wait(timeout=3)
        worker_pid = query_started.get(timeout=5)
        deadline = monotonic() + 5
        blocked = False
        while monotonic() < deadline:
            blockers = lock_conn.execute(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": worker_pid}
            ).scalar_one()
            if blockers:
                blocked = True
                break
            sleep(0.02)
        assert blocked, "approval query did not wait on the locked source account row"
        lock_tx.commit()
    finally:
        event.remove(postgres_bank.engine, "before_cursor_execute", observe_locked_account_query)
        if lock_tx.is_active:
            lock_tx.rollback()
        lock_conn.close()
        pool.shutdown(wait=True)

    result = future.result(timeout=5)
    assert result["status"] == "completed"
    assert _balance(postgres_bank, "demo-checking-001") == source_before - 8_500_000
