from __future__ import annotations

import json

from app.main import create_app
from fastapi.testclient import TestClient


def tool_call(name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": f"spec30-{name}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
        }],
    }


class ScriptedModel:
    configured = True

    def __init__(self, responses):
        self.responses = iter(responses)

    async def close(self):
        pass

    async def complete(self, messages, tools, emit=None):
        return next(self.responses)


def post(client, path, payload):
    response = client.post(path, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_billing_report_facts_win_over_unrequested_task_clock(tmp_path):
    model = ScriptedModel([
        tool_call("get_billing_report", {}),
        tool_call("get_task_clock", {}),
        {"role": "assistant", "content": "现在是模拟时间。"},
    ])
    with TestClient(create_app(
        f"sqlite:///{(tmp_path / 'billing-clock.sqlite').as_posix()}",
        model=model,
        enable_tasks_actions=True,
        task_poll_seconds=3600,
    )) as client:
        conversation = post(client, "/api/conversations", {})["id"]
        result = post(client, "/api/chat", {
            "message": "这个月外卖大概花了多少？", "conversation_id": conversation,
        })
    assert "模拟时间" not in result["reply"]
    assert "没有“外卖”的独立汇总" in result["reply"]
    assert "净支出总额不能直接当作该项金额" in result["reply"]


def test_workflow_time_marked_unset_prompts_without_creating_preview(tmp_path):
    model = ScriptedModel([
        tool_call("get_workflow_catalog", {}),
        {"role": "assistant", "content": "目录里有鲜花和蛋糕。"},
    ])
    with TestClient(create_app(
        f"sqlite:///{(tmp_path / 'workflow-time.sqlite').as_posix()}",
        model=model,
        enable_workflow_actions=True,
        task_poll_seconds=3600,
    )) as client:
        conversation = post(client, "/api/conversations", {})["id"]
        result = post(client, "/api/chat", {
            "message": "下周末家里来人，帮我准备鲜花和蛋糕，预算先按500，具体时间还没定。",
            "conversation_id": conversation,
        })
        detail = client.get(f"/api/conversations/{conversation}").json()
    assert "具体哪天几点" in result["reply"]
    assert "不会默认成立即开始" in result["reply"]
    assert result["action"] is None and detail["pending_action"] is None


def test_workflow_time_guard_rejects_model_immediate_default(tmp_path):
    model = ScriptedModel([
        tool_call("create_workflow_preview", {
            "action": "arrange", "account_id": "demo-checking-001",
            "recipient_label": "家人", "budget_minor": 50000,
            "items": [{"product_id": "flowers-basic", "quantity": 1}],
        }),
        {"role": "assistant", "content": "请补充具体时间。"},
    ])
    with TestClient(create_app(
        f"sqlite:///{(tmp_path / 'workflow-guard.sqlite').as_posix()}",
        model=model,
        enable_workflow_actions=True,
        task_poll_seconds=3600,
    )) as client:
        conversation = post(client, "/api/conversations", {})["id"]
        result = post(client, "/api/chat", {
            "message": "下周末家里来人，帮我准备鲜花和蛋糕，具体时间还没定。",
            "conversation_id": conversation,
        })
        detail = client.get(f"/api/conversations/{conversation}").json()
    assert "具体哪天几点" in result["reply"]
    assert result["action"] is None and detail["pending_action"] is None
