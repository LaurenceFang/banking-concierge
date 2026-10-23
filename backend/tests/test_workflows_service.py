from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from app.bank.errors import BankError
from app.bank.models import Account, FundHold, LedgerTransaction
from app.bank.service import BankService
from app.task_clock import TaskClock
from app.workflows import WorkflowService
from app.workflows.models import WorkflowCatalogItem, WorkflowMerchant, WorkflowMerchantOrder
from sqlalchemy import select


@pytest.fixture
def scenario(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'workflows.sqlite3').as_posix()}"
    bank = BankService(database_url)
    bank.seed()
    clock = TaskClock(database_url)
    workflow = WorkflowService(database_url)
    workflow.seed_demo()
    yield bank, clock, workflow, database_url
    workflow.close()
    clock.close()
    bank.dispose()


def _balance(workflow: WorkflowService, account_id: str) -> int:
    with workflow._session_factory() as session:
        return session.get(Account, account_id).balance_minor


def _plan(
    workflow: WorkflowService,
    *,
    conversation: str = "birthday",
    items: list[dict] | None = None,
    budget: int = 40_000,
    execute_at=None,
    action: str = "arrange",
    task_id: str | None = None,
):
    return workflow.create_action_plan(
        action,
        conversation,
        account_id="demo-checking-001",
        recipient_label="妈妈",
        budget_minor=budget,
        items=items
        or [
            {"product_id": "flowers-basic", "quantity": 1},
            {"product_id": "cake-basic", "quantity": 1},
        ],
        execute_at=execute_at,
        task_id=task_id,
    )


def test_normal_flow_reserves_exact_quote_and_posts_simulated_merchant_spend(scenario):
    bank, _clock, workflow, _url = scenario
    source_before = _balance(workflow, "demo-checking-001")
    plan = _plan(workflow, budget=35_000)

    assert plan["total_minor"] == 32_800
    assert plan["reserved_minor"] == 32_800
    assert [(row["id"], row["price_minor"]) for row in workflow.catalog()] == [
        ("flowers-basic", 19_900), ("cake-basic", 12_900)
    ]
    accepted = workflow.decide_plan(plan["id"], "approve")
    task = workflow.get_task(plan["task_id"])

    assert accepted["status"] == "completed"
    assert accepted["task_status"] == "completed"
    assert "不表示商品已配送" in accepted["message"]
    assert task["status"] == "completed"
    assert task["paid_minor"] == 32_800
    assert task["reserved_minor"] == 0
    assert task["net_spent_minor"] == 32_800
    assert sorted(step["total_minor"] for step in task["steps"]) == [12_900, 19_900]
    assert _balance(workflow, "demo-checking-001") == source_before - 32_800
    assert _balance(workflow, "workflow-merchant-flowers-account") == 19_900
    assert _balance(workflow, "workflow-merchant-cake-account") == 12_900
    with workflow._session_factory() as session:
        hold = session.scalar(select(FundHold).where(FundHold.id == task["hold_id"]))
        assert hold.status == "consumed" and hold.remaining_minor == 0
        descriptions = session.scalars(
            select(LedgerTransaction.description).where(LedgerTransaction.operation_id.like("wf-pay-%"))
        ).all()
    assert set(descriptions) == {"模拟商户消费·花间花坊", "模拟商户消费·云朵烘焙"}
    assert all(
        event["receipt"]["state_version"] <= task["state_version"] for event in workflow.completed_events()
    )
    assert bank.overview()["accounts"]


def test_budget_is_an_upper_bound_and_price_change_requires_a_new_preview(scenario):
    _bank, _clock, workflow, _url = scenario
    with pytest.raises(BankError, match="超过已填写预算"):
        _plan(workflow, budget=32_799)

    plan = _plan(workflow)
    with workflow._transaction() as session:
        session.get(WorkflowCatalogItem, "cake-basic").price_minor = 13_000
        session.get(WorkflowCatalogItem, "cake-basic").version = 2
    decision = workflow.decide_plan(plan["id"], "approve")
    assert decision["status"] == "declined"
    assert decision["code"] == "PRODUCT_RULE_CHANGED"
    assert workflow.get_task(plan["task_id"]) is None
    assert _balance(workflow, "demo-checking-001") == 9_100_567


