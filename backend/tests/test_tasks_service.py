from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.bank import BankError, BankService
from app.bank.models import Account, LedgerTransaction
from app.task_clock import TaskClock
from app.tasks import TasksService
from app.tasks.models import TaskActionPlan
from app.wealth import WealthService
from sqlalchemy import func, select, update


@pytest.fixture
def task_stack(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'tasks.sqlite3').as_posix()}"
    bank = BankService(database_url)
    tasks = TasksService(database_url)
    clock = TaskClock(database_url)
    bank.seed()
    try:
        yield database_url, bank, tasks, clock
    finally:
        tasks.close()
        clock.close()
        bank.dispose()


def _account_balance(bank: BankService, account_id: str) -> int:
    # External recipient accounts are intentionally absent from the user's
    # account-list API, so inspect the persisted ledger row for these service tests.
    with bank._session_factory() as session:
        result = session.scalar(select(Account.balance_minor).where(Account.id == account_id))
    assert result is not None
    return result


def _create_aa(tasks: TasksService, conversation_id: str = "aa-create") -> dict:
    return tasks.create_action_plan(
        "create_aa",
        conversation_id,
        account_id="demo-checking-001",
        total_minor=100,
        participant_ids=["self", "demo-recipient-chen", "demo-recipient-li"],
    )


def _create_schedule(tasks: TasksService, execute_at: datetime, *, amount_minor: int = 5_000, conversation_id="time-create") -> dict:
    return tasks.create_action_plan(
        "schedule_transfer",
        conversation_id,
        account_id="demo-checking-001",
        recipient_id="demo-recipient-chen",
        amount_minor=amount_minor,
        execute_at=execute_at,
    )


def test_aa_preview_splits_remainder_and_request_does_not_book_money(task_stack):
    _database_url, bank, tasks, _clock = task_stack
    source_before = _account_balance(bank, "demo-checking-001")
    transactions_before = bank.list_transactions()

    plan = _create_aa(tasks)

    assert plan["kind"] == "task"
    assert plan["action"] == "create_aa"
    assert plan["collectable_minor"] == 66
    assert [(row["id"], row["share_minor"]) for row in plan["participants"]] == [
        ("self", 34),
        ("demo-recipient-chen", 33),
        ("demo-recipient-li", 33),
    ]
    assert tasks.list_aa() == []
    assert _account_balance(bank, "demo-checking-001") == source_before
    assert bank.list_transactions() == transactions_before

    result = tasks.decide_plan(plan["id"], "approve")
    aa = tasks.get_task(plan["task_id"])
    assert result["status"] == "completed"
    assert result["task_status"] == "awaiting_payment"
    assert aa["collected_minor"] == 0
    assert aa["status"] == "awaiting_payment"
    assert _account_balance(bank, "demo-checking-001") == source_before
    assert bank.list_transactions() == transactions_before


def test_aa_partial_payment_is_idempotent_and_cancel_keeps_received_money(task_stack):
    _database_url, bank, tasks, _clock = task_stack
    source_before = _account_balance(bank, "demo-checking-001")
    payer_before = _account_balance(bank, "demo-destination-chen")
    plan = _create_aa(tasks, "aa-partial")
    tasks.decide_plan(plan["id"], "approve")

    first = tasks.simulate_payment(plan["task_id"], "demo-recipient-chen")
    repeated = tasks.simulate_payment(plan["task_id"], "demo-recipient-chen")
    assert first["task_status"] == "partial"
    assert first["collected_minor"] == 33
    assert repeated["duplicate"] is True
    assert repeated["operation_id"] == first["operation_id"]
    assert _account_balance(bank, "demo-checking-001") == source_before + 33
    assert _account_balance(bank, "demo-destination-chen") == payer_before - 33
    with bank._session_factory() as session:
        assert session.scalar(
            select(func.count(LedgerTransaction.id)).where(
                LedgerTransaction.operation_id == first["operation_id"]
            )
        ) == 2

    cancel = tasks.create_action_plan("cancel_aa", "aa-cancel", task_id=plan["task_id"])
    result = tasks.decide_plan(cancel["id"], "approve")
    aa = tasks.get_task(plan["task_id"])
    assert result["task_status"] == "cancelled"
    assert aa["status"] == "cancelled"
    assert aa["collected_minor"] == 33
    participants = {row["id"]: row for row in aa["participants"]}
    assert participants["demo-recipient-chen"]["status"] == "paid"
    assert participants["demo-recipient-li"]["status"] == "cancelled"
    assert _account_balance(bank, "demo-checking-001") == source_before + 33
    with pytest.raises(BankError) as error:
        tasks.simulate_payment(plan["task_id"], "demo-recipient-li")
    assert error.value.code == "TASK_CANCELLED"


