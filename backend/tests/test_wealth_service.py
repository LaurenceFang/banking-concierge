from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Barrier

import pytest
from app.bank import BankError, BankService
from app.bank.models import Account, LedgerTransaction
from app.wealth import WealthService
from app.wealth.models import (
    WealthActionPlan,
    WealthPosition,
    WealthSimulationClock,
)
from sqlalchemy import func, select

R3_ANSWERS = {
    "horizon": "medium",
    "loss_tolerance": "moderate",
    "experience": "basic",
    "liquidity_need": "medium",
    "objective": "balanced",
}
R5_ANSWERS = {
    "horizon": "long",
    "loss_tolerance": "high",
    "experience": "experienced",
    "liquidity_need": "low",
    "objective": "growth",
}


@pytest.fixture
def services(tmp_path):
    url = f"sqlite:///{(tmp_path / 'wealth.sqlite3').as_posix()}"
    bank = BankService(url)
    bank.seed()
    wealth = WealthService(url)
    wealth.seed_demo()
    yield bank, wealth
    wealth.close()
    bank.dispose()


def _balance(bank: BankService, account_id: str = "demo-checking-001") -> int:
    return next(row["balance_minor"] for row in bank.list_accounts() if row["id"] == account_id)


def _profile_r3(wealth: WealthService) -> dict:
    return wealth.set_risk_profile(dict(R3_ANSWERS))


def _purchase(
    wealth: WealthService,
    *,
    amount: int = 100_000,
    product_id: str = "demo-wealth-flex",
    conversation: str = "purchase",
) -> dict:
    return wealth.create_action_plan("purchase", conversation, product_id=product_id, amount_minor=amount)


def test_products_require_a_complete_risk_profile_and_enforce_suitability(services):
    _bank, wealth = services

    assert len(wealth.list_products()) == 3
    assert all(product["eligible"] is False for product in wealth.list_products())
    assert wealth.get_risk_profile() is None
    with pytest.raises(BankError) as missing:
        _purchase(wealth)
    assert missing.value.code == "RISK_PROFILE_REQUIRED"

    with pytest.raises(BankError) as invalid:
        wealth.set_risk_profile({**R3_ANSWERS, "objective": ""})
    assert invalid.value.code == "INVALID_RISK_PROFILE"
    with pytest.raises(BankError) as incomplete:
        wealth.set_risk_profile({"horizon": "short"})
    assert incomplete.value.code == "INVALID_RISK_PROFILE"
    assert wealth.get_risk_profile() is None

    profile = _profile_r3(wealth)
    assert profile["risk_level"] == 3
    assert profile["risk_label"] == "R3"
    assert "收益预测" in profile["explanation"]
    products = {product["id"]: product for product in wealth.list_products()}
    assert products["demo-wealth-flex"]["eligible"] is True
    assert products["demo-wealth-balance"]["eligible"] is True
    assert products["demo-wealth-growth"]["eligible"] is False
    with pytest.raises(BankError) as foreign_account:
        wealth.create_action_plan(
            "purchase",
            "foreign-account",
            product_id="demo-wealth-flex",
            account_id="demo-destination-chen",
            amount_minor=100_000,
        )
    assert foreign_account.value.code == "ACCOUNT_NOT_FOUND"
    with pytest.raises(BankError) as unsuitable:
        _purchase(wealth, amount=1_000_000, product_id="demo-wealth-growth")
    assert unsuitable.value.code == "RISK_PROFILE_INSUFFICIENT"

    before = wealth.get_risk_profile()
    with pytest.raises(BankError):
        wealth.set_risk_profile({**R3_ANSWERS, "extra": "unknown"})
    assert wealth.get_risk_profile() == before


