from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from app.subscriptions import SubscriptionService
from app.subscriptions.models import RenewalNotification, SubscriptionActionPlan, SubscriptionContract
from sqlalchemy import delete, make_url


@pytest.fixture
def postgres_services():
    database_url = os.environ.get("SUBSCRIPTIONS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set SUBSCRIPTIONS_TEST_DATABASE_URL to run isolated PostgreSQL lock tests.")
    parsed = make_url(database_url)
    if (
        parsed.get_backend_name() != "postgresql"
        or parsed.host != "127.0.0.1"
        or parsed.database != "concierge_test"
        or parsed.port != 55432
    ):
        pytest.fail("PostgreSQL lock tests require the dedicated concierge_test database on port 55432.")

    first = SubscriptionService(database_url)
    _reset_subscription_rows(first)
    first.seed_demo()
    second = SubscriptionService(database_url)
    try:
        yield first, second
    finally:
        second.close()
        _reset_subscription_rows(first)
        first.close()


def _reset_subscription_rows(service: SubscriptionService) -> None:
    """Clear only subscription-owned rows in the dedicated test database."""

    with service.engine.begin() as connection:
        connection.execute(delete(RenewalNotification))
        connection.execute(delete(SubscriptionActionPlan))
        connection.execute(delete(SubscriptionContract))


def _run_together(calls):
    barrier = Barrier(len(calls))

    def run(service: SubscriptionService, plan_id: str) -> dict:
        barrier.wait(timeout=10)
        return service.decide_plan(plan_id, "approve")

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(run, service, plan_id) for service, plan_id in calls]
        return [future.result(timeout=20) for future in futures]


def test_postgres_same_plan_confirmation_is_serialized_and_idempotent(postgres_services):
    first, second = postgres_services
    plan = first.create_action_plan("demo-sub-video", "stop_autopay")

    results = _run_together([(first, plan["id"]), (second, plan["id"])])

    assert results[0] == results[1]
    assert results[0]["status"] == "completed"
    assert results[0]["operation_id"]
    assert first.decide_plan(plan["id"], "approve") == results[0]


def test_postgres_distinct_plans_for_one_subscription_lock_contract_state(postgres_services):
    first, second = postgres_services
    plans = [
        first.create_action_plan("demo-sub-music", "stop_autopay"),
        first.create_action_plan("demo-sub-music", "stop_autopay"),
    ]

    results = _run_together(
        [(first, plans[0]["id"]), (second, plans[1]["id"])]
    )

    assert sorted(result["status"] for result in results) == ["completed", "declined"]
    assert sum("operation_id" in result for result in results) == 1
    declined = next(result for result in results if result["status"] == "declined")
    assert declined["code"] == "SUBSCRIPTION_STATE_CHANGED"
    assert first.decide_plan(plans[0]["id"], "approve") == results[0]
    assert second.decide_plan(plans[1]["id"], "approve") == results[1]


def test_postgres_different_subscription_rows_can_complete_concurrently(postgres_services):
    first, second = postgres_services
    video = first.create_action_plan("demo-sub-video", "stop_autopay")
    music = first.create_action_plan("demo-sub-music", "stop_autopay")

    results = _run_together([(first, video["id"]), (second, music["id"])])

    assert [result["status"] for result in results] == ["completed", "completed"]
    assert results[0]["operation_id"] != results[1]["operation_id"]
    assert len(first.completed_events()) == 2
