"""Isolated HTTP and tool-call coverage for the approved simulated workflow slice."""

from __future__ import annotations

import json
from datetime import datetime

from app.agent import _workflow_requested_immediately
from app.bank.models import Account, LedgerTransaction
from app.llm import ModelError
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import select


def call(name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"workflow-test-{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ],
    }


class WorkflowModel:
    configured = True

    def __init__(self, responses=None):
        self.responses = iter(responses or [])
        self.available_tools: list[set[str]] = []
        self.execute_at_descriptions: list[str] = []

    async def close(self):
        pass

    async def complete(self, messages, tools, emit=None):
        self.available_tools.append({item["function"]["name"] for item in tools})
        for item in tools:
            if item["function"]["name"] == "create_workflow_preview":
                self.execute_at_descriptions.append(
                    item["function"]["parameters"]["properties"]["execute_at"]["description"]
                )
        try:
            return next(self.responses)
        except StopIteration:
            return {"role": "assistant", "content": "我会依据当前安排和订单状态回答。"}


class FailAfterWorkflowPreview(WorkflowModel):
    def __init__(self):
        self.calls = 0
        self.available_tools = []

    async def complete(self, messages, tools, emit=None):
        self.calls += 1
        self.available_tools.append({item["function"]["name"] for item in tools})
        if self.calls == 1:
            return call(
                "create_workflow_preview",
                {
                    "action": "arrange",
                    "account_id": "demo-checking-001",
                    "recipient_label": "妈妈",
                    "budget_minor": 50_000,
                    "items": [
                        {"product_id": "flowers-basic", "quantity": 1},
                        {"product_id": "cake-basic", "quantity": 1},
                    ],
                },
            )
        raise ModelError("PROVIDER_TIMEOUT", "模型暂时超时，请重试。")


def _app(db_url, model=None, *, workflows=False, fault_mode=None):
    return create_app(
        db_url,
        model=model or WorkflowModel(),
        enable_workflow_actions=workflows,
        enable_tasks_actions=False,
        workflow_fault_mode=fault_mode,
        task_poll_seconds=3600,
    )


def _conversation(client, scope="life/arrangements"):
    return client.post("/api/conversations", json={"scope": scope}).json()["id"]


def _account_balance(client, account_id: str) -> int:
    with client.app.state.bank._session_factory() as session:
        value = session.scalar(select(Account.balance_minor).where(Account.id == account_id))
    assert value is not None
    return value


def _preview(client, conversation_id: str, *, action="arrange", task_id=None, items=None):
    payload = {
        "action": action,
        "conversation_id": conversation_id,
        "account_id": "demo-checking-001",
        "recipient_label": "妈妈",
        "budget_minor": 50_000,
        "items": items
        or [
            {"product_id": "flowers-basic", "quantity": 1},
            {"product_id": "cake-basic", "quantity": 1},
        ],
    }
    if task_id:
        payload["task_id"] = task_id
    if action == "cancel":
        payload = {"action": action, "conversation_id": conversation_id, "task_id": task_id}
    return client.post("/api/workflows/preview", json=payload)


def test_workflow_flag_is_independent_and_model_tools_cannot_execute_or_recover(tmp_path):
    model = WorkflowModel()
    url = f"sqlite:///{(tmp_path / 'workflow-only-tools.sqlite').as_posix()}"
    with TestClient(_app(url, model, workflows=True)) as client:
        health = client.get("/api/health").json()
        assert health["tasks_actions_enabled"] is False
        assert health["workflow_actions_enabled"] is True
        response = client.post(
            "/api/chat",
            json={"message": "有哪些生活服务可以安排？", "scope": "life/arrangements"},
        )
        assert response.status_code == 200
        assert client.get("/api/workflows").status_code == 200
        assert client.get("/api/tasks").status_code == 404
        names = set.union(*model.available_tools)
        assert {
            "get_workflow_catalog",
            "list_workflows",
            "get_workflow_clock",
            "create_workflow_preview",
            "cancel_workflow_preview",
        } <= names
        assert "get_task_clock" not in names
        assert not any(
            fragment in name
            for name in names
            for fragment in ("approve", "decision", "recover", "payment", "advance", "fault", "order")
        )

    off_url = f"sqlite:///{(tmp_path / 'workflow-off.sqlite').as_posix()}"
    with TestClient(_app(off_url, workflows=False)) as client:
        assert client.get("/api/workflows").status_code == 404