def test_out_of_stock_releases_remaining_hold_and_refunds_paid_flower(scenario):
    _bank, _clock, workflow, _url = scenario
    source_before = _balance(workflow, "demo-checking-001")
    workflow.fault_mode = "cake_out_of_stock"
    plan = _plan(workflow)
    workflow.decide_plan(plan["id"], "approve")
    task = workflow.get_task(plan["task_id"])

    assert task["status"] == "failed"
    assert task["paid_minor"] == 19_900
    assert task["refunded_minor"] == 19_900
    assert task["refund_pending_minor"] == 0
    assert task["net_spent_minor"] == 0
    assert task["reserved_minor"] == 0
    assert [step["status"] for step in task["steps"]] == ["refunded", "failed"]
    assert _balance(workflow, "demo-checking-001") == source_before
    assert _balance(workflow, "workflow-merchant-flowers-account") == 0
    with workflow._session_factory() as session:
        rows = session.scalars(
            select(LedgerTransaction).where(LedgerTransaction.operation_id.like("wf-refund-%"))
        ).all()
    assert len(rows) == 2
    assert {row.description for row in rows} == {"模拟商户退款·花间花坊"}


def test_lost_receipt_recovers_original_order_without_repaying(scenario):
    _bank, _clock, workflow, _url = scenario
    source_before = _balance(workflow, "demo-checking-001")
    workflow.fault_mode = "response_lost"
    plan = _plan(workflow)
    accepted = workflow.decide_plan(plan["id"], "approve")
    assert accepted["task_status"] == "unknown"
    assert _balance(workflow, "demo-checking-001") == source_before - 19_900

    recovered = workflow.recover(plan["task_id"])
    assert recovered["status"] == "running"
    assert recovered["paid_minor"] == 19_900
    assert _balance(workflow, "demo-checking-001") == source_before - 19_900
    workflow.process_due()
    workflow.process_due()
    final = workflow.get_task(plan["task_id"])
    assert final["status"] == "completed"
    assert final["paid_minor"] == 32_800
    assert final["net_spent_minor"] == 32_800
    assert _balance(workflow, "demo-checking-001") == source_before - 32_800
    with workflow._session_factory() as session:
        orders = session.scalars(
            select(WorkflowMerchantOrder).where(WorkflowMerchantOrder.task_id == plan["task_id"])
        ).all()
        operations = session.scalars(
            select(LedgerTransaction.operation_id).where(LedgerTransaction.operation_id.like("wf-pay-%"))
        ).all()
    assert len(orders) == 2
    assert len(operations) == 4


def test_refund_failure_keeps_merchant_funds_pending_then_recovers_same_order(scenario):
    _bank, _clock, workflow, _url = scenario
    source_before = _balance(workflow, "demo-checking-001")
    workflow.fault_mode = "refund_failure"
    plan = _plan(workflow)
    workflow.decide_plan(plan["id"], "approve")
    pending = workflow.get_task(plan["task_id"])

    assert pending["status"] == "refund_pending"
    assert pending["paid_minor"] == 19_900
    assert pending["refunded_minor"] == 0
    assert pending["refund_pending_minor"] == 19_900
    assert pending["net_spent_minor"] == 19_900
    assert _balance(workflow, "demo-checking-001") == source_before - 19_900
    assert _balance(workflow, "workflow-merchant-flowers-account") == 19_900
    recovered = workflow.recover(plan["task_id"])
    assert recovered["status"] == "failed"
    assert recovered["refunded_minor"] == 19_900
    assert recovered["refund_pending_minor"] == 0
    assert recovered["net_spent_minor"] == 0
    assert _balance(workflow, "demo-checking-001") == source_before
    workflow.recover(plan["task_id"])
    assert _balance(workflow, "demo-checking-001") == source_before
    with workflow._session_factory() as session:
        payments = session.scalars(
            select(LedgerTransaction.operation_id).where(LedgerTransaction.operation_id.like("wf-pay-%"))
        ).all()
        refunds = session.scalars(
            select(LedgerTransaction.operation_id).where(LedgerTransaction.operation_id.like("wf-refund-%"))
        ).all()
    assert len(payments) == 2
    assert len(refunds) == 2


def test_cancellation_reports_fulfilled_order_as_partial(scenario):
    _bank, _clock, workflow, _url = scenario
    plan = _plan(workflow)
    workflow.decide_plan(plan["id"], "approve")
    task = workflow.get_task(plan["task_id"])
    with workflow._transaction() as session:
        flower_order = session.scalar(
            select(WorkflowMerchantOrder).where(
                WorkflowMerchantOrder.task_id == task["id"],
                WorkflowMerchantOrder.product_id == "flowers-basic",
            )
        )
        flower_order.status = "FULFILLED"

    cancel = workflow.create_action_plan("cancel", "cancel-chat", task_id=task["id"])
    decision = workflow.decide_plan(cancel["id"], "approve")
    result = workflow.get_task(task["id"])
    assert decision["status"] == "completed"
    assert result["status"] == "partial"
    assert result["paid_minor"] == 32_800
    assert result["refunded_minor"] == 12_900
    assert result["net_spent_minor"] == 19_900
    assert any("不可退款" in step["message"] for step in result["steps"])
    event = workflow.completed_events()[-1]
    assert event["receipt"]["related_plan_ids"] == [cancel["id"]]


