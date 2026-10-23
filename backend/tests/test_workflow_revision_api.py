from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from time import sleep

import pytest
from app.bank.errors import BankError
from app.bank.service import BankService
from app.plan_routes import plan_revision_router
from app.task_clock import TaskClock
from app.workflows import WorkflowService
from app.workflows.models import WorkflowPlan
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine


@pytest.fixture
def revision_scenario(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'workflow-revision.sqlite3').as_posix()}"
    bank = BankService(database_url)
    bank.seed()
    clock = TaskClock(database_url)
    workflow = WorkflowService(database_url)
    workflow.seed_demo()
    yield bank, clock, workflow, database_url
    workflow.close()
    clock.close()
    bank.dispose()


def _preview(
    workflow: WorkflowService,
    clock: TaskClock,
    *,
    conversation_id: str = "revision-chat",
    execute_at: datetime | None = None,
    immediate: bool = False,
):
    requested_at = None if immediate else execute_at or clock.now() + timedelta(minutes=30)
    return workflow.create_action_plan(
        "arrange",
        conversation_id,
        account_id="demo-checking-001",
        recipient_label="妈妈",
        budget_minor=50_000,
        title="生日安排",
        items=[
            {"product_id": "flowers-basic", "quantity": 1},
            {"product_id": "cake-basic", "quantity": 1},
        ],
        execute_at=requested_at,
    )


def test_revision_replaces_items_preserves_other_fields_and_survives_restart(revision_scenario):
    _bank, clock, workflow, database_url = revision_scenario
    original = _preview(workflow, clock)

    revised = workflow.revise_action_plan(
        original["id"],
        "revision-chat",
        {
            "recipient_label": "伴侣",
            "budget_minor": 30_000,
            "items": [{"product_id": "flowers-basic", "quantity": 1}],
        },
    )

    assert revised["status"] == "pending"
    assert revised["account_id"] == original["account_id"]
    assert revised["execute_at"] == original["execute_at"]
    assert revised["title"] == "生日安排"
    assert revised["recipient_label"] == "伴侣"
    assert revised["budget_minor"] == 30_000
    assert revised["total_minor"] == 19_900
    assert [(row["product_id"], row["quantity"]) for row in revised["items"]] == [
        ("flowers-basic", 1)
    ]
    assert revised["revision"]["source_plan_id"] == original["id"]
    assert {row["field"] for row in revised["revision"]["changes"]} == {
        "recipient_label",
        "budget_minor",
        "items",
    }
    assert all(row["summary"] and row["before"] != row["after"] for row in revised["revision"]["changes"])
    assert workflow.get_plan(original["id"])["status"] == "invalidated"

    workflow.close()
    restarted = WorkflowService(database_url)
    try:
        restarted.seed_demo()
        restored = restarted.get_plan(revised["id"])
        assert restored["revision"] == revised["revision"]
        with restarted._session_factory() as session:
            stored = session.get(WorkflowPlan, revised["id"])
            assert stored.revision_json
    finally:
        restarted.close()


def test_immediate_revision_uses_current_time_without_reporting_a_timing_change(revision_scenario):
    _bank, clock, workflow, _database_url = revision_scenario
    original = _preview(workflow, clock, immediate=True)
    assert original["execute_immediately"] is True

    # Let the real task clock advance between preview creation and revision.
    sleep(0.02)
    revised = workflow.revise_action_plan(
        original["id"],
        "revision-chat",
        {
            "budget_minor": 30_000,
            "items": [{"product_id": "flowers-basic", "quantity": 1}],
        },
    )

    assert revised["execute_immediately"] is True
    assert datetime.fromisoformat(revised["execute_at"]) > datetime.fromisoformat(original["execute_at"])
    assert revised["total_minor"] == 19_900
    assert {item["field"] for item in revised["revision"]["changes"]} == {"budget_minor", "items"}


def test_scheduled_revision_preserves_time_and_rejects_a_missed_schedule(revision_scenario):
    _bank, clock, workflow, _database_url = revision_scenario
    scheduled_at = clock.now() + timedelta(minutes=30)
    original = _preview(workflow, clock, execute_at=scheduled_at)
    revised = workflow.revise_action_plan(
        original["id"],
        "revision-chat",
        {"budget_minor": 60_000},
    )
    assert revised["execute_immediately"] is False
    assert revised["execute_at"] == original["execute_at"]
    assert "execute_at" not in {item["field"] for item in revised["revision"]["changes"]}

    due_at = clock.now() + timedelta(minutes=1)
    missed = _preview(workflow, clock, conversation_id="missed-schedule", execute_at=due_at)
    clock.advance(2)
    with pytest.raises(BankError) as error:
        workflow.revise_action_plan(
            missed["id"],
            "missed-schedule",
            {"budget_minor": 60_000},
        )
    assert error.value.code == "INVALID_EXECUTE_AT"
    assert workflow.get_plan(missed["id"])["status"] == "pending"


