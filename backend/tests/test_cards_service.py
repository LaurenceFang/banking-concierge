from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from app.bank import BankError, BankService
from app.cards import CardService
from app.cards.models import Base, Card
from sqlalchemy import inspect


@pytest.fixture
def cards(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'cards.sqlite3').as_posix()}"
    service = CardService(database_url)
    service.seed_demo()
    try:
        yield service
    finally:
        service.close()


def _card(service: CardService, card_id: str = "demo-card-debit-001") -> dict:
    return next(row for row in service.list_cards() if row["id"] == card_id)


def test_seed_is_idempotent_and_uses_only_card_tables(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'cards-only.sqlite3').as_posix()}"
    service = CardService(database_url)
    try:
        service.seed_demo()
        first = service.list_cards()
        service.seed_demo()
        assert service.list_cards() == first
        assert {row["id"] for row in first} == {
            "demo-card-debit-001",
            "demo-card-credit-001",
        }
        debit = _card(service)
        credit = _card(service, "demo-card-credit-001")
        assert (debit["kind"], debit["last4"], debit["account_id"]) == (
            "debit",
            "6028",
            "demo-checking-001",
        )
        assert debit["daily_limit_minor"] == 500_000
        assert debit["credit_limit_minor"] is None
        assert (credit["kind"], credit["last4"]) == ("credit", "7391")
        assert credit["credit_limit_minor"] == 2_000_000
        assert credit["outstanding_minor"] == 85_000
        assert credit["available_credit_minor"] == 1_915_000
        assert all("card_number" not in row and "full_number" not in row for row in first)
        assert set(inspect(service.engine).get_table_names()) == {
            "card_action_plans",
            "card_applications",
            "card_records",
        }
        assert not any(name.startswith("bank_") for name in inspect(service.engine).get_table_names())
        assert set(Base.metadata.tables) == {
            "card_action_plans",
            "card_applications",
            "card_records",
        }
    finally:
        service.close()


def test_freeze_blocks_only_card_channel_and_confirmation_is_idempotent(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'cards-with-bank.sqlite3').as_posix()}"
    bank = BankService(database_url)
    cards = CardService(database_url)
    try:
        bank.seed()
        cards.seed_demo()
        account_before = next(row for row in bank.list_accounts() if row["id"] == "demo-checking-001")[
            "balance_minor"
        ]
        transactions_before = bank.list_transactions("demo-checking-001")

        plan = cards.create_action_plan("freeze", "freeze-flow", card_id="demo-card-debit-001")
        assert plan["kind"] == "card"
        assert plan["status"] == "pending"
        assert plan["before"] == {"status": "active"}
        assert plan["after"] == {"status": "frozen"}
        result = cards.decide_plan(plan["id"], "approve")

        assert result["status"] == "completed"
        assert result["operation_id"]
        assert cards.decide_plan(plan["id"], "approve") == result
        assert _card(cards)["status"] == "frozen"
        assert _card(cards)["version"] == 2
        rejected = cards.authorize_charge("demo-card-debit-001", 100)
        assert rejected["allowed"] is False
        assert rejected["code"] == "CARD_FROZEN"
        assert "未发生扣款" in rejected["message"]
        assert (
            next(row for row in bank.list_accounts() if row["id"] == "demo-checking-001")["balance_minor"]
            == account_before
        )
        assert bank.list_transactions("demo-checking-001") == transactions_before

        transfer = bank.create_transfer_plan(
            "demo-checking-001",
            "demo-recipient-chen",
            250,
            conversation_id="transfer-after-card-freeze",
        )
        assert bank.decide_plan(transfer["id"], "approve")["status"] == "completed"
        assert (
            next(row for row in bank.list_accounts() if row["id"] == "demo-checking-001")["balance_minor"]
            == account_before - 250
        )
        assert len(bank.list_transactions("demo-checking-001")) == len(transactions_before) + 1

        events = cards.completed_events()
        assert len(events) == 1
        assert events[0]["id"] == f"card:{result['operation_id']}"
        assert events[0]["plan_id"] == plan["id"]
        assert events[0]["receipt"]["operation_id"] == result["operation_id"]
        assert cards.completed_events() == events
    finally:
        cards.close()
        bank.dispose()


def test_status_transitions_reject_repeat_and_lost_card_cannot_be_unfrozen(cards):
    freeze = cards.create_action_plan("freeze", "status-flow", card_id="demo-card-debit-001")
    cards.decide_plan(freeze["id"], "approve")
    with pytest.raises(BankError) as error:
        cards.create_action_plan("freeze", "status-flow", card_id="demo-card-debit-001")
    assert error.value.code == "CARD_ALREADY_FROZEN"

    unfreeze = cards.create_action_plan("unfreeze", "status-flow", card_id="demo-card-debit-001")
    cards.decide_plan(unfreeze["id"], "approve")
    assert _card(cards)["status"] == "active"

    lost = cards.create_action_plan("report_lost", "status-flow", card_id="demo-card-debit-001")
    cards.decide_plan(lost["id"], "approve")
    assert _card(cards)["status"] == "lost"
    with pytest.raises(BankError) as error:
        cards.create_action_plan("unfreeze", "status-flow", card_id="demo-card-debit-001")
    assert error.value.code == "CARD_UNFREEZE_NOT_ALLOWED"
    assert _card(cards)["version"] == 4