def test_cancelling_scheduled_arrangement_releases_the_hold_without_spending(scenario):
    _bank, clock, workflow, _url = scenario
    source_before = _balance(workflow, "demo-checking-001")
    plan = _plan(workflow, execute_at=clock.now() + timedelta(hours=2))
    workflow.decide_plan(plan["id"], "approve")
    scheduled = workflow.get_task(plan["task_id"])
    assert scheduled["status"] == "scheduled"
    assert scheduled["reserved_minor"] == 32_800
    assert scheduled["paid_minor"] == 0

    cancel = workflow.create_action_plan("cancel", "cancel-before-run", task_id=plan["task_id"])
    decision = workflow.decide_plan(cancel["id"], "approve")
    cancelled = workflow.get_task(plan["task_id"])

    assert decision["task_status"] == "cancelled"
    assert cancelled["status"] == "cancelled"
    assert cancelled["reserved_minor"] == 0
    assert cancelled["paid_minor"] == 0
    assert _balance(workflow, "demo-checking-001") == source_before
    with workflow._session_factory() as session:
        hold = session.get(FundHold, scheduled["hold_id"])
    assert hold.status == "released" and hold.remaining_minor == 0


def test_two_workers_process_due_task_once_and_expired_window_releases_hold(scenario):
    _bank, clock, workflow, database_url = scenario
    future = clock.now() + timedelta(minutes=1)
    plan = _plan(workflow, execute_at=future)
    accepted = workflow.decide_plan(plan["id"], "approve")
    assert accepted["task_status"] == "scheduled"
    assert _balance(workflow, "demo-checking-001") == 9_100_567
    clock.advance(1)
    worker = WorkflowService(database_url)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda service: service.process_due(), [workflow, worker]))
    finally:
        worker.close()
    task = workflow.get_task(plan["task_id"])
    assert task["status"] == "completed"
    assert task["paid_minor"] == 32_800
    assert _balance(workflow, "demo-checking-001") == 9_100_567 - 32_800
    with workflow._session_factory() as session:
        operations = session.scalars(
            select(LedgerTransaction.operation_id).where(LedgerTransaction.operation_id.like("wf-pay-%"))
        ).all()
    assert len(operations) == 4

    late = _plan(workflow, conversation="late", execute_at=clock.now() + timedelta(minutes=1))
    workflow.decide_plan(late["id"], "approve")
    clock.advance(24 * 60 + 2)
    workflow.process_due()
    expired = workflow.get_task(late["task_id"])
    assert expired["status"] == "expired"
    assert expired["reserved_minor"] == 0
    assert all(step["status"] == "failed" for step in expired["steps"])
    assert _balance(workflow, "demo-checking-001") == 9_100_567 - 32_800


def _single_service_plan(workflow, service, *, budget=None, execute_at=None, title=""):
    return workflow.create_action_plan(
        "arrange",
        "custom-service-chat",
        account_id="demo-checking-001",
        recipient_label="家人",
        budget_minor=budget or service["price_minor"],
        items=[{"product_id": service["id"], "quantity": 1}],
        execute_at=execute_at,
        title=title or None,
    )


def test_custom_service_runs_through_real_simulated_payment_and_original_order_refund(scenario):
    _bank, _clock, workflow, _url = scenario
    source_before = _balance(workflow, "demo-checking-001")
    service = workflow.create_service(
        "节日贺卡",
        "小岛手作",
        2_900,
        unit="张",
        description="简约手写贺卡",
        cancel_supported=True,
        client_request_id="custom-card-once",
    )
    assert service["active"] is True
    assert service["merchant_name"] == "小岛手作"
    assert workflow.create_service(
        "ignored retry",
        "other merchant",
        8_800,
        client_request_id="custom-card-once",
    )["id"] == service["id"]

    plan = _single_service_plan(workflow, service, title="家人的节日卡片")
    accepted = workflow.decide_plan(plan["id"], "approve")
    task = workflow.get_task(plan["task_id"])
    assert accepted["task_status"] == "completed"
    assert task["title"] == "家人的节日卡片"
    assert task["paid_minor"] == 2_900
    assert task["net_spent_minor"] == 2_900
    assert _balance(workflow, "demo-checking-001") == source_before - 2_900

    with workflow._session_factory() as session:
        merchant = session.get(WorkflowMerchant, service["merchant_id"])
        order = session.scalar(
            select(WorkflowMerchantOrder).where(WorkflowMerchantOrder.task_id == task["id"])
        )
        assert merchant is not None
        merchant_account_id = merchant.account_id
        assert order is not None and order.status == "CONFIRMED"
        assert order.amount_minor == 2_900
    assert _balance(workflow, merchant_account_id) == 2_900

    cancel = workflow.create_action_plan("cancel", "custom-service-cancel", task_id=task["id"])
    workflow.decide_plan(cancel["id"], "approve")
    refunded = workflow.get_task(task["id"])
    assert refunded["status"] == "cancelled"
    assert refunded["paid_minor"] == 2_900
    assert refunded["refunded_minor"] == 2_900
    assert refunded["net_spent_minor"] == 0
    assert _balance(workflow, "demo-checking-001") == source_before
    assert _balance(workflow, merchant_account_id) == 0


