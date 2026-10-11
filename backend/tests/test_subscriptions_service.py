from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest
from app.bank import BankError, BankService
from app.billing import BillingService
from app.subscriptions import SubscriptionService
from app.subscriptions.models import (
    RenewalNotification,
    SubscriptionActionPlan,
    SubscriptionContract,
)
from sqlalchemy import inspect, select


@pytest.fixture
def subscriptions(tmp_path):
    db_path = tmp_path / "subscriptions.sqlite3"
    service = SubscriptionService(f"sqlite:///{db_path.as_posix()}")
    service.seed_demo()
    yield service
    service.close()


def _entry(
    entry_id: str,
    merchant: str,
    event_date: str,
    amount_minor: int = -1_990,
    *,
    raw_merchant: str | None = None,
    transaction_kind: str = "expense",
) -> dict:
    return {
        "id": entry_id,
        "date": event_date,
        "merchant": merchant,
        "raw_merchant": raw_merchant or merchant,
        "amount_minor": amount_minor,
        "currency": "CNY",
        "account_id": "demo-checking-001",
        "category": "subscription",
        "transaction_kind": transaction_kind,
        "source": "demo",
        "confidence": 1.0,
        "reason": "synthetic test entry",
    }


def _contract(service: SubscriptionService, subscription_id: str) -> dict:
    return next(
        row
        for row in service.list_subscriptions([])["subscriptions"]
        if row["id"] == subscription_id
    )


def test_seed_is_idempotent_and_creates_only_the_exact_demo_contracts(subscriptions):
    first = subscriptions.list_subscriptions([])["subscriptions"]
    subscriptions.seed_demo()
    second = subscriptions.list_subscriptions([])["subscriptions"]

    assert {
        row["id"]: (row["merchant"], row["amount_minor"]) for row in second
    } == {
        "demo-sub-cloud": ("轻云存储", 1_800),
        "demo-sub-music": ("云舟音乐", 1_500),
        "demo-sub-video": ("星云视频", 2_390),
    }
    assert first == second
    assert all(row["source"] == "demo_contract" for row in second)
    assert next(row for row in second if row["id"] == "demo-sub-cloud")["cancel_supported"] is False
    assert all(row["cancel_supported"] for row in second if row["id"] != "demo-sub-cloud")


def test_subscription_tables_are_separate_from_the_bank_ledger(tmp_path):
    db_path = tmp_path / "isolated.sqlite3"
    service = SubscriptionService(f"sqlite:///{db_path.as_posix()}")
    try:
        service.seed_demo()
        names = set(inspect(service.engine).get_table_names())
        assert names == {
            "subscription_action_plans",
            "subscription_contracts",
            "subscription_renewal_notifications",
        }
        assert not any(name.startswith("bank_") for name in names)
    finally:
        service.close()


def test_detects_month_end_monthly_aliases_missing_cycles_and_price_changes(subscriptions):
    entries = [
        _entry("alias-1", "星云视频", "2026-01-15", -2_390, raw_merchant="星雲視頻VIP自动续费"),
        _entry("alias-2", "星云视频", "2026-02-15", -2_390, raw_merchant="Nebula Video"),
        _entry("alias-3", "星云视频", "2026-03-15", -2_390, raw_merchant="星云视频会员"),
        _entry("m-1", "影集Plus", "2026-01-31", -1_590),
        _entry("m-2", "影集Plus", "2026-02-28", -1_590),
        _entry("m-3", "影集Plus", "2026-03-31", -1_990),
        _entry("m-4", "影集Plus", "2026-05-31", -1_990),
        _entry("rent-1", "云栖公寓·月租", "2026-01-30", -2_200_000),
        _entry("rent-2", "云栖公寓·月租", "2026-02-28", -2_200_000),
        _entry("rent-3", "云栖公寓·月租", "2026-03-30", -2_200_000),
        _entry("refund", "RefundClub", "2026-04-01", 1_990, transaction_kind="refund"),
        _entry("transfer", "TransferClub", "2026-04-01", -1_990, transaction_kind="transfer"),
    ]

    candidates = subscriptions.detect(entries, as_of="2026-06-10")
    by_merchant = {row["merchant"]: row for row in candidates}

    assert "星云视频" not in by_merchant  # An existing contract is not duplicated as a candidate.
    assert by_merchant["影集Plus"]["cycle"] == "monthly"
    assert by_merchant["影集Plus"]["missing_cycles"] == 1
    assert by_merchant["影集Plus"]["price_change"] is True
    assert by_merchant["影集Plus"]["amount_minor"] == 1_990
    assert by_merchant["影集Plus"]["entry_ids"] == ["m-1", "m-2", "m-3", "m-4"]
    assert by_merchant["影集Plus"]["next_charge_on"] == "2026-06-30"
    assert by_merchant["影集Plus"]["is_subscription_candidate"] is True
    assert "尚未确认存在商户合同" in by_merchant["影集Plus"]["reason"]
    assert by_merchant["云栖公寓·月租"]["cycle"] == "monthly"
    assert by_merchant["云栖公寓·月租"]["is_subscription_candidate"] is False
    assert "周期性居住消费" in by_merchant["云栖公寓·月租"]["reason"]
    assert "RefundClub" not in by_merchant
    assert "TransferClub" not in by_merchant