def test_purchase_debits_cash_once_and_pending_order_is_not_a_position(services):
    bank, wealth = services
    _profile_r3(wealth)
    # 500,500 * 30 bps is 1,501.5 minor units, rounded half-up to 1,502.
    plan = _purchase(
        wealth,
        amount=500_500,
        product_id="demo-wealth-balance",
        conversation="purchase-atomic",
    )
    assert plan["fee_minor"] == 1_502
    assert plan["total_minor"] == 502_002
    assert plan["payout_minor"] == 0
    assert plan["settles_on"] > wealth.current_date()

    before = _balance(bank)
    result = wealth.decide_plan(plan["id"], "approve", conversation_id="purchase-atomic")
    retry = wealth.decide_plan(plan["id"], "approve", conversation_id="purchase-atomic")
    assert result == retry
    assert result["status"] == "completed"
    assert result["order_status"] == "pending_settlement"
    assert result["order_id"]
    assert "尚未" in result["message"] or "结算前" in result["message"]
    assert _balance(bank) == before - 502_002
    assert wealth.list_positions() == []
    order = wealth.get_order(result["order_id"])
    assert order is not None
    assert order["status"] == "pending_settlement"
    assert order["position_id"] is None

    with wealth._session_factory() as session:
        clearing = session.get(Account, "wealth-clearing-account")
        assert clearing is not None
        assert clearing.owner_id != "demo-user-001"
        assert clearing.balance_minor == 502_002
        source = session.get(Account, "demo-checking-001")
        assert source.balance_minor + clearing.balance_minor == before
        rows = session.scalars(
            select(LedgerTransaction).where(LedgerTransaction.operation_id == result["operation_id"])
        ).all()
        assert sorted(row.amount_minor for row in rows) == [-500_500, -1_502, 1_502, 500_500]
        assert sum(row.amount_minor for row in rows) == 0
        assert {row.description for row in rows} == {
            "理财申购本金·平衡180计划 R3",
            "理财申购手续费·平衡180计划 R3",
        }
    assert {row["id"] for row in bank.list_accounts()} == {
        "demo-checking-001",
        "demo-savings-001",
    }


def test_settlement_clock_survives_restart_and_repeated_processing_is_idempotent(services):
    _bank, wealth = services
    _profile_r3(wealth)
    purchase = _purchase(wealth, amount=123_456, conversation="clock")
    accepted = wealth.decide_plan(purchase["id"], "approve")
    assert accepted["order_status"] == "pending_settlement"
    assert wealth.list_positions() == []

    database_url = wealth.database_url
    current = wealth.current_date()
    wealth.close()
    restarted = WealthService(database_url)
    try:
        restarted.seed_demo()
        assert restarted.current_date() == current
        assert restarted.get_order(accepted["order_id"])["status"] == "pending_settlement"
        advance = restarted.advance_demo_clock(1)
        assert advance["settled_orders"][0]["id"] == accepted["order_id"]
        assert advance["settled_orders"][0]["status"] == "settled"
        assert restarted.list_positions()[0]["principal_minor"] == 123_456
        assert restarted.process_due() == []
        events = restarted.completed_events()
        assert [event["id"] for event in events] == [
            f"wealth:{accepted['operation_id']}",
            f"wealth-settlement:{accepted['order_id']}",
        ]
        final_event = events[1]
        assert final_event["plan_id"] == purchase["id"]
        assert final_event["receipt"]["order_status"] == "settled"
        assert "持仓已入账" in final_event["receipt"]["message"]
        assert restarted.completed_events() == events
    finally:
        restarted.close()


