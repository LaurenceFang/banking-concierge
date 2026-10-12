"""PostgreSQL locking/atomicity checks for the wealth service.

These tests only connect to the dedicated local concierge_test database. They
never drop bank tables: fixture cleanup is scoped to wealth tables and bank
accounts/transactions introduced by this fixture.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from app.bank import BankError
from app.bank.models import Account, LedgerTransaction, User
from app.wealth import WealthService
from app.wealth.models import (
    WealthActionPlan,
    WealthOrder,
    WealthPosition,
    WealthProduct,
    WealthRiskProfile,
    WealthSimulationClock,
)
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url

TEST_DATABASE_ENV = "BANK_TEST_DATABASE_URL"
R3_ANSWERS = {
    "horizon": "medium",
    "loss_tolerance": "moderate",
    "experience": "basic",
    "liquidity_need": "medium",
    "objective": "balanced",
}


@pytest.fixture
def postgres_wealth() -> Iterator[WealthService]:
    raw_url = os.environ.get(TEST_DATABASE_ENV)
    if not raw_url:
        pytest.skip(f"set {TEST_DATABASE_ENV} to run isolated PostgreSQL wealth tests")
    url = make_url(raw_url)
    if not (
        url.drivername == "postgresql+psycopg"
        and url.host == "127.0.0.1"
        and url.port == 55432
        and url.database == "concierge_test"
        and url.password == "local-demo-only"
    ):
        pytest.fail(
            "Refusing PostgreSQL wealth test outside 127.0.0.1:55432/concierge_test "
            "with the local demo-only credential."
        )

    service = WealthService(raw_url)
    original_users: set[str]
    original_accounts: dict[str, dict[str, object]]
    original_transaction_ids: set[str]
    with service._session_factory() as session:
        original_users = set(session.scalars(select(User.id)).all())
        original_accounts = {
            row.id: {
                "owner_id": row.owner_id,
                "name": row.name,
                "kind": row.kind,
                "balance_minor": row.balance_minor,
                "currency": row.currency,
            }
            for row in session.scalars(
                select(Account).where(Account.id.in_({"demo-checking-001", "wealth-clearing-account"}))
            ).all()
        }
        original_transaction_ids = set(session.scalars(select(LedgerTransaction.id)).all())

    try:
        service.seed_demo()
        with service._transaction() as session:
            source = session.get(Account, "demo-checking-001")
            if source is None:
                session.add(
                    Account(
                        id="demo-checking-001",
                        owner_id="demo-user-001",
                        name="理财测试付款账户",
                        kind="checking",
                        balance_minor=20_000_000,
                        currency="CNY",
                    )
                )
            elif source.owner_id != "demo-user-001" or source.currency != "CNY":
                pytest.fail("demo-checking-001 is not a CNY account owned by demo-user-001")
            else:
                source.balance_minor = 20_000_000
            clearing = session.get(Account, "wealth-clearing-account")
            if clearing is None or clearing.owner_id != "wealth-clearing-owner":
                pytest.fail("wealth-clearing-account is missing or has an unexpected owner")
            clearing.balance_minor = 0
            session.flush()
        _clear_wealth_namespace(service)
        service.seed_demo()
        yield service
    finally:
        try:
            _clear_wealth_namespace(service)
            with service._transaction() as session:
                new_transactions = session.scalars(
                    select(LedgerTransaction).where(
                        LedgerTransaction.id.not_in(original_transaction_ids),
                        LedgerTransaction.operation_id.like("wealth-op-%"),
                        LedgerTransaction.account_id.in_({"demo-checking-001", "wealth-clearing-account"}),
                    )
                ).all()
                for row in new_transactions:
                    session.delete(row)

                for account_id in ("demo-checking-001", "wealth-clearing-account"):
                    account = session.get(Account, account_id)
                    snapshot = original_accounts.get(account_id)
                    if snapshot is None:
                        if account is not None:
                            session.delete(account)
                    elif account is not None:
                        for field, value in snapshot.items():
                            setattr(account, field, value)

                for user_id in {"demo-user-001", "wealth-clearing-owner"} - original_users:
                    user = session.get(User, user_id)
                    if user is not None:
                        session.delete(user)
        finally:
            service.close()


def _clear_wealth_namespace(service: WealthService) -> None:
    with service._transaction() as session:
        session.execute(delete(WealthOrder).where(WealthOrder.owner_id == "demo-user-001"))
        session.execute(delete(WealthActionPlan).where(WealthActionPlan.owner_id == "demo-user-001"))
        session.execute(delete(WealthPosition).where(WealthPosition.owner_id == "demo-user-001"))
        session.execute(delete(WealthRiskProfile).where(WealthRiskProfile.owner_id == "demo-user-001"))
        session.execute(
            delete(WealthProduct).where(
                WealthProduct.id.in_({"demo-wealth-flex", "demo-wealth-balance", "demo-wealth-growth"})
            )
        )
        session.execute(delete(WealthSimulationClock).where(WealthSimulationClock.id == "demo"))


def _balance(service: WealthService, account_id: str) -> int:
    with service._session_factory() as session:
        return session.get(Account, account_id).balance_minor


def _purchase(service: WealthService, amount: int, conversation: str) -> dict:
    return service.create_action_plan(
        "purchase",
        conversation,
        product_id="demo-wealth-flex",
        amount_minor=amount,
    )


def test_postgres_rechecks_competing_cash_and_repeats_settlement_safely(
    postgres_wealth: WealthService,
):
    service = postgres_wealth
    with pytest.raises(BankError) as missing_profile:
        _purchase(service, 10_000_000, "pg-no-profile")
    assert missing_profile.value.code == "RISK_PROFILE_REQUIRED"
    assert service.list_orders() == []
    service.set_risk_profile(dict(R3_ANSWERS))

    source_before = _balance(service, "demo-checking-001")
    clearing_before = _balance(service, "wealth-clearing-account")
    first = _purchase(service, 12_000_000, "pg-cash-one")
    second = _purchase(service, 12_000_000, "pg-cash-two")
    barrier = Barrier(2)

    def approve(plan_id: str) -> dict:
        barrier.wait(timeout=5)
        return service.decide_plan(plan_id, "approve")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(approve, first["id"]), pool.submit(approve, second["id"])]
        outcomes = [future.result(timeout=10) for future in futures]

    assert sorted(row["status"] for row in outcomes) == ["completed", "declined"]
    approved_plan_id, approved = next(
        (plan["id"], outcome)
        for plan, outcome in zip((first, second), outcomes, strict=True)
        if outcome["status"] == "completed"
    )
    assert service.decide_plan(approved_plan_id, "approve") == approved
    assert _balance(service, "demo-checking-001") == source_before - 12_000_000
    assert _balance(service, "wealth-clearing-account") == clearing_before + 12_000_000
    assert (
        _balance(service, "demo-checking-001") + _balance(service, "wealth-clearing-account")
        == source_before + clearing_before
    )

    url = service.database_url
    date_before_restart = service.current_date()
    service.close()
    restarted = WealthService(url)
    try:
        restarted.seed_demo()
        assert restarted.current_date() == date_before_restart
        assert restarted.get_order(approved["order_id"])["status"] == "pending_settlement"
        settled = restarted.advance_demo_clock(1)["settled_orders"]
        assert [row["id"] for row in settled] == [approved["order_id"]]
        assert restarted.process_due() == []
        position = restarted.list_positions()[0]
        assert position["principal_minor"] == 12_000_000
        events = restarted.completed_events()
        assert f"wealth-settlement:{approved['order_id']}" in {event["id"] for event in events}
        assert restarted.completed_events() == events
    finally:
        restarted.close()


def test_postgres_competing_redemptions_reserve_each_share_once(postgres_wealth: WealthService):
    service = postgres_wealth
    service.set_risk_profile(dict(R3_ANSWERS))
    purchase = _purchase(service, 1_000_000, "pg-shares-buy")
    accepted = service.decide_plan(purchase["id"], "approve")
    service.advance_demo_clock(1)
    position = service.list_positions()[0]
    cash_before = _balance(service, "demo-checking-001")
    first = service.create_action_plan(
        "redeem",
        "pg-shares-one",
        product_id=position["product_id"],
        position_id=position["id"],
        amount_minor=750_000,
    )
    second = service.create_action_plan(
        "redeem",
        "pg-shares-two",
        product_id=position["product_id"],
        position_id=position["id"],
        amount_minor=750_000,
    )
    barrier = Barrier(2)

    def redeem(plan_id: str) -> dict:
        barrier.wait(timeout=5)
        return service.decide_plan(plan_id, "approve")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(redeem, first["id"]), pool.submit(redeem, second["id"])]
        outcomes = [future.result(timeout=10) for future in futures]
    assert sorted(row["status"] for row in outcomes) == ["completed", "declined"]
    assert _balance(service, "demo-checking-001") == cash_before
    reserved = next(row for row in service.list_positions() if row["id"] == position["id"])
    assert reserved["principal_minor"] == 1_000_000
    assert reserved["available_minor"] == 250_000
    assert reserved["redemption_pending_minor"] == 750_000

    settled = service.advance_demo_clock(1)["settled_orders"]
    assert len(settled) == 1
    assert settled[0]["action"] == "redeem"
    assert _balance(service, "demo-checking-001") == cash_before + 750_000
    assert service.process_due() == []
    with service._session_factory() as session:
        purchase_order = session.get(WealthOrder, accepted["order_id"])
        assert purchase_order is not None and purchase_order.status == "settled"


def test_postgres_fee_legs_and_failed_fee_write_rollback_together(
    postgres_wealth: WealthService, monkeypatch: pytest.MonkeyPatch
):
    service = postgres_wealth
    service.set_risk_profile(dict(R3_ANSWERS))
    plan = service.create_action_plan(
        "purchase",
        "pg-fee-rollback",
        product_id="demo-wealth-balance",
        amount_minor=500_500,
    )
    source_before = _balance(service, "demo-checking-001")
    clearing_before = _balance(service, "wealth-clearing-account")
    original_writer = service._write_ledger_pair

    def fail_on_fee(*args, **kwargs):
        if kwargs.get("leg") == "fee":
            raise RuntimeError("forced PostgreSQL fee-leg failure")
        return original_writer(*args, **kwargs)

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(service, "_write_ledger_pair", fail_on_fee)
        with pytest.raises(RuntimeError, match="forced PostgreSQL fee-leg failure"):
            service.decide_plan(plan["id"], "approve")

    assert _balance(service, "demo-checking-001") == source_before
    assert _balance(service, "wealth-clearing-account") == clearing_before
    assert service.list_orders() == []
    with service._session_factory() as session:
        assert (
            session.scalar(
                select(LedgerTransaction.id)
                .where(LedgerTransaction.operation_id.like("wealth-op-%"))
                .limit(1)
            )
            is None
        )
        assert session.get(WealthActionPlan, plan["id"]).status == "pending"

    receipt = service.decide_plan(plan["id"], "approve")
    assert receipt["status"] == "completed"
    assert _balance(service, "demo-checking-001") == source_before - 502_002
    assert _balance(service, "wealth-clearing-account") == clearing_before + 502_002
    with service._session_factory() as session:
        rows = session.scalars(
            select(LedgerTransaction).where(LedgerTransaction.operation_id == receipt["operation_id"])
        ).all()
    assert len(rows) == 4
    assert sum(row.amount_minor for row in rows) == 0
    assert sorted(row.amount_minor for row in rows) == [-500_500, -1_502, 1_502, 500_500]
    assert {row.description for row in rows} == {
        "理财申购本金·平衡180计划 R3",
        "理财申购手续费·平衡180计划 R3",
    }