def test_near_duplicate_charges_keep_all_evidence_without_weakening_weekly_detection(subscriptions):
    entries = [
        _entry("monthly-1", "近邻月费", "2026-07-12"),
        _entry("monthly-2", "近邻月费", "2026-08-12"),
        _entry("monthly-3a", "近邻月费", "2026-09-10"),
        _entry("monthly-3b", "近邻月费", "2026-09-11"),
        _entry("monthly-3c", "近邻月费", "2026-09-12"),
        _entry("weekly-1", "每周练习", "2026-09-01"),
        _entry("weekly-2", "每周练习", "2026-09-08"),
        _entry("weekly-3", "每周练习", "2026-09-15"),
    ]

    candidates = {
        row["merchant"]: row for row in subscriptions.detect(entries, as_of="2026-09-20")
    }

    monthly = candidates["近邻月费"]
    assert monthly["cycle"] == "monthly"
    assert monthly["entry_ids"] == [
        "monthly-1",
        "monthly-2",
        "monthly-3a",
        "monthly-3b",
        "monthly-3c",
    ]
    assert "仍保留全部流水证据" in monthly["reason"]
    assert candidates["每周练习"]["cycle"] == "weekly"
    assert candidates["每周练习"]["missing_cycles"] == 0


def test_live_billing_and_bank_fixture_keeps_monthly_rent_as_non_subscription(tmp_path):
    db_path = tmp_path / "billing-bank-subscriptions.sqlite3"
    database_url = f"sqlite:///{db_path.as_posix()}"
    bank = BankService(database_url)
    billing = BillingService(database_url)
    subscriptions = SubscriptionService(database_url)
    try:
        bank.seed()
        billing.seed_demo()
        subscriptions.seed_demo()
        entries = billing.entries()
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        candidates = subscriptions.detect(entries, as_of=today)
        by_merchant = {row["merchant"]: row for row in candidates}
        rent_rows = [
            row
            for row in entries
            if row["merchant"] == "云栖公寓" and row["transaction_kind"] == "expense"
        ]
        rent = by_merchant["云栖公寓"]

        assert len(rent_rows) == 7  # Six billing months plus the bank's recent rent entry.
        assert rent["cycle"] == "monthly"
        assert rent["is_subscription_candidate"] is False
        assert set(rent["entry_ids"]) == {row["id"] for row in rent_rows}
        assert {row["merchant"] for row in candidates if row["is_subscription_candidate"]} == {
            "白鹭阅读",
            "随手办公",
        }
        assert by_merchant["白鹭阅读"]["missing_cycles"] == 1
        assert by_merchant["随手办公"]["price_change"] is True
        assert by_merchant["随手办公"]["amount_minor"] == 1_590
    finally:
        subscriptions.close()
        billing.close()
        bank.dispose()


@pytest.mark.parametrize(
    ("merchant", "dates", "expected_cycle"),
    [
        ("每周冥想", ["2026-01-05", "2026-01-12", "2026-01-19"], "weekly"),
        ("季度杂志", ["2025-01-30", "2025-04-30", "2025-07-30"], "quarterly"),
        ("年度会员", ["2023-06-15", "2024-06-15", "2025-06-15"], "yearly"),
    ],
)
def test_detects_weekly_quarterly_and_yearly_patterns(subscriptions, merchant, dates, expected_cycle):
    candidates = subscriptions.detect(
        [_entry(f"{merchant}-{index}", merchant, event_date) for index, event_date in enumerate(dates)],
        as_of="2026-01-01",
    )

    assert len(candidates) == 1
    assert candidates[0]["cycle"] == expected_cycle
    assert candidates[0]["is_subscription_candidate"] is True


def test_list_keeps_contracts_and_inferred_candidates_in_separate_fields(subscriptions):
    result = subscriptions.list_subscriptions(
        [
            _entry("c-1", "晨间课程", "2026-01-02"),
            _entry("c-2", "晨间课程", "2026-01-09"),
            _entry("c-3", "晨间课程", "2026-01-16"),
        ],
        as_of="2026-01-20",
    )

    assert {row["id"] for row in result["subscriptions"]} == {
        "demo-sub-video",
        "demo-sub-music",
        "demo-sub-cloud",
    }
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["id"].startswith("candidate-")
    assert "id" not in result["candidates"][0] or result["candidates"][0]["id"] != "demo-sub-video"