def test_scheduled_custom_service_keeps_confirmed_price_merchant_and_cancel_snapshot(scenario):
    _bank, clock, workflow, _url = scenario
    source_before = _balance(workflow, "demo-checking-001")
    service = workflow.create_service("手作礼盒", "旧商户", 3_499, cancel_supported=True)
    old_merchant_id = service["merchant_id"]
    old_merchant_account = workflow._merchant_account(
        workflow._session_factory(), old_merchant_id
    )
    scheduled = _single_service_plan(
        workflow,
        service,
        execute_at=clock.now() + timedelta(minutes=30),
        title="周末礼盒安排",
    )
    workflow.decide_plan(scheduled["id"], "approve")
    assert _balance(workflow, "demo-checking-001") == source_before

    edited = workflow.update_service(
        service["id"],
        version=service["version"],
        merchant_name="新商户",
        price_minor=8_800,
        cancel_supported=False,
    )
    assert edited["version"] == 2
    removed = workflow.remove_service(service["id"], version=edited["version"])
    assert removed["active"] is False
    workflow.seed_demo()
    assert service["id"] not in {row["id"] for row in workflow.catalog()}

    clock.advance(30)
    workflow.process_due()
    task = workflow.get_task(scheduled["task_id"])
    assert task["status"] == "completed"
    assert task["title"] == "周末礼盒安排"
    assert task["paid_minor"] == 3_499
    assert task["steps"][0]["merchant_id"] == old_merchant_id
    assert task["steps"][0]["merchant_name"] == "旧商户"
    assert _balance(workflow, old_merchant_account) == 3_499

    cancel = workflow.create_action_plan("cancel", "scheduled-custom-cancel", task_id=task["id"])
    workflow.decide_plan(cancel["id"], "approve")
    final = workflow.get_task(task["id"])
    assert final["status"] == "cancelled"
    assert final["refunded_minor"] == 3_499
    assert final["net_spent_minor"] == 0
    assert _balance(workflow, "demo-checking-001") == source_before
    assert _balance(workflow, old_merchant_account) == 0
    with workflow._session_factory() as session:
        current_merchant = session.scalar(
            select(WorkflowMerchant).where(WorkflowMerchant.name == "新商户")
        )
        assert current_merchant is not None
        assert _balance(workflow, current_merchant.account_id) == 0


def test_custom_non_cancellable_service_keeps_payment_after_cancel_request(scenario):
    _bank, _clock, workflow, _url = scenario
    source_before = _balance(workflow, "demo-checking-001")
    service = workflow.create_service("定制刻字", "刻字工坊", 1_250, cancel_supported=False)
    plan = _single_service_plan(workflow, service)
    workflow.decide_plan(plan["id"], "approve")
    task = workflow.get_task(plan["task_id"])
    assert task["status"] == "completed"
    assert task["title"] == "给家人的服务安排"

    cancel = workflow.create_action_plan("cancel", "non-cancellable-cancel", task_id=task["id"])
    workflow.decide_plan(cancel["id"], "approve")
    final = workflow.get_task(task["id"])
    assert final["status"] == "partial"
    assert final["paid_minor"] == 1_250
    assert final["refunded_minor"] == 0
    assert final["net_spent_minor"] == 1_250
    assert "不支持取消" in final["steps"][0]["message"]
    assert _balance(workflow, "demo-checking-001") == source_before - 1_250


def test_editing_pending_custom_service_invalidates_confirmation(scenario):
    _bank, _clock, workflow, _url = scenario
    service = workflow.create_service("定制卡片", "纸间商店", 2_000)
    plan = _single_service_plan(workflow, service)
    workflow.update_service(service["id"], version=service["version"], price_minor=2_500)

    with pytest.raises(BankError) as error:
        workflow.decide_plan(plan["id"], "approve")
    assert error.value.code == "PLAN_INVALIDATED"