def test_aa_insufficient_member_balance_has_no_partial_effect(task_stack):
    _database_url, bank, tasks, _clock = task_stack
    plan = _create_aa(tasks, "aa-insufficient")
    tasks.decide_plan(plan["id"], "approve")
    with bank.engine.begin() as connection:
        connection.execute(
            update(Account)
            .where(Account.id == "demo-destination-chen")
            .values(balance_minor=0)
        )
    before_count = len(bank.list_transactions())

    with pytest.raises(BankError) as error:
        tasks.simulate_payment(plan["task_id"], "demo-recipient-chen")

    assert error.value.code == "INSUFFICIENT_BALANCE"
    aa = tasks.get_task(plan["task_id"])
    assert aa["status"] == "awaiting_payment"
    assert aa["collected_minor"] == 0
    assert next(row for row in aa["participants"] if row["id"] == "demo-recipient-chen")["status"] == "pending"
    assert len(bank.list_transactions()) == before_count


def test_schedule_reserves_only_on_confirmation_and_recovers_after_restart(task_stack):
    database_url, bank, tasks, clock = task_stack
    source_before = _account_balance(bank, "demo-checking-001")
    destination_before = _account_balance(bank, "demo-destination-chen")
    execute_at = clock.now() + timedelta(hours=1)
    plan = _create_schedule(tasks, execute_at)

    assert plan["reserved_minor"] == 5_000
    assert tasks.list_schedules() == []
    assert _account_balance(bank, "demo-checking-001") == source_before
    accepted = tasks.decide_plan(plan["id"], "approve")
    assert accepted["status"] == "completed"
    assert accepted["task_status"] == "scheduled"
    assert _account_balance(bank, "demo-checking-001") == source_before
    assert tasks.list_schedules()[0]["reserved_minor"] == 5_000

    assert tasks.process_due(as_of=execute_at - timedelta(microseconds=1)) == []
    tasks.close()
    restarted = TasksService(database_url)
    try:
        result = restarted.process_due(as_of=execute_at)
        assert len(result) == 1
        assert result[0]["status"] == "completed"
        assert result[0]["reserved_minor"] == 0
        assert restarted.process_due(as_of=execute_at + timedelta(minutes=5)) == []
        assert _account_balance(bank, "demo-checking-001") == source_before - 5_000
        assert _account_balance(bank, "demo-destination-chen") == destination_before + 5_000
        with bank._session_factory() as session:
            schedule = session.get(__import__("app.tasks.models", fromlist=["ScheduledTransfer"]).ScheduledTransfer, plan["task_id"])
            assert schedule is not None
            assert session.scalar(
                select(func.count(LedgerTransaction.id)).where(
                    LedgerTransaction.operation_id == schedule.operation_id
                )
            ) == 2
    finally:
        restarted.close()


def test_schedule_executes_at_exact_deadline_but_expires_after_it(task_stack):
    _database_url, bank, tasks, clock = task_stack
    source_before = _account_balance(bank, "demo-checking-001")
    execute_at = clock.now() + timedelta(minutes=10)
    allowed = _create_schedule(tasks, execute_at, conversation_id="deadline-exact")
    tasks.decide_plan(allowed["id"], "approve")
    assert tasks.process_due(as_of=execute_at + timedelta(hours=24)) [0]["status"] == "completed"

    expired_at = clock.now() + timedelta(minutes=15)
    expired_plan = _create_schedule(tasks, expired_at, conversation_id="deadline-late")
    tasks.decide_plan(expired_plan["id"], "approve")
    deadline = datetime.fromisoformat(expired_plan["deadline_at"])
    result = tasks.process_due(as_of=deadline + timedelta(microseconds=1))
    expired = next(row for row in result if row["id"] == expired_plan["task_id"])

    assert expired["status"] == "expired"
    assert expired["reserved_minor"] == 0
    assert tasks.process_due(as_of=deadline + timedelta(hours=1)) == []
    assert _account_balance(bank, "demo-checking-001") == source_before - 5_000