def test_legacy_past_schedule_migrates_as_scheduled_not_immediate(revision_scenario):
    _bank, clock, workflow, database_url = revision_scenario
    missed = _preview(
        workflow,
        clock,
        conversation_id="legacy-schedule",
        execute_at=clock.now() + timedelta(minutes=1),
    )
    clock.advance(2)
    workflow.close()

    migration_engine = create_engine(database_url)
    try:
        with migration_engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE workflow_plans DROP COLUMN execute_immediately")
    finally:
        migration_engine.dispose()

    restarted = WorkflowService(database_url)
    try:
        migrated = restarted.get_plan(missed["id"])
        assert migrated["execute_immediately"] is False
        with pytest.raises(BankError) as error:
            restarted.revise_action_plan(
                missed["id"],
                "legacy-schedule",
                {"budget_minor": 60_000},
            )
        assert error.value.code == "INVALID_EXECUTE_AT"
        assert restarted.get_plan(missed["id"])["status"] == "pending"
    finally:
        restarted.close()


@pytest.mark.parametrize(
    ("changes", "expected_code"),
    [
        ({"items": [{"product_id": "flowers-basic", "quantity": 2}], "budget_minor": 10_000}, "BUDGET_EXCEEDED"),
        ({"items": [{"product_id": "not-in-catalog", "quantity": 1}]}, "PRODUCT_NOT_FOUND"),
        ({"items": None}, "INVALID_ITEMS"),
    ],
)
def test_invalid_revision_keeps_original_preview_approvable(revision_scenario, changes, expected_code):
    _bank, clock, workflow, _database_url = revision_scenario
    original = _preview(workflow, clock)

    with pytest.raises(BankError) as error:
        workflow.revise_action_plan(original["id"], "revision-chat", changes)

    assert error.value.code == expected_code
    assert workflow.get_plan(original["id"])["status"] == "pending"
    result = workflow.decide_plan(original["id"], "approve", "revision-chat")
    assert result["status"] == "completed"
    task = workflow.get_task(original["task_id"])
    assert task["status"] == "scheduled"
    assert task["reserved_minor"] == 32_800


def test_revision_rejects_wrong_conversation_stale_expired_and_decided_plans(revision_scenario):
    _bank, clock, workflow, _database_url = revision_scenario
    original = _preview(workflow, clock)

    with pytest.raises(BankError, match="另一条会话"):
        workflow.revise_action_plan(original["id"], "other-chat", {"recipient_label": "家人"})
    assert workflow.get_plan(original["id"])["status"] == "pending"

    revised = workflow.revise_action_plan(original["id"], "revision-chat", {"recipient_label": "家人"})
    with pytest.raises(BankError) as stale:
        workflow.revise_action_plan(original["id"], "revision-chat", {"budget_minor": 60_000})
    assert stale.value.code == "PLAN_INVALIDATED"
    assert workflow.get_plan(revised["id"])["status"] == "pending"

    with workflow._transaction() as session:
        row = session.get(WorkflowPlan, revised["id"])
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(BankError) as expired:
        workflow.revise_action_plan(revised["id"], "revision-chat", {"recipient_label": "家人"})
    assert expired.value.code == "PLAN_EXPIRED"
    assert workflow.get_plan(revised["id"])["status"] == "expired"

    decided = _preview(workflow, clock, conversation_id="decided-chat")
    workflow.decide_plan(decided["id"], "approve", "decided-chat")
    with pytest.raises(BankError) as already_decided:
        workflow.revise_action_plan(decided["id"], "decided-chat", {"budget_minor": 60_000})
    assert already_decided.value.code == "PLAN_ALREADY_DECIDED"


def test_concurrent_revisions_replace_one_source_plan_only_once(revision_scenario):
    _bank, clock, workflow, _database_url = revision_scenario
    original = _preview(workflow, clock)

    def revise(label: str):
        try:
            return workflow.revise_action_plan(
                original["id"],
                "revision-chat",
                {"recipient_label": label},
            )
        except BankError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(revise, ("伴侣", "家人")))

    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(result == "PLAN_INVALIDATED" for result in results) == 1
    current = workflow.pending_for_conversation("revision-chat")
    assert current is not None
    assert current["id"] != original["id"]


def test_revision_route_accepts_patch_and_rejects_extra_fields(revision_scenario):
    _bank, clock, workflow, _database_url = revision_scenario
    original = _preview(workflow, clock)
    app = FastAPI()
    app.state.workflows = workflow
    app.include_router(plan_revision_router(app))

    with TestClient(app) as client:
        response = client.post(
            f"/api/workflow-plans/{original['id']}/revise",
            json={
                "conversation_id": "revision-chat",
                "items": [{"product_id": "flowers-basic", "quantity": 1}],
            },
        )
        assert response.status_code == 200
        plan = response.json()
        assert plan["kind"] == "workflow"
        assert plan["action"] == "arrange"
        assert plan["revision"]["source_plan_id"] == original["id"]

        immediate = client.post(
            f"/api/workflow-plans/{plan['id']}/revise",
            json={"conversation_id": "revision-chat", "execute_at": None},
        )
        assert immediate.status_code == 200
        assert immediate.json()["execute_immediately"] is True
        timing = next(item for item in immediate.json()["revision"]["changes"] if item["field"] == "execute_at")
        assert timing["after"] == "确认后立即开始"

        extra = client.post(
            f"/api/workflow-plans/{immediate.json()['id']}/revise",
            json={"conversation_id": "revision-chat", "dry_run": True},
        )
        assert extra.status_code == 422