def test_quota_rules_are_integer_fen_bounded_and_reject_noop_changes(cards):
    lower = cards.create_action_plan(
        "set_daily_limit",
        "limits",
        card_id="demo-card-debit-001",
        amount_minor=10_000,
    )
    assert lower["before"] == {"daily_limit_minor": 500_000}
    assert lower["after"] == {"daily_limit_minor": 10_000}
    assert cards.decide_plan(lower["id"], "approve")["status"] == "completed"

    with pytest.raises(BankError) as error:
        cards.create_action_plan(
            "set_daily_limit",
            "limits",
            card_id="demo-card-debit-001",
            amount_minor=10_000,
        )
    assert error.value.code == "CARD_LIMIT_UNCHANGED"
    for invalid_amount in (9_999, 2_000_001, True, 0, 100.5):
        with pytest.raises(BankError):
            cards.create_action_plan(
                "set_daily_limit",
                "limits",
                card_id="demo-card-debit-001",
                amount_minor=invalid_amount,
            )

    minimum_credit_limit = cards.create_action_plan(
        "set_credit_limit",
        "limits",
        card_id="demo-card-credit-001",
        amount_minor=300_000,
    )
    assert cards.decide_plan(minimum_credit_limit["id"], "approve")["status"] == "completed"
    maximum_credit_limit = cards.create_action_plan(
        "set_credit_limit",
        "limits",
        card_id="demo-card-credit-001",
        amount_minor=5_000_000,
    )
    assert cards.decide_plan(maximum_credit_limit["id"], "approve")["status"] == "completed"
    assert _card(cards, "demo-card-credit-001")["credit_limit_minor"] == 5_000_000

    for invalid_amount, code in (
        (84_999, "CREDIT_LIMIT_OUT_OF_RANGE"),
        (299_999, "CREDIT_LIMIT_OUT_OF_RANGE"),
        (5_000_001, "CREDIT_LIMIT_OUT_OF_RANGE"),
        (5_000_000, "CARD_LIMIT_UNCHANGED"),
    ):
        with pytest.raises(BankError) as error:
            cards.create_action_plan(
                "set_credit_limit",
                "limits",
                card_id="demo-card-credit-001",
                amount_minor=invalid_amount,
            )
        assert error.value.code == code
    with cards._transaction() as session:
        credit_card = session.get(Card, "demo-card-credit-001")
        credit_card.outstanding_minor = 400_000
    with pytest.raises(BankError) as error:
        cards.create_action_plan(
            "set_credit_limit",
            "limits",
            card_id="demo-card-credit-001",
            amount_minor=350_000,
        )
    assert error.value.code == "CREDIT_LIMIT_BELOW_OUTSTANDING"
    with pytest.raises(BankError) as error:
        cards.create_action_plan(
            "set_credit_limit",
            "limits",
            card_id="demo-card-debit-001",
            amount_minor=400_000,
        )
    assert error.value.code == "CARD_KIND_UNSUPPORTED"
    assert _card(cards)["daily_limit_minor"] == 10_000


def test_competing_previews_cannot_overwrite_a_new_card_version(cards):
    quota = cards.create_action_plan(
        "set_daily_limit",
        "quota-change",
        card_id="demo-card-debit-001",
        amount_minor=450_000,
    )
    freeze = cards.create_action_plan("freeze", "status-change", card_id="demo-card-debit-001")

    first = cards.decide_plan(freeze["id"], "approve")
    stale = cards.decide_plan(quota["id"], "approve")

    assert first["status"] == "completed"
    assert stale["status"] == "declined"
    assert stale["code"] == "CARD_STATE_CHANGED"
    current = _card(cards)
    assert current["status"] == "frozen"
    assert current["daily_limit_minor"] == 500_000
    assert current["version"] == 2
    assert len(cards.completed_events()) == 1