def test_preview_and_decline_do_not_change_contract_or_autopay(subscriptions):
    before = _contract(subscriptions, "demo-sub-video")
    preview = subscriptions.create_action_plan(
        "demo-sub-video", "cancel_subscription", conversation_id="main"
    )

    assert preview["kind"] == "subscription"
    assert preview["action"] == "cancel_subscription"
    assert preview["status"] == "pending"
    assert preview["conversation_id"] == "main"
    assert preview["expires_at"]
    assert "模拟商户侧取消" in preview["description"]
    assert "cancelled" not in preview["description"]
    assert _contract(subscriptions, "demo-sub-video") == before

    result = subscriptions.decide_plan(preview["id"], "decline")
    assert result["status"] == "declined"
    assert "均未改变" in result["message"]
    assert subscriptions.decide_plan(preview["id"], "decline") == result
    assert _contract(subscriptions, "demo-sub-video") == before


def test_cancel_and_stop_autopay_have_distinct_persisted_effects(subscriptions):
    cancel = subscriptions.create_action_plan("demo-sub-video", "cancel_subscription")
    cancel_result = subscriptions.decide_plan(cancel["id"], "approve")
    assert cancel_result["status"] == "completed"
    assert cancel_result["operation_id"]
    assert subscriptions.decide_plan(cancel["id"], "approve") == cancel_result
    video = _contract(subscriptions, "demo-sub-video")
    assert video["status"] == "cancelled"
    assert video["autopay_enabled"] is False

    stop = subscriptions.create_action_plan("demo-sub-music", "stop_autopay")
    assert "不会因此取消" in stop["description"]
    stop_result = subscriptions.decide_plan(stop["id"], "approve")
    assert stop_result["status"] == "completed"
    music = _contract(subscriptions, "demo-sub-music")
    assert music["status"] == "active"
    assert music["autopay_enabled"] is False
    assert "商户服务并未取消" in stop_result["message"]


def test_cloud_cancellation_is_unsupported_but_separate_stop_autopay_requires_confirmation(
    subscriptions,
):
    with pytest.raises(BankError) as error:
        subscriptions.create_action_plan("demo-sub-cloud", "cancel_subscription", "cloud-chat")

    assert error.value.code == "CANCEL_NOT_SUPPORTED"
    assert "单独预览并确认“停止代扣”" in error.value.message
    assert subscriptions.pending_for_conversation("cloud-chat") is None
    cloud = _contract(subscriptions, "demo-sub-cloud")
    assert cloud["status"] == "active"
    assert cloud["autopay_enabled"] is True

    preview = subscriptions.create_action_plan("demo-sub-cloud", "stop_autopay", "cloud-chat")
    assert preview["action"] == "stop_autopay"
    assert _contract(subscriptions, "demo-sub-cloud")["autopay_enabled"] is True
    result = subscriptions.decide_plan(preview["id"], "approve")
    assert result["status"] == "completed"
    assert _contract(subscriptions, "demo-sub-cloud")["status"] == "active"
    assert _contract(subscriptions, "demo-sub-cloud")["autopay_enabled"] is False


def test_action_plans_expire_idempotently_and_conversation_invalidation_is_scoped(subscriptions):
    expired = subscriptions.create_action_plan("demo-sub-video", "stop_autopay", "expired-chat")
    with subscriptions._session_factory.begin() as session:
        row = session.get(SubscriptionActionPlan, expired["id"])
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    first = subscriptions.decide_plan(expired["id"], "approve")
    assert first["status"] == "declined"
    assert first["code"] == "PLAN_EXPIRED"
    assert subscriptions.decide_plan(expired["id"], "approve") == first
    with pytest.raises(BankError) as error:
        subscriptions.decide_plan(expired["id"], "decline")
    assert error.value.code == "PLAN_ALREADY_DECIDED"
    assert _contract(subscriptions, "demo-sub-video")["autopay_enabled"] is True

    old = subscriptions.create_action_plan("demo-sub-music", "stop_autopay", "same-chat")
    other = subscriptions.create_action_plan("demo-sub-cloud", "stop_autopay", "other-chat")
    new = subscriptions.create_action_plan("demo-sub-music", "stop_autopay", "same-chat")
    assert subscriptions.get_plan(old["id"])["status"] == "invalidated"
    assert subscriptions.pending_for_conversation("same-chat")["id"] == new["id"]
    assert subscriptions.pending_for_conversation("other-chat")["id"] == other["id"]
    with pytest.raises(BankError) as error:
        subscriptions.decide_plan(old["id"], "approve")
    assert error.value.code == "PLAN_INVALIDATED"
    assert subscriptions.invalidate_pending_plans("same-chat") == 1
    assert subscriptions.pending_for_conversation("same-chat") is None
    assert subscriptions.pending_for_conversation("other-chat")["id"] == other["id"]