def test_lock_starts_on_settlement_date_and_redeem_credits_only_when_due(services):
    bank, wealth = services
    _profile_r3(wealth)
    with wealth._transaction() as session:
        clock = session.get(WealthSimulationClock, "demo")
        clock.current_date = date(2026, 10, 9)  # Friday; T+1 is Monday.

    purchase = _purchase(
        wealth,
        amount=500_000,
        product_id="demo-wealth-balance",
        conversation="locked-purchase",
    )
    assert purchase["settles_on"] == "2026-10-12"
    before = _balance(bank)
    receipt = wealth.decide_plan(purchase["id"], "approve")
    assert receipt["status"] == "completed"
    assert _balance(bank) == before - 501_500
    assert wealth.list_positions() == []

    wealth.advance_demo_clock(1)
    position = wealth.list_positions()[0]
    with wealth._session_factory() as session:
        row = session.get(WealthPosition, position["id"])
        assert row.settlement_date == date(2026, 10, 12)
        assert row.unlock_on == date(2026, 10, 12) + timedelta(days=180)
        clock = session.get(WealthSimulationClock, "demo")
        clock.current_date = row.unlock_on - timedelta(days=1)
    with pytest.raises(BankError) as locked:
        wealth.create_action_plan(
            "redeem",
            "too-early",
            product_id="demo-wealth-balance",
            position_id=position["id"],
            amount_minor=500,
        )
    assert locked.value.code == "LOCK_PERIOD_ACTIVE"

    with wealth._transaction() as session:
        session.get(WealthSimulationClock, "demo").current_date = date.fromisoformat(position["unlock_on"])
    redeem = wealth.create_action_plan(
        "redeem",
        "redemption",
        product_id="demo-wealth-balance",
        position_id=position["id"],
        amount_minor=500,
    )
    assert redeem["fee_minor"] == 1  # 500 * 10 bps = half a fen, rounded half-up.
    assert redeem["total_minor"] == 500
    assert redeem["payout_minor"] == 499
    before_redeem = _balance(bank)
    with wealth._session_factory() as session:
        clearing_before_redeem = session.get(Account, "wealth-clearing-account").balance_minor
    accepted = wealth.decide_plan(redeem["id"], "approve")
    assert accepted["order_status"] == "pending_settlement"
    assert _balance(bank) == before_redeem
    held = wealth.list_positions()[0]
    assert held["principal_minor"] == 500_000
    assert held["available_minor"] == 499_500
    assert held["redemption_pending_minor"] == 500

    settled = wealth.advance_demo_clock(1)["settled_orders"]
    assert len(settled) == 1
    assert settled[0]["id"] == accepted["order_id"]
    assert _balance(bank) == before_redeem + 499
    with wealth._session_factory() as session:
        clearing = session.get(Account, "wealth-clearing-account")
        assert _balance(bank) + clearing.balance_minor == before_redeem + clearing_before_redeem
        rows = session.scalars(
            select(LedgerTransaction).where(LedgerTransaction.operation_id == accepted["operation_id"])
        ).all()
        assert sorted(row.amount_minor for row in rows) == [-500, -1, 1, 500]
        assert sum(row.amount_minor for row in rows) == 0
        assert {row.description for row in rows} == {
            "理财赎回本金·平衡180计划 R3",
            "理财赎回手续费·平衡180计划 R3",
        }
    held_after = wealth.list_positions()[0]
    assert held_after["principal_minor"] == 499_500
    assert held_after["available_minor"] == 499_500
    assert held_after["redemption_pending_minor"] == 0
    final = wealth.completed_events()[-1]
    assert final["id"] == f"wealth-settlement:{accepted['order_id']}"
    assert final["receipt"]["order_status"] == "settled"
    assert "赎回款已到账" in final["text"]


def test_profile_or_product_rule_change_after_preview_declines_without_effect(services):
    bank, wealth = services
    _profile_r3(wealth)
    plan = _purchase(wealth, conversation="risk-stale")
    before = _balance(bank)
    wealth.set_risk_profile(dict(R3_ANSWERS))
    result = wealth.decide_plan(plan["id"], "approve")
    assert result["status"] == "declined"
    assert result["code"] == "RISK_PROFILE_CHANGED"
    assert _balance(bank) == before
    assert wealth.list_orders() == []

    plan = _purchase(wealth, conversation="product-stale")
    with wealth._transaction() as session:
        from app.wealth.models import WealthProduct

        session.get(WealthProduct, "demo-wealth-flex").purchase_fee_bps = 1
    result = wealth.decide_plan(plan["id"], "approve")
    assert result["status"] == "declined"
    assert result["code"] == "PRODUCT_RULE_CHANGED"
    assert _balance(bank) == before
    assert wealth.list_orders() == []


