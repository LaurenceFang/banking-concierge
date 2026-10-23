from __future__ import annotations

from datetime import timedelta

import pytest
from app.bank import BankError, BankService
from app.bank.models import Account, LedgerTransaction, Recipient
from app.task_clock import TaskClock
from app.tasks import TasksService
from sqlalchemy import func, select


@pytest.fixture
def contact_stack(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'contacts.sqlite3').as_posix()}"
    bank = BankService(database_url)
    tasks = TasksService(database_url)
    clock = TaskClock(database_url)
    bank.seed()
    try:
        yield bank, tasks, clock
    finally:
        tasks.close()
        clock.close()
        bank.dispose()


def _balance(bank: BankService, account_id: str) -> int:
    with bank._session_factory() as session:
        result = session.scalar(select(Account.balance_minor).where(Account.id == account_id))
    assert result is not None
    return result


def _destination(bank: BankService, contact_id: str) -> str:
    with bank._session_factory() as session:
        recipient = session.get(Recipient, contact_id)
        assert recipient is not None
        return recipient.destination_account_id


def test_name_only_contact_is_idempotent_aa_payable_but_not_transferable(contact_stack):
    bank, tasks, _clock = contact_stack
    source_before = _balance(bank, "demo-checking-001")
    created = bank.create_contact("小周", note="同事", client_request_id="contact-create-1")
    repeated = bank.create_contact("小周", note="同事", client_request_id="contact-create-1")
    contact_id = created["id"]
    destination_id = _destination(bank, contact_id)

    assert created["can_transfer"] is False
    assert created["account_hint"] == "仅AA参与"
    assert repeated["id"] == contact_id
    assert repeated["already_exists"] is True
    assert {row["id"] for row in bank.list_accounts()} == {"demo-checking-001", "demo-savings-001"}
    assert bank.find_recipients("同事")[0]["can_transfer"] is False
    with pytest.raises(BankError) as transfer_error:
        bank.create_transfer_plan("demo-checking-001", contact_id, 100, "name-only-transfer")
    assert transfer_error.value.code == "RECIPIENT_TRANSFER_DETAILS_REQUIRED"

    plan = tasks.create_action_plan(
        "create_aa",
        "name-only-aa",
        account_id="demo-checking-001",
        total_minor=2_000,
        participant_ids=["self", contact_id],
    )
    accepted = tasks.decide_plan(plan["id"], "approve")
    receipt = tasks.simulate_payment(plan["task_id"], contact_id)

    assert accepted["task_status"] == "awaiting_payment"
    assert receipt["status"] == "completed"
    assert receipt["amount_minor"] == 1_000
    assert _balance(bank, "demo-checking-001") == source_before + 1_000
    assert _balance(bank, destination_id) == 4_999_000


def test_binding_edit_and_removal_preserve_confirmed_schedule_snapshot(contact_stack):
    bank, tasks, clock = contact_stack
    contact = bank.create_contact(
        "小林",
        note="家人",
        bank_name="模拟银行A",
        account_last4="1234",
        client_request_id="contact-create-schedule",
    )
    old_destination = _destination(bank, contact["id"])
    source_before = _balance(bank, "demo-checking-001")
    execute_at = clock.now() + timedelta(hours=1)
    plan = tasks.create_action_plan(
        "schedule_transfer",
        "schedule-new-contact",
        account_id="demo-checking-001",
        recipient_id=contact["id"],
        amount_minor=4_321,
        execute_at=execute_at,
    )
    accepted = tasks.decide_plan(plan["id"], "approve")
    edited = bank.update_contact(
        contact["id"],
        version=1,
        bank_name="模拟银行B",
        account_last4="9876",
    )
    new_destination = _destination(bank, contact["id"])
    removed = bank.remove_contact(contact["id"], version=edited["version"])

    assert accepted["task_status"] == "scheduled"
    assert edited["version"] == 2
    assert old_destination != new_destination
    assert edited["account_last4"] == "9876"
    assert removed["pending_schedule_count"] == 1
    assert removed["contact"]["active"] is False
    assert all(row["id"] != contact["id"] for row in bank.list_contacts())

    result = tasks.process_due(as_of=execute_at)

    assert result[0]["status"] == "completed"
    assert result[0]["recipient_id"] == contact["id"]
    assert result[0]["recipient_name"] == "小林"
    assert _balance(bank, "demo-checking-001") == source_before - 4_321
    assert _balance(bank, old_destination) == 5_004_321
    assert _balance(bank, new_destination) == 5_000_000


def test_contact_edit_invalidates_pending_transfer_and_aa_previews(contact_stack):
    bank, tasks, _clock = contact_stack
    contact = bank.create_contact(
        "小陈",
        bank_name="模拟银行",
        account_last4="5566",
        client_request_id="contact-create-pending",
    )
    transfer = bank.create_transfer_plan("demo-checking-001", contact["id"], 100, "pending-contact-transfer")
    aa = tasks.create_action_plan(
        "create_aa",
        "pending-contact-aa",
        account_id="demo-checking-001",
        total_minor=100,
        participant_ids=["self", contact["id"]],
    )

    bank.update_contact(contact["id"], version=1, name="小陈同学")

    with pytest.raises(BankError) as transfer_error:
        bank.decide_plan(transfer["id"], "approve")
    with pytest.raises(BankError) as aa_error:
        tasks.decide_plan(aa["id"], "approve")
    assert transfer_error.value.code == "PLAN_INVALIDATED"
    assert aa_error.value.code == "PLAN_INVALIDATED"
    with bank._session_factory() as session:
        assert session.scalar(
            select(func.count(LedgerTransaction.id)).where(
                LedgerTransaction.account_id == "demo-checking-001",
                LedgerTransaction.operation_id.is_not(None),
            )
        ) == 0


def test_contact_update_checks_version_and_clears_optional_transfer_binding(contact_stack):
    bank, _tasks, _clock = contact_stack
    contact = bank.create_contact(
        "小许",
        bank_name="模拟银行",
        account_last4="0007",
        client_request_id="contact-create-version",
    )
    cleared = bank.update_contact(contact["id"], version=1, bank_name=None)

    assert cleared["can_transfer"] is False
    assert cleared["bank_name"] is None
    assert cleared["account_last4"] is None
    with pytest.raises(BankError) as error:
        bank.update_contact(contact["id"], version=1, name="旧版本")
    assert error.value.code == "CONTACT_VERSION_CONFLICT"


def test_renaming_contact_keeps_completed_transfer_history_snapshot(contact_stack):
    bank, _tasks, _clock = contact_stack
    contact = bank.create_contact(
        "原联系人名",
        bank_name="模拟银行",
        account_last4="2109",
        client_request_id="contact-create-history",
    )
    plan = bank.create_transfer_plan("demo-checking-001", contact["id"], 321, "history-transfer")
    bank.decide_plan(plan["id"], "approve")

    bank.update_contact(contact["id"], version=1, name="新联系人名")

    restored_plan = bank.get_plan(plan["id"])
    event = next(item for item in bank.completed_events() if item["plan_id"] == plan["id"])
    transactions = bank.list_transactions("demo-checking-001")
    assert restored_plan["recipient_name"] == "原联系人名"
    assert "原联系人名" in event["text"]
    assert any(item["description"] == "转账至原联系人名" for item in transactions)