def test_model_preview_and_read_only_list_keep_one_scoped_pending_plan(tmp_path):
    model = WorkflowModel(
        [
            call("get_workflow_catalog", {}),
            call("get_workflow_clock", {}),
            call(
                "create_workflow_preview",
                {
                    "action": "arrange",
                    "account_id": "demo-checking-001",
                    "recipient_label": "妈妈",
                    "budget_minor": 50_000,
                    "items": [
                        {"product_id": "flowers-basic", "quantity": 1},
                        {"product_id": "cake-basic", "quantity": 1},
                    ],
                },
            ),
            {"role": "assistant", "content": "已按模拟服务目录准备预览，请核对方案。"},
            call("list_workflows", {}),
            {"role": "assistant", "content": "查询已确认的生日安排。"},
        ]
    )
    url = f"sqlite:///{(tmp_path / 'workflow-agent.sqlite').as_posix()}"
    with TestClient(_app(url, model, workflows=True)) as client:
        before = _account_balance(client, "demo-checking-001")
        result = client.post(
            "/api/chat",
            json={
                "message": "给妈妈安排生日，预算500元，确认后立即开始，一束花和一个蛋糕",
                "scope": "life/arrangements",
                "client_request_id": "workflow-chat-preview-001",
            },
        )
        assert result.status_code == 200
        body = result.json()
        action = body["action"]
        assert action["kind"] == "workflow"
        assert action["total_minor"] == 32_800
        assert "先预留实际报价" in action["description"]
        assert "仅表示模拟订单确认" not in action["description"]
        assert _account_balance(client, "demo-checking-001") == before
        assert client.get("/api/workflows").json()["tasks"] == []
        conversation_id = body["conversation_id"]
        detail = client.get(f"/api/conversations/{conversation_id}").json()
        assert detail["pending_action"]["id"] == action["id"]
        assert detail["conversation"]["scopes"] == ["life/arrangements"]

        readonly = client.post(
            "/api/chat",
            json={"message": "看一下现有安排", "conversation_id": conversation_id},
        )
        assert readonly.status_code == 200
        assert client.get(f"/api/conversations/{conversation_id}").json()["pending_action"]["id"] == action["id"]
        names = set.union(*model.available_tools)
        assert {"get_workflow_catalog", "get_workflow_clock", "list_workflows", "create_workflow_preview"} <= names
        assert not any(
            fragment in name
            for name in names
            for fragment in ("approve", "decision", "recover", "payment", "advance", "fault")
        )