def test_pending_action_can_be_recovered_after_service_restart(tmp_path):
    db_path = tmp_path / "recover.sqlite3"
    database_url = f"sqlite:///{db_path.as_posix()}"
    first = SubscriptionService(database_url)
    first.seed_demo()
    preview = first.create_action_plan("demo-sub-video", "stop_autopay", "restart-chat")
    first.close()

    restarted = SubscriptionService(database_url)
    try:
        assert restarted.get_plan(preview["id"]) == preview
        assert restarted.pending_for_conversation("restart-chat") == preview
        result = restarted.decide_plan(preview["id"], "approve")
        assert result["status"] == "completed"
        assert restarted.decide_plan(preview["id"], "approve") == result
        restarted.seed_demo()
        assert _contract(restarted, "demo-sub-video")["autopay_enabled"] is False
        events = restarted.completed_events()
        assert len(events) == 1
        event = events[0]
        assert event["id"] == f"subscription:{result['operation_id']}"
        assert event["kind"] == "subscription"
        assert event["source"] == "subscriptions"
        assert event["plan_id"] == preview["id"]
        assert event["conversation_id"] == "restart-chat"
        assert event["operation_id"] == result["operation_id"]
        assert event["receipt"]["status"] == "completed"
        assert event["receipt"]["subscription_id"] == "demo-sub-video"
        assert restarted.completed_events() == events
    finally:
        restarted.close()


def test_confirmation_is_serialized_across_service_instances(tmp_path):
    db_path = tmp_path / "concurrent.sqlite3"
    database_url = f"sqlite:///{db_path.as_posix()}"
    first = SubscriptionService(database_url)
    first.seed_demo()
    second = SubscriptionService(database_url)
    preview = first.create_action_plan("demo-sub-video", "cancel_subscription")
    barrier = Barrier(2)

    def confirm(service: SubscriptionService) -> dict:
        barrier.wait(timeout=5)
        return service.decide_plan(preview["id"], "approve")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(confirm, (first, second)))
        assert results[0] == results[1]
        assert results[0]["status"] == "completed"
        assert results[0]["operation_id"]
        assert _contract(first, "demo-sub-video")["status"] == "cancelled"
    finally:
        second.close()
        first.close()


def test_renewal_notifications_are_due_only_once_and_survive_restart(tmp_path):
    db_path = tmp_path / "notifications.sqlite3"
    database_url = f"sqlite:///{db_path.as_posix()}"
    service = SubscriptionService(database_url)
    service.seed_demo()
    as_of = date(2026, 6, 1)
    due = as_of + timedelta(days=3)
    with service._session_factory.begin() as session:
        session.get(SubscriptionContract, "demo-sub-video").next_charge_on = due
        session.get(SubscriptionContract, "demo-sub-music").next_charge_on = due
        session.get(SubscriptionContract, "demo-sub-cloud").next_charge_on = due

    cancel = service.create_action_plan("demo-sub-video", "cancel_subscription")
    service.decide_plan(cancel["id"], "approve")
    stop = service.create_action_plan("demo-sub-music", "stop_autopay")
    service.decide_plan(stop["id"], "approve")

    first = service.renewal_notifications([], as_of=as_of, window_days=7)
    assert len(first) == 1
    assert first[0]["subscription_id"] == "demo-sub-cloud"
    assert first[0]["kind"] == "renewal"
    assert first[0]["source"] == "demo_contract"
    assert first[0]["next_charge_on"] == due.isoformat()
    assert "不会自动扣款" in first[0]["text"]
    assert service.renewal_notifications([], as_of=as_of, window_days=7) == []
    durable = service.persisted_notifications()
    assert durable == first
    service.close()

    restarted = SubscriptionService(database_url)
    try:
        restarted.seed_demo()
        assert restarted.renewal_notifications([], as_of=as_of, window_days=7) == []
        assert restarted.persisted_notifications() == first
        with restarted._session_factory() as session:
            rows = session.scalars(select(RenewalNotification)).all()
            assert len(rows) == 1
    finally:
        restarted.close()


def test_invalid_actions_unknown_plans_and_invalid_windows_are_business_errors(subscriptions):
    with pytest.raises(BankError) as error:
        subscriptions.create_action_plan("demo-sub-video", "refund")
    assert error.value.code == "INVALID_ACTION"
    assert subscriptions.get_plan("missing") is None
    with pytest.raises(BankError) as error:
        subscriptions.decide_plan("missing", "approve")
    assert error.value.code == "PLAN_NOT_FOUND"
    with pytest.raises(BankError) as error:
        subscriptions.renewal_notifications([], window_days=-1)
    assert error.value.code == "INVALID_WINDOW"