def test_preview_replacement_is_conversation_local_and_invalid_preview_keeps_old_plan(cards):
    first = cards.create_action_plan("freeze", "conversation-a", card_id="demo-card-debit-001")
    other = cards.create_action_plan(
        "set_daily_limit",
        "conversation-b",
        card_id="demo-card-credit-001",
        amount_minor=250_000,
    )
    replacement = cards.create_action_plan(
        "set_daily_limit",
        "conversation-a",
        card_id="demo-card-debit-001",
        amount_minor=400_000,
    )
    assert cards.get_plan(first["id"])["status"] == "invalidated"
    assert cards.pending_for_conversation("conversation-a")["id"] == replacement["id"]
    assert cards.pending_for_conversation("conversation-b")["id"] == other["id"]

    with pytest.raises(BankError) as error:
        cards.create_action_plan(
            "set_credit_limit",
            "conversation-a",
            card_id="demo-card-debit-001",
            amount_minor=400_000,
        )
    assert error.value.code == "CARD_KIND_UNSUPPORTED"
    assert cards.pending_for_conversation("conversation-a")["id"] == replacement["id"]
    assert cards.invalidate_pending_plans("conversation-a") == 1
    assert cards.pending_for_conversation("conversation-a") is None
    assert cards.pending_for_conversation("conversation-b")["id"] == other["id"]


def test_apply_card_submits_only_and_repeated_confirmation_creates_one_record(cards):
    plan = cards.create_action_plan("apply_card", "application", card_kind="credit")
    assert plan["card_id"] is None
    assert plan["card_kind"] == "credit"
    result = cards.decide_plan(plan["id"], "approve")

    assert result["status"] == "completed"
    assert "待审核" in result["message"]
    assert "不表示已经获批" in result["message"]
    assert cards.decide_plan(plan["id"], "approve") == result
    applications = cards.list_applications()
    assert len(applications) == 1
    assert applications[0]["card_kind"] == "credit"
    assert applications[0]["status"] == "submitted"
    assert applications[0]["operation_id"] == result["operation_id"]
    assert len(cards.list_cards()) == 2

    event = cards.completed_events()[0]
    assert event["id"] == f"card:{result['operation_id']}"
    assert event["plan_id"] == plan["id"]
    assert event["receipt"]["application_status"] == "submitted"
    assert len(cards.completed_events()) == 1


def test_completed_card_actions_and_application_survive_service_reopen(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'cards-restart.sqlite3').as_posix()}"
    first = CardService(database_url)
    first.seed_demo()
    plan = first.create_action_plan("apply_card", "restart-flow", card_kind="debit")
    result = first.decide_plan(plan["id"], "approve")
    first.close()

    restored = CardService(database_url)
    try:
        assert restored.list_applications()[0]["status"] == "submitted"
        assert restored.list_applications()[0]["operation_id"] == result["operation_id"]
        event = restored.completed_events()[0]
        assert event["id"] == f"card:{result['operation_id']}"
        assert event["plan_id"] == plan["id"]
        assert restored.decide_plan(plan["id"], "approve") == result
        assert len(restored.list_applications()) == 1
        assert len(restored.completed_events()) == 1
    finally:
        restored.close()


def test_decline_and_read_only_channel_check_do_not_change_business_state(cards):
    before = cards.list_cards()
    plan = cards.create_action_plan(
        "set_credit_limit",
        "decline-credit-limit",
        card_id="demo-card-credit-001",
        amount_minor=2_500_000,
    )
    with pytest.raises(BankError) as error:
        cards.decide_plan(plan["id"], "approve/decline")
    assert error.value.code == "INVALID_DECISION"
    assert cards.pending_for_conversation("decline-credit-limit")["id"] == plan["id"]
    result = cards.decide_plan(plan["id"], "decline")
    assert result["status"] == "declined"
    assert "未改变" in result["message"]
    assert cards.list_cards() == before

    application_count = len(cards.list_applications())
    event_count = len(cards.completed_events())
    raise_daily_limit = cards.create_action_plan(
        "set_daily_limit",
        "credit-daily-limit",
        card_id="demo-card-credit-001",
        amount_minor=2_000_000,
    )
    cards.decide_plan(raise_daily_limit["id"], "approve")
    channel_state = cards.list_cards()
    allowed = cards.authorize_charge("demo-card-credit-001", 100_000)
    too_large = cards.authorize_charge("demo-card-credit-001", 2_000_001)
    over_credit = cards.authorize_charge("demo-card-credit-001", 1_915_001)
    assert allowed["allowed"] is True
    assert too_large["allowed"] is False
    assert too_large["code"] == "DAILY_LIMIT_EXCEEDED"
    assert over_credit["allowed"] is False
    assert over_credit["code"] == "INSUFFICIENT_CREDIT"
    assert cards.list_cards() == channel_state
    assert len(cards.list_applications()) == application_count
    assert len(cards.completed_events()) == event_count + 1


def test_concurrent_confirmation_of_one_plan_returns_one_saved_operation(cards):
    plan = cards.create_action_plan("freeze", "one-plan-concurrency", card_id="demo-card-debit-001")
    barrier = Barrier(2)

    def approve() -> dict:
        barrier.wait(timeout=5)
        return cards.decide_plan(plan["id"], "approve")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(approve) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0] == results[1]
    assert results[0]["status"] == "completed"
    assert _card(cards)["version"] == 2
    assert len(cards.completed_events()) == 1