def test_normal_workflow_api_reserves_actual_quote_then_confirms_simulated_orders(tmp_path):
    url = f"sqlite:///{(tmp_path / 'workflow-normal-api.sqlite').as_posix()}"
    with TestClient(_app(url, workflows=True)) as client:
        conversation_id = _conversation(client)
        before = _account_balance(client, "demo-checking-001")
        plan_response = _preview(client, conversation_id)
        assert plan_response.status_code == 200
        plan = plan_response.json()
        assert plan["total_minor"] == 32_800
        assert plan["budget_minor"] == 50_000
        assert plan["reserved_minor"] == 32_800  # amount to reserve after explicit approval
        assert "先预留实际报价" in plan["description"]
        assert _account_balance(client, "demo-checking-001") == before
        checking = next(
            item
            for item in client.app.state.bank.list_accounts()
            if item["id"] == "demo-checking-001"
        )
        assert checking["reserved_minor"] == 0
        detail = client.get(f"/api/conversations/{conversation_id}").json()
        assert detail["pending_action"]["id"] == plan["id"]
        assert detail["conversation"]["scopes"] == ["life/arrangements"]

        accepted = client.post(
            f"/api/workflow-plans/{plan['id']}/decision",
            json={"decision": "approve", "conversation_id": conversation_id},
        )
        assert accepted.status_code == 200
        result = accepted.json()
        assert result["status"] == "completed"
        assert result["task_status"] == "completed"
        assert "配送" in result["message"]
        task = client.app.state.workflows.get_task(plan["task_id"])
        assert task["conversation_id"] == conversation_id
        assert task["paid_minor"] == 32_800
        assert task["reserved_minor"] == 0
        assert task["refunded_minor"] == 0
        assert task["refund_pending_minor"] == 0
        assert task["net_spent_minor"] == 32_800
        assert [step["status"] for step in task["steps"]] == ["confirmed", "confirmed"]
        assert _account_balance(client, "demo-checking-001") == before - 32_800
        listed = client.get("/api/workflows").json()
        assert len(listed["catalog"]) == 2
        assert len(listed["tasks"]) == 1
        assert listed["tasks"][0]["conversation_id"] == conversation_id


def test_refund_pending_path_recovers_original_order_without_adding_spend(tmp_path):
    url = f"sqlite:///{(tmp_path / 'workflow-refund-api.sqlite').as_posix()}"
    with TestClient(_app(url, workflows=True, fault_mode="refund_failure")) as client:
        conversation_id = _conversation(client)
        source_before = _account_balance(client, "demo-checking-001")
        flower_before = _account_balance(client, "workflow-merchant-flowers-account")
        plan = _preview(
            client,
            conversation_id,
            items=[
                {"product_id": "flowers-basic", "quantity": 1},
                {"product_id": "cake-basic", "quantity": 1},
            ],
        ).json()
        accepted = client.post(
            f"/api/workflow-plans/{plan['id']}/decision",
            json={"decision": "approve", "conversation_id": conversation_id},
        )
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "completed"  # confirmation accepted
        assert accepted.json()["task_status"] == "refund_pending"
        pending = client.app.state.workflows.get_task(plan["task_id"])
        assert pending["status"] == "refund_pending"
        assert pending["paid_minor"] == 19_900
        assert pending["refunded_minor"] == 0
        assert pending["refund_pending_minor"] == 19_900
        assert pending["net_spent_minor"] == 19_900
        assert pending["reserved_minor"] == 0
        assert pending["conversation_id"] == conversation_id
        assert [step["product_id"] for step in pending["steps"]] == ["flowers-basic", "cake-basic"]
        assert _account_balance(client, "demo-checking-001") == source_before - 19_900
        assert _account_balance(client, "workflow-merchant-flowers-account") == flower_before + 19_900
        assert "退款" in pending["message"] and "仍归属" in pending["message"]

        recovered_response = client.post(f"/api/workflows/{plan['task_id']}/recover")
        assert recovered_response.status_code == 200
        recovered = recovered_response.json()
        assert recovered["status"] == "failed"
        assert recovered["refunded_minor"] == 19_900
        assert recovered["refund_pending_minor"] == 0
        assert recovered["net_spent_minor"] == 0
        assert recovered["conversation_id"] == conversation_id
        assert _account_balance(client, "demo-checking-001") == source_before
        assert _account_balance(client, "workflow-merchant-flowers-account") == flower_before
        with client.app.state.workflows._session_factory() as session:
            payments = session.scalars(
                select(LedgerTransaction.operation_id).where(LedgerTransaction.operation_id.like("wf-pay-%"))
            ).all()
            refunds = session.scalars(
                select(LedgerTransaction.operation_id).where(LedgerTransaction.operation_id.like("wf-refund-%"))
            ).all()
        assert len(payments) == 2
        assert len(refunds) == 2


