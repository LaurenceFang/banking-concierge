from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.bank import BankError, BankService
from app.bank.models import Account, LedgerTransaction, TransferPlan
from sqlalchemy import func, select


@pytest.fixture
def bank(tmp_path):
    db_path = tmp_path / "bank.sqlite3"
    service = BankService(f"sqlite:///{db_path.as_posix()}")
    service.seed()
    yield service
    service.dispose()


def _balance(service: BankService, account_id: str) -> int:
    return next(row["balance_minor"] for row in service.list_accounts() if row["id"] == account_id)


def _plan(service: BankService, *, amount: int, conversation: str | None = None) -> dict:
    return service.create_transfer_plan(
        source_account_id="demo-checking-001",
        recipient_id="demo-recipient-chen",
        amount_minor=amount,
        conversation_id=conversation,
    )


def test_seed_is_idempotent_and_only_exposes_fixed_demo_accounts(bank: BankService):
    first = bank.overview()
    bank.seed()
    second = bank.overview()

    assert first["user"] == {"id": "demo-user-001", "name": "林小满"}
    assert {row["id"] for row in second["accounts"]} == {
        "demo-checking-001",
        "demo-savings-001",
    }
    assert _balance(bank, "demo-checking-001") == 9_100_567
    assert _balance(bank, "demo-savings-001") == 16_008_032
    assert len(first["transactions"]) == len(second["transactions"])


def test_approval_updates_both_accounts_once_and_duplicate_returns_original_result(bank: BankService):
    plan = _plan(bank, amount=12_345, conversation="approval")
    source_before = _balance(bank, "demo-checking-001")
    result = bank.decide_plan(plan["id"], "approve")
    result_again = bank.decide_plan(plan["id"], "approve")

    assert plan["fee_minor"] == 0
    assert plan["total_minor"] == 12_345
    assert result["status"] == "completed"
    assert result["operation_id"]
    assert result_again == result
    assert _balance(bank, "demo-checking-001") == source_before - 12_345
    with bank._session_factory() as session:
        destination = session.get(Account, "demo-destination-chen")
        assert destination.balance_minor == 5_000_000 + 12_345
        transfer_rows = session.scalars(
            select(LedgerTransaction).where(LedgerTransaction.operation_id == result["operation_id"])
        ).all()
        assert sorted(row.amount_minor for row in transfer_rows) == [-12_345, 12_345]
        assert len(transfer_rows) == 2


def test_decline_persists_outcome_without_changing_any_balance(bank: BankService):
    plan = _plan(bank, amount=50_000, conversation="decline")
    before = {
        account["id"]: account["balance_minor"]
        for account in bank.list_accounts()
    }

    result = bank.decide_plan(plan["id"], "decline")
    result_again = bank.decide_plan(plan["id"], "decline")

    assert result["status"] == "declined"
    assert result_again == result
    assert {
        account["id"]: account["balance_minor"] for account in bank.list_accounts()
    } == before
    with bank._session_factory() as session:
        stored = session.get(TransferPlan, plan["id"])
        assert stored.status == "declined"
        assert stored.decision == "decline"
        assert stored.operation_id is None


def test_approval_rechecks_balance_and_declines_without_partial_ledger_effect(bank: BankService):
    expensive = _plan(bank, amount=8_500_000, conversation="expensive")
    later = _plan(bank, amount=1_000_000, conversation="later")
    destination_before = 5_000_000
    with bank._session_factory() as session:
        transaction_count_before = session.scalar(select(func.count(LedgerTransaction.id)))

    first = bank.decide_plan(expensive["id"], "approve")
    source_after_first = _balance(bank, "demo-checking-001")
    second = bank.decide_plan(later["id"], "approve")

    assert first["status"] == "completed"
    assert source_after_first == 600_567
    assert second["status"] == "declined"
    assert second["code"] == "INSUFFICIENT_BALANCE"
    assert bank.decide_plan(later["id"], "approve") == second
    assert _balance(bank, "demo-checking-001") == source_after_first
    with bank._session_factory() as session:
        destination = session.get(Account, "demo-destination-chen")
        assert destination.balance_minor == destination_before + 8_500_000
        later_plan = session.get(TransferPlan, later["id"])
        assert later_plan.status == "declined"
        assert later_plan.operation_id is None
        assert session.scalar(
            select(func.count(LedgerTransaction.id))
        ) == transaction_count_before + 2


def test_same_name_recipients_remain_ambiguous_until_id_is_selected(bank: BankService):
    matches = bank.find_recipients(" 张伟 ")

    assert len(matches) == 2
    assert {match["bank_name"] for match in matches} == {"海岚银行", "东洲银行"}
    assert {match["account_hint"] for match in matches} == {"尾号 7712", "尾号 3306"}
    with pytest.raises(BankError) as error:
        bank.create_transfer_plan("demo-checking-001", "张伟", 100, "ambiguous")
    assert error.value.code == "RECIPIENT_NOT_FOUND"
    assert _balance(bank, "demo-checking-001") == 9_100_567


def test_expired_or_superseded_plan_cannot_move_money(bank: BankService):
    expired = _plan(bank, amount=5_000, conversation="expired")
    with bank._session_factory.begin() as session:
        stored = session.get(TransferPlan, expired["id"])
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    before = _balance(bank, "demo-checking-001")

    result = bank.decide_plan(expired["id"], "approve")

    assert result["status"] == "declined"
    assert result["code"] == "PLAN_EXPIRED"
    assert bank.decide_plan(expired["id"], "approve") == result
    assert _balance(bank, "demo-checking-001") == before

    old = _plan(bank, amount=2_000, conversation="changed")
    new = _plan(bank, amount=3_000, conversation="changed")
    with pytest.raises(BankError) as error:
        bank.decide_plan(old["id"], "approve")
    assert error.value.code == "PLAN_INVALIDATED"
    assert bank.decide_plan(new["id"], "decline")["status"] == "declined"
    assert _balance(bank, "demo-checking-001") == before


def test_account_and_transaction_queries_enforce_fixed_user_ownership(bank: BankService):
    with pytest.raises(BankError) as error:
        bank.list_transactions("demo-destination-chen")
    assert error.value.code == "ACCOUNT_NOT_FOUND"
    with pytest.raises(BankError) as error:
        bank.create_transfer_plan("demo-destination-chen", "demo-recipient-chen", 100)
    assert error.value.code == "ACCOUNT_NOT_FOUND"
