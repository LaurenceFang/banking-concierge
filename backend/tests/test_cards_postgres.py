"""Card concurrency checks against the dedicated local concierge_test database."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Barrier, Event
from time import monotonic, sleep

import pytest
from app.cards import CardService
from app.cards.models import Card, CardActionPlan, CardApplication
from sqlalchemy import delete, event, text
from sqlalchemy.engine import make_url


@pytest.fixture
def postgres_cards():
    database_url = os.environ.get("CARDS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set CARDS_TEST_DATABASE_URL to run isolated PostgreSQL lock tests.")
    parsed = make_url(database_url)
    if not (
        parsed.get_backend_name() == "postgresql"
        and parsed.host == "127.0.0.1"
        and parsed.port == 55432
        and parsed.database == "concierge_test"
    ):
        pytest.fail("PostgreSQL card tests are restricted to 127.0.0.1:55432/concierge_test.")

    first = CardService(database_url)
    _reset_card_rows(first)
    first.seed_demo()
    second = CardService(database_url)
    try:
        yield first, second
    finally:
        second.close()
        _reset_card_rows(first)
        first.close()


def _reset_card_rows(service: CardService) -> None:
    """Clear only the card package tables in the guarded test database."""

    with service.engine.begin() as connection:
        connection.execute(delete(CardApplication))
        connection.execute(delete(CardActionPlan))
        connection.execute(delete(Card))


def _run_together(calls):
    barrier = Barrier(len(calls))

    def run(service: CardService, plan_id: str) -> dict:
        barrier.wait(timeout=10)
        return service.decide_plan(plan_id, "approve")

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(run, service, plan_id) for service, plan_id in calls]
        return [future.result(timeout=20) for future in futures]


def test_postgres_same_plan_confirmation_is_serialized_and_idempotent(postgres_cards):
    first, second = postgres_cards
    plan = first.create_action_plan("freeze", "pg-idempotent", card_id="demo-card-debit-001")

    results = _run_together([(first, plan["id"]), (second, plan["id"])])

    assert results[0] == results[1]
    assert results[0]["status"] == "completed"
    assert results[0]["operation_id"]
    assert first.decide_plan(plan["id"], "approve") == results[0]
    assert len(first.completed_events()) == 1


def test_postgres_competing_card_versions_cannot_both_commit(postgres_cards):
    first, second = postgres_cards
    quota = first.create_action_plan(
        "set_daily_limit",
        "pg-quota",
        card_id="demo-card-debit-001",
        amount_minor=450_000,
    )
    freeze = first.create_action_plan("freeze", "pg-freeze", card_id="demo-card-debit-001")

    results = _run_together([(first, quota["id"]), (second, freeze["id"])])

    assert sorted(result["status"] for result in results) == ["completed", "declined"]
    assert sum("operation_id" in result for result in results) == 1
    declined = next(result for result in results if result["status"] == "declined")
    assert declined["code"] == "CARD_STATE_CHANGED"
    assert next(row for row in first.list_cards() if row["id"] == "demo-card-debit-001")["version"] == 2
    assert len(first.completed_events()) == 1


def test_postgres_confirmation_waits_for_card_row_lock(postgres_cards):
    service, _second = postgres_cards
    plan = service.create_action_plan("freeze", "pg-explicit-lock", card_id="demo-card-debit-001")
    lock_connection = service.engine.connect()
    lock_transaction = lock_connection.begin()
    lock_connection.execute(
        text("SELECT id FROM card_records WHERE id = :card_id FOR UPDATE"),
        {"card_id": "demo-card-debit-001"},
    )
    query_started: Queue[int] = Queue()
    worker_started = Event()

    def observe_locked_card_query(
        connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        normalized = statement.upper()
        if "CARD_RECORDS" in normalized and "FOR UPDATE" in normalized:
            driver_connection = connection.connection.driver_connection
            query_started.put(driver_connection.info.backend_pid)

    event.listen(service.engine, "before_cursor_execute", observe_locked_card_query)

    def approve() -> dict:
        worker_started.set()
        return service.decide_plan(plan["id"], "approve")

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(approve)
    try:
        assert worker_started.wait(timeout=3)
        worker_pid = query_started.get(timeout=5)
        deadline = monotonic() + 5
        blocked = False
        while monotonic() < deadline:
            blockers = lock_connection.execute(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": worker_pid}
            ).scalar_one()
            if blockers:
                blocked = True
                break
            sleep(0.02)
        assert blocked, "card confirmation did not wait on the locked card row"
        lock_transaction.commit()
    finally:
        event.remove(service.engine, "before_cursor_execute", observe_locked_card_query)
        if lock_transaction.is_active:
            lock_transaction.rollback()
        lock_connection.close()
        pool.shutdown(wait=True)

    assert future.result(timeout=5)["status"] == "completed"