def test_workflow_action_retry_reuses_pending_preview_and_keeps_original_message(tmp_path):
    model = FailAfterWorkflowPreview()
    client_request_id = "workflow-preview-retry-001"
    payload = {
        "message": "给妈妈安排生日，预算500元，一束花和一个蛋糕",
        "scope": "life/arrangements",
        "client_request_id": client_request_id,
    }
    url = f"sqlite:///{(tmp_path / 'workflow-preview-retry.sqlite').as_posix()}"
    with TestClient(_app(url, model, workflows=True)) as client:
        first = client.post("/api/chat", json=payload)
        assert first.status_code == 503
        conversation_id = client.app.state.store.request_conversation_id(None, client_request_id)
        pending = client.app.state.workflows.pending_for_conversation(conversation_id)
        assert pending and pending["action"] == "arrange"
        retry = client.post("/api/chat", json={**payload, "retry_failed": True})
        assert retry.status_code == 200
        assert retry.json()["action"]["id"] == pending["id"]
        assert model.calls == 2
        detail = client.get(f"/api/conversations/{conversation_id}").json()
        assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]
        assert detail["pending_action"]["id"] == pending["id"]
        assert client.get("/api/workflows").json()["tasks"] == []


def test_immediate_request_drops_model_invented_future_time(tmp_path):
    model = WorkflowModel(
        [
            call("get_workflow_clock", {}),
            call(
                "create_workflow_preview",
                {
                    "action": "arrange",
                    "account_id": "demo-checking-001",
                    "recipient_label": "家人",
                    "budget_minor": 50_000,
                    "items": [
                        {"product_id": "flowers-basic", "quantity": 1},
                        {"product_id": "cake-basic", "quantity": 1},
                    ],
                    "execute_at": "2099-01-01T19:00:00+08:00",
                },
            ),
            {"role": "assistant", "content": "立即安排的预览已准备好。"},
        ]
    )
    url = f"sqlite:///{(tmp_path / 'workflow-immediate.sqlite').as_posix()}"
    request = (
        "帮家人安排生日，预算500元，用日常账户买基础花束一份和基础蛋糕一份，"
        "确认后立即开始。请先查目录并给我确认单。"
    )
    with TestClient(_app(url, model, workflows=True)) as client:
        result = client.post(
            "/api/chat",
            json={"message": request, "scope": "life/arrangements", "client_request_id": "immediate-guard-001"},
        )
        assert result.status_code == 200
        action = result.json()["action"]
        execute_at = datetime.fromisoformat(action["execute_at"])
        after = client.app.state.task_clock.now()
        assert abs((execute_at - after).total_seconds()) < 5
        assert execute_at < datetime.fromisoformat("2099-01-01T19:00:00+08:00")
        assert client.get("/api/workflows").json()["tasks"] == []
        assert model.execute_at_descriptions
        assert all(
            "确认后开始" in value and "必须省略" in value and "绝不能编造" in value
            for value in model.execute_at_descriptions
        )


def test_immediate_intent_does_not_match_negation_or_historical_query():
    assert _workflow_requested_immediately("确认后立即开始，先查目录给我确认单")
    assert not _workflow_requested_immediately("不要立即开始，安排到明天下午三点")
    assert not _workflow_requested_immediately("查询刚才立即安排的生日方案和执行时间")
    assert not _workflow_requested_immediately("请安排到明天19:00执行")


def test_workflow_catalog_defaults_flowers_first_but_preview_preserves_selected_order(tmp_path):
    url = f"sqlite:///{(tmp_path / 'workflow-item-order.sqlite').as_posix()}"
    with TestClient(_app(url, workflows=True)) as client:
        catalog = client.get("/api/workflows").json()["catalog"]
        assert [item["id"] for item in catalog] == ["flowers-basic", "cake-basic"]
        conversation_id = _conversation(client)
        plan = _preview(
            client,
            conversation_id,
            items=[
                {"product_id": "cake-basic", "quantity": 1},
                {"product_id": "flowers-basic", "quantity": 1},
            ],
        ).json()
        assert [item["product_id"] for item in plan["items"]] == ["cake-basic", "flowers-basic"]
