"""Isolated HTTP and model-tool coverage for the gated AA and schedule slice."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from app.bank.models import Account, LedgerTransaction
from app.llm import ModelError
from app.main import create_app
from app.task_clock import TaskClock
from fastapi.testclient import TestClient
from sqlalchemy import func, select


def call(name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"task-test-{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ],
    }


class TaskSequenceModel:
    configured = True

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0
        self.available_tools: list[set[str]] = []

    async def close(self):
        pass

    async def complete(self, messages, tools, emit=None):
        self.calls += 1
        self.available_tools.append({item["function"]["name"] for item in tools})
        return next(self.responses)


class QuietModel(TaskSequenceModel):
    def __init__(self):
        super().__init__([{"role": "assistant", "content": "我会按当前安排状态回答。"}] * 20)


class FailAfterAATaskPreview(TaskSequenceModel):
    def __init__(self):
        self.calls = 0
        self.available_tools = []

    async def complete(self, messages, tools, emit=None):
        self.calls += 1
        self.available_tools.append({item["function"]["name"] for item in tools})
        if self.calls == 1:
            return call(
                "create_task_preview",
                {
                    "action": "create_aa",
                    "account_id": "demo-checking-001",
                    "total_minor": 10_000,
                    "participant_ids": ["self", "demo-recipient-chen"],
                },
            )
        raise ModelError("PROVIDER_TIMEOUT", "模型暂时超时，请重试。")


def _app(db_url, model=None, *, tasks=False, workflows=False):
    return create_app(
        db_url,
        model=model or QuietModel(),
        enable_tasks_actions=tasks,
        enable_workflow_actions=workflows,
        task_poll_seconds=3600,
    )


def _new_conversation(client, scope="bills/payments"):
    return client.post("/api/conversations", json={"scope": scope}).json()["id"]


def _account_balance(client, account_id: str) -> int:
    with client.app.state.bank._session_factory() as session:
        value = session.scalar(select(Account.balance_minor).where(Account.id == account_id))
    assert value is not None
    return value


def _schedule_preview(client, conversation_id: str, execute_at, amount_minor=5_000):
    return client.post(
        "/api/tasks/preview",
        json={
            "action": "schedule_transfer",
            "conversation_id": conversation_id,
            "account_id": "demo-checking-001",
            "recipient_id": "demo-recipient-chen",
            "amount_minor": amount_minor,
            "execute_at": execute_at.isoformat(),
        },
    )


def test_factory_defaults_closed_and_task_workflow_flags_are_independent(tmp_path):
    default_url = f"sqlite:///{(tmp_path / 'default-flags.sqlite').as_posix()}"
    with TestClient(_app(default_url)) as client:
        health = client.get("/api/health").json()
        assert health["tasks_actions_enabled"] is False
        assert health["workflow_actions_enabled"] is False
        assert client.get("/api/tasks").status_code == 404
        assert client.get("/api/workflows").status_code == 404

    task_url = f"sqlite:///{(tmp_path / 'task-only.sqlite').as_posix()}"
    with TestClient(_app(task_url, tasks=True, workflows=False)) as client:
        health = client.get("/api/health").json()
        assert health["tasks_actions_enabled"] is True
        assert health["workflow_actions_enabled"] is False
        assert client.get("/api/tasks").status_code == 200
        assert client.get("/api/workflows").status_code == 404

    workflow_url = f"sqlite:///{(tmp_path / 'workflow-only.sqlite').as_posix()}"
    with TestClient(_app(workflow_url, tasks=False, workflows=True)) as client:
        health = client.get("/api/health").json()
        assert health["tasks_actions_enabled"] is False
        assert health["workflow_actions_enabled"] is True
        assert client.get("/api/tasks").status_code == 404
        assert client.get("/api/workflows").status_code == 200


def test_model_can_query_and_preview_aa_but_has_no_confirmation_or_payment_tools(tmp_path):
    model = TaskSequenceModel(
        [
            call("find_recipients", {"query": "陈晨"}),
            call(
                "create_task_preview",
                {
                    "action": "create_aa",
                    "account_id": "demo-checking-001",
                    "total_minor": 10_000,
                    "participant_ids": ["self", "demo-recipient-chen", "demo-recipient-li"],
                },
            ),
            {"role": "assistant", "content": "我已准备AA预览，请核对参与者和金额。"},
            call("list_tasks", {}),
            {"role": "assistant", "content": "目前还没有创建中的AA请求。"},
        ]
    )
    url = f"sqlite:///{(tmp_path / 'task-agent.sqlite').as_posix()}"
    with TestClient(_app(url, model, tasks=True)) as client:
        conversation_id = _new_conversation(client)
        before = client.get("/api/overview").json()
        result = client.post(
            "/api/chat",
            json={
                "message": "帮我发起一个总额100元的三人AA，我、陈晨和李娜",
                "conversation_id": conversation_id,
                "scope": "bills/payments",
            },
        ).json()
        assert result["action"]["kind"] == "task"
        assert result["action"]["action"] == "create_aa"
        assert result["action"]["collectable_minor"] == 6_666
        assert client.get("/api/overview").json()["accounts"] == before["accounts"]
        assert client.get("/api/tasks").json()["aa"] == []
        detail = client.get(f"/api/conversations/{conversation_id}").json()
        assert detail["pending_action"]["id"] == result["action"]["id"]

        followup = client.post(
            "/api/chat",
            json={"message": "查询当前AA状态", "conversation_id": conversation_id},
        )
        assert followup.status_code == 200
        assert client.get(f"/api/conversations/{conversation_id}").json()["pending_action"]["id"] == result["action"]["id"]

        assert model.available_tools
        names = set.union(*model.available_tools)
        assert {"get_task_clock", "list_tasks", "find_recipients", "create_task_preview", "cancel_task_preview"} <= names
        assert not any(
            fragment in name
            for name in names
            for fragment in ("approve", "decision", "demo-payment", "advance")
        )


def test_aa_http_partial_payment_duplicate_and_cancel_preserves_received_share(tmp_path):
    url = f"sqlite:///{(tmp_path / 'task-aa-api.sqlite').as_posix()}"
    with TestClient(_app(url, tasks=True)) as client:
        conversation_id = _new_conversation(client)
        source_before = _account_balance(client, "demo-checking-001")
        chen_before = _account_balance(client, "demo-destination-chen")
        plan = client.post(
            "/api/tasks/preview",
            json={
                "action": "create_aa",
                "conversation_id": conversation_id,
                "account_id": "demo-checking-001",
                "total_minor": 100,
                "participant_ids": ["self", "demo-recipient-chen", "demo-recipient-li"],
            },
        ).json()
        assert [row["share_minor"] for row in plan["participants"]] == [34, 33, 33]
        assert _account_balance(client, "demo-checking-001") == source_before
        assert client.get("/api/tasks").json()["aa"] == []

        accepted = client.post(
            f"/api/task-plans/{plan['id']}/decision",
            json={"decision": "approve", "conversation_id": conversation_id},
        ).json()
        assert accepted["task_status"] == "awaiting_payment"
        assert accepted["status"] == "completed"
        assert _account_balance(client, "demo-checking-001") == source_before

        path = f"/api/tasks/{plan['task_id']}/demo-payment"
        first = client.post(path, json={"participant_id": "demo-recipient-chen"}).json()
        duplicate = client.post(path, json={"participant_id": "demo-recipient-chen"}).json()
        assert first["task_status"] == "partial"
        assert first["collected_minor"] == 33
        assert duplicate["duplicate"] is True
        assert duplicate["operation_id"] == first["operation_id"]
        assert _account_balance(client, "demo-checking-001") == source_before + 33
        assert _account_balance(client, "demo-destination-chen") == chen_before - 33
        with client.app.state.bank._session_factory() as session:
            assert session.scalar(
                select(func.count(LedgerTransaction.id)).where(
                    LedgerTransaction.operation_id == first["operation_id"]
                )
            ) == 2

        cancel = client.post(
            "/api/tasks/preview",
            json={"action": "cancel_aa", "conversation_id": conversation_id, "task_id": plan["task_id"]},
        ).json()
        cancelled = client.post(
            f"/api/task-plans/{cancel['id']}/decision",
            json={"decision": "approve", "conversation_id": conversation_id},
        ).json()
        assert cancelled["task_status"] == "cancelled"
        aa = next(item for item in client.get("/api/tasks").json()["aa"] if item["id"] == plan["task_id"])
        statuses = {item["id"]: item["status"] for item in aa["participants"]}
        assert statuses["demo-recipient-chen"] == "paid"
        assert statuses["demo-recipient-li"] == "cancelled"
        messages = client.get(f"/api/conversations/{conversation_id}").json()["messages"]
        origin_message = next(message for message in messages if (message.get("action") or {}).get("id") == plan["id"])
        assert origin_message["receipt"]["task_status"] == "cancelled"
        assert origin_message["receipt"]["state_version"] == aa["state_version"]
        late_payment = client.post(
            f"/api/tasks/{plan['task_id']}/demo-payment", json={"participant_id": "demo-recipient-li"}
        )
        assert late_payment.status_code == 409
        assert late_payment.json()["detail"]["code"] == "TASK_CANCELLED"


def test_schedule_preview_reserves_only_after_confirmation_and_due_payment_is_once(tmp_path):
    url = f"sqlite:///{(tmp_path / 'task-schedule-api.sqlite').as_posix()}"
    with TestClient(_app(url, tasks=True)) as client:
        conversation_id = _new_conversation(client)
        execute_at = client.app.state.task_clock.now() + timedelta(minutes=5)
        before = next(item for item in client.get("/api/overview").json()["accounts"] if item["id"] == "demo-checking-001")
        transactions_before = client.app.state.bank.list_transactions()
        plan_response = _schedule_preview(client, conversation_id, execute_at)
        assert plan_response.status_code == 200
        plan = plan_response.json()
        after_preview = next(item for item in client.get("/api/overview").json()["accounts"] if item["id"] == "demo-checking-001")
        assert after_preview["reserved_minor"] == before["reserved_minor"]
        assert after_preview["balance_minor"] == before["balance_minor"]
        assert client.app.state.bank.list_transactions() == transactions_before

        accepted = client.post(
            f"/api/task-plans/{plan['id']}/decision",
            json={"decision": "approve", "conversation_id": conversation_id},
        ).json()
        assert accepted["task_status"] == "scheduled"
        reserved = next(item for item in client.get("/api/overview").json()["accounts"] if item["id"] == "demo-checking-001")
        assert reserved["balance_minor"] == before["balance_minor"]
        assert reserved["reserved_minor"] == before["reserved_minor"] + 5_000

        chat = client.post(
            "/api/chat", json={"message": "顺便解释下这个计划", "conversation_id": conversation_id}
        )
        assert chat.status_code == 200
        schedule = next(item for item in client.get("/api/tasks").json()["schedules"] if item["id"] == plan["task_id"])
        assert schedule["status"] == "scheduled"
        assert schedule["reserved_minor"] == 5_000

        advanced = client.post("/api/tasks/demo/advance", json={"minutes": 6})
        assert advanced.status_code == 200
        done = next(item for item in client.get("/api/tasks").json()["schedules"] if item["id"] == plan["task_id"])
        assert done["status"] == "completed"
        assert done["reserved_minor"] == 0
        paid = next(item for item in client.get("/api/overview").json()["accounts"] if item["id"] == "demo-checking-001")
        assert paid["balance_minor"] == before["balance_minor"] - 5_000
        client.post("/api/tasks/demo/advance", json={"minutes": 1})
        with client.app.state.bank._session_factory() as session:
            assert session.scalar(
                select(func.count(LedgerTransaction.id)).where(
                    LedgerTransaction.operation_id == schedule["operation_id"]
                )
            ) == 2


@pytest.mark.parametrize(
    ("advance_minutes", "expected_status"),
    [(6, "completed"), (24 * 60 + 6, "expired")],
)
def test_scheduled_transfer_recovers_or_expires_after_restart(tmp_path, advance_minutes, expected_status):
    url = f"sqlite:///{(tmp_path / f'restart-{expected_status}.sqlite').as_posix()}"
    model = QuietModel()
    with TestClient(_app(url, model, tasks=True)) as client:
        conversation_id = _new_conversation(client)
        execute_at = client.app.state.task_clock.now() + timedelta(minutes=5)
        before = _account_balance(client, "demo-checking-001")
        plan = _schedule_preview(client, conversation_id, execute_at).json()
        client.post(
            f"/api/task-plans/{plan['id']}/decision",
            json={"decision": "approve", "conversation_id": conversation_id},
        ).raise_for_status()
        assert _account_balance(client, "demo-checking-001") == before

    clock = TaskClock(url)
    clock.advance(advance_minutes)
    clock.close()

    with TestClient(_app(url, QuietModel(), tasks=True)) as client:
        schedule = next(item for item in client.get("/api/tasks").json()["schedules"] if item["id"] == plan["task_id"])
        assert schedule["status"] == expected_status
        assert schedule["reserved_minor"] == 0
        balance = _account_balance(client, "demo-checking-001")
        assert (
            balance == before - 5_000
            if expected_status == "completed"
            else balance == before
        )


def test_retry_after_task_preview_failure_reuses_same_plan_and_user_message(tmp_path):
    model = FailAfterAATaskPreview()
    request_id = "aa-preview-timeout-001"
    url = f"sqlite:///{(tmp_path / 'task-preview-retry.sqlite').as_posix()}"
    payload = {
        "message": "帮我发起100元AA，我和陈晨均分",
        "scope": "bills/payments",
        "client_request_id": request_id,
    }
    with TestClient(_app(url, model, tasks=True)) as client:
        first = client.post("/api/chat", json=payload)
        assert first.status_code == 503
        conversation_id = client.app.state.store.request_conversation_id(None, request_id)
        pending = client.app.state.tasks.pending_for_conversation(conversation_id)
        assert pending and pending["action"] == "create_aa"
        retry = client.post("/api/chat", json={**payload, "retry_failed": True})
        assert retry.status_code == 200
        assert retry.json()["action"]["id"] == pending["id"]
        assert model.calls == 2
        detail = client.get(f"/api/conversations/{conversation_id}").json()
        assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]
        assert detail["pending_action"]["id"] == pending["id"]
        assert client.get("/api/tasks").json()["aa"] == []