def test_confirmation_rechecks_balance_and_rolls_back_all_writes_on_ledger_failure(services, monkeypatch):
    bank, wealth = services
    _profile_r3(wealth)
    plan = _purchase(wealth, amount=1_000_000, conversation="balance-race")
    with wealth._transaction() as session:
        source = session.get(Account, "demo-checking-001")
        source.balance_minor = 500_000
    result = wealth.decide_plan(plan["id"], "approve")
    assert result["status"] == "declined"
    assert result["code"] == "INSUFFICIENT_BALANCE"
    assert _balance(bank) == 500_000
    assert wealth.list_orders() == []

    with wealth._transaction() as session:
        session.get(Account, "demo-checking-001").balance_minor = 9_100_567
    fresh = _purchase(wealth, amount=100_000, conversation="rollback")
    clear_before = None
    with wealth._session_factory() as session:
        clear_before = session.get(Account, "wealth-clearing-account").balance_minor

    def fail_after_balance_changes(*_args, **_kwargs):
        raise RuntimeError("forced ledger insert failure")

    monkeypatch.setattr(wealth, "_write_ledger_pair", fail_after_balance_changes)
    with pytest.raises(RuntimeError, match="forced ledger insert failure"):
        wealth.decide_plan(fresh["id"], "approve")
    assert _balance(bank) == 9_100_567
    assert wealth.list_orders() == []
    assert wealth.list_positions() == []
    with wealth._session_factory() as session:
        assert session.get(Account, "wealth-clearing-account").balance_minor == clear_before
        assert (
            session.scalar(
                select(func.count(LedgerTransaction.id)).where(
                    LedgerTransaction.operation_id.like("wealth-op-%")
                )
            )
            == 0
        )
        pending = session.get(WealthActionPlan, fresh["id"])
        assert pending.status == "pending"


def test_concurrent_approvals_serialize_balance_and_available_shares(services):
    bank, wealth = services
    _profile_r3(wealth)
    first = _purchase(wealth, amount=5_000_000, conversation="balance-one")
    second = _purchase(wealth, amount=5_000_000, conversation="balance-two")
    barrier = Barrier(2)

    def approve(plan_id: str) -> dict:
        barrier.wait(timeout=5)
        return wealth.decide_plan(plan_id, "approve")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(approve, first["id"]), pool.submit(approve, second["id"])]
        outcomes = [future.result(timeout=10) for future in futures]
    assert sorted(row["status"] for row in outcomes) == ["completed", "declined"]
    assert _balance(bank) == 9_100_567 - 5_000_000

    # Seed a settled, liquid position, then race two redemptions against its shares.
    liquid = _purchase(wealth, amount=600_000, conversation="shares-purchase")
    accepted = wealth.decide_plan(liquid["id"], "approve")
    wealth.advance_demo_clock(1)
    position = next(row for row in wealth.list_positions() if row["principal_minor"] == 600_000)
    redeem_one = wealth.create_action_plan(
        "redeem",
        "shares-one",
        product_id=position["product_id"],
        position_id=position["id"],
        amount_minor=400_000,
    )
    redeem_two = wealth.create_action_plan(
        "redeem",
        "shares-two",
        product_id=position["product_id"],
        position_id=position["id"],
        amount_minor=400_000,
    )
    share_barrier = Barrier(2)

    def redeem(plan_id: str) -> dict:
        share_barrier.wait(timeout=5)
        return wealth.decide_plan(plan_id, "approve")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(redeem, redeem_one["id"]), pool.submit(redeem, redeem_two["id"])]
        redemption_outcomes = [future.result(timeout=10) for future in futures]
    assert sorted(row["status"] for row in redemption_outcomes) == ["completed", "declined"]
    current = next(row for row in wealth.list_positions() if row["id"] == position["id"])
    assert current["principal_minor"] == 600_000
    assert current["available_minor"] == 200_000
    assert current["redemption_pending_minor"] == 400_000
    assert wealth.get_order(accepted["order_id"])["status"] == "settled"