def test_explicit_cancel_releases_hold_and_new_chat_does_not_revoke_future_authorization(task_stack):
    _database_url, bank, tasks, clock = task_stack
    source_before = _account_balance(bank, "demo-checking-001")
    schedule = _create_schedule(tasks, clock.now() + timedelta(days=1), conversation_id="keep-future")
    tasks.decide_plan(schedule["id"], "approve")
    assert tasks.invalidate_pending_plans("keep-future") == 0
    # A new pending preview in the same conversation must not cancel the already
    # confirmed future transfer or release its hold.
    aa_plan = tasks.create_action_plan(
        "create_aa",
        "keep-future",
        account_id="demo-checking-001",
        total_minor=100,
        participant_ids=["self", "demo-recipient-chen"],
    )
    assert tasks.get_task(schedule["task_id"])["status"] == "scheduled"
    assert tasks.list_schedules()[0]["reserved_minor"] == 5_000
    tasks.decide_plan(aa_plan["id"], "decline")

    cancel = tasks.create_action_plan("cancel_schedule", "cancel-future", task_id=schedule["task_id"])
    cancelled = tasks.decide_plan(cancel["id"], "approve")
    assert cancelled["task_status"] == "cancelled"
    assert tasks.get_task(schedule["task_id"])["reserved_minor"] == 0
    assert _account_balance(bank, "demo-checking-001") == source_before
    assert tasks.decide_plan(cancel["id"], "approve") == cancelled

    repeated_cancel = tasks.create_action_plan("cancel_schedule", "cancel-future-again", task_id=schedule["task_id"])
    assert tasks.decide_plan(repeated_cancel["id"], "approve")["task_status"] == "cancelled"
    assert _account_balance(bank, "demo-checking-001") == source_before


def test_schedule_reservation_blocks_bank_transfer_and_wealth_purchase(task_stack):
    database_url, bank, tasks, clock = task_stack
    wealth = WealthService(database_url)
    wealth.seed_demo()
    wealth.set_risk_profile(
        {
            "horizon": "long",
            "loss_tolerance": "high",
            "experience": "experienced",
            "liquidity_need": "low",
            "objective": "growth",
        }
    )
    try:
        account_before = _account_balance(bank, "demo-checking-001")
        schedule = _create_schedule(
            tasks,
            clock.now() + timedelta(hours=2),
            amount_minor=9_010_000,
            conversation_id="reservation-race",
        )
        assert tasks.decide_plan(schedule["id"], "approve")["task_status"] == "scheduled"
        assert _account_balance(bank, "demo-checking-001") == account_before
        assert tasks.list_schedules()[0]["reserved_minor"] == 9_010_000

        with pytest.raises(BankError) as transfer_error:
            bank.create_transfer_plan(
                "demo-checking-001", "demo-recipient-chen", 100_000, conversation_id="bank-contender"
            )
        assert transfer_error.value.code == "INSUFFICIENT_BALANCE"

        with pytest.raises(BankError) as wealth_error:
            wealth.create_action_plan(
                "purchase",
                "wealth-contender",
                product_id="demo-wealth-flex",
                account_id="demo-checking-001",
                amount_minor=100_000,
            )
        assert wealth_error.value.code == "INSUFFICIENT_BALANCE"
        assert _account_balance(bank, "demo-checking-001") == account_before
    finally:
        wealth.close()


def test_plan_ttl_uses_real_time_and_rejects_naive_execution_time(task_stack):
    _database_url, _bank, tasks, _clock = task_stack
    with pytest.raises(BankError) as error:
        _create_schedule(tasks, datetime.now() + timedelta(hours=1))
    assert error.value.code == "INVALID_EXECUTE_AT"

    plan = _create_aa(tasks, "plan-expiry")
    with tasks._transaction() as session:
        row = session.get(TaskActionPlan, plan["id"])
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    result = tasks.decide_plan(plan["id"], "approve")
    assert result["status"] == "declined"
    assert result["code"] == "PLAN_EXPIRED"
    assert tasks.get_task(plan["task_id"]) is None


def test_seed_does_not_create_completed_tasks(task_stack):
    _database_url, _bank, tasks, _clock = task_stack
    tasks.seed_demo()
    assert tasks.list_aa() == []
    assert tasks.list_schedules() == []
