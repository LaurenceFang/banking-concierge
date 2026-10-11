"""Offline API integration tests; live DS/browser checks are performed separately."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from app.llm import ModelError
from app.main import create_app
from fastapi.testclient import TestClient


def call(name: str, arguments: dict) -> dict:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"test-{name}", "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}


class ScriptedModel:
    configured = True

    async def close(self):
        pass

    async def complete(self, messages, tools, emit=None):
        current = messages[-1]
        if current["role"] == "user":
            message = current["content"]
            if "余额" in message:
                return call("list_accounts", {})
            if "转账" in message:
                return call("create_transfer_plan", {
                    "source_account_id": "demo-checking-001", "recipient_id": "demo-recipient-chen",
                    "amount_minor": 50000})
            reply = "停止代扣和取消商户会员是两个不同动作。"
        else:
            # Deliberately wrong numerical paraphrase must never override tool facts.
            reply = "储蓄余额 ¥160,008.32，转账已经完成，金额一万元。"
        if emit:
            await emit("token", {"text": reply})
        return {"role": "assistant", "content": reply}


class UnavailableModel(ScriptedModel):
    async def complete(self, messages, tools, emit=None):
        raise ModelError("PROVIDER_TIMEOUT", "模型暂时超时，请重试。")


@pytest.fixture
def client(tmp_path):
    app = create_app(f"sqlite:///{(tmp_path / 'api.sqlite').as_posix()}", model=ScriptedModel())
    with TestClient(app) as client:
        yield client


def test_overview_and_health(client):
    health = client.get("/api/health").json()
    assert health["model"] == "deepseek-flash"
    assert health["knowledge_documents"] == 3
    overview = client.get("/api/overview").json()
    assert len(overview["accounts"]) == 2
    assert len(overview["transactions"]) == 12


def test_balance_reply_uses_actual_tool_amounts(client):
    response = client.post("/api/chat", json={"message": "查一下余额"})
    assert response.status_code == 200
    reply = response.json()["reply"]
    accounts = client.get("/api/overview").json()["accounts"]
    for account in accounts:
        assert f"¥{Decimal(account['balance_minor']) / 100:,.2f}" in reply
    assert f"¥{Decimal(sum(row['balance_minor'] for row in accounts)) / 100:,.2f}" in reply
    assert "160,008.32" not in reply


def test_rag_retrieval_is_included_even_when_model_does_not_call_search(client):
    response = client.post("/api/chat", json={"message": "停止代扣就等于取消会员吗"})
    assert response.status_code == 200
    result = response.json()
    assert result["sources"]
    assert any("取消" in source["excerpt"] for source in result["sources"])
    assert "plan" in result and result["plan"] is None


def test_transfer_preview_approval_and_retry(client):
    before = client.get("/api/overview").json()
    response = client.post("/api/chat", json={"message": "给陈晨转账500元"}).json()
    assert response["plan"]["amount_minor"] == 50000
    assert "已经完成" not in response["reply"]
    assert client.get("/api/overview").json()["accounts"] == before["accounts"]
    path = f"/api/plans/{response['plan']['id']}/decision"
    first = client.post(path, json={"decision": "approve"}).json()
    second = client.post(path, json={"decision": "approve"}).json()
    assert first["operation_id"] == second["operation_id"]
    after = client.get("/api/overview").json()
    assert after["accounts"][0]["balance_minor"] == before["accounts"][0]["balance_minor"] - 50000
    assert len(after["transactions"]) == len(before["transactions"]) + 1


def test_new_message_invalidates_old_preview(client):
    first = client.post("/api/chat", json={"message": "给陈晨转账500元"}).json()
    client.post("/api/chat", json={"message": "查一下余额", "conversation_id": first["conversation_id"]})
    response = client.post(f"/api/plans/{first['plan']['id']}/decision", json={"decision": "approve"})
    assert response.status_code == 409


def test_stream_events_and_input_validation(client):
    response = client.post("/api/chat/stream", json={"message": "停止代扣就是取消会员吗"})
    assert response.status_code == 200
    assert "event: status" in response.text
    assert "event: result" in response.text and "event: done" in response.text
    assert client.post("/api/chat", json={"message": "   "}).status_code == 422
    assert client.post("/api/chat", json={"message": "你好", "conversation_id": "../other"}).status_code == 422


def test_provider_error_is_user_readable(tmp_path):
    app = create_app(f"sqlite:///{(tmp_path / 'timeout.sqlite').as_posix()}", model=UnavailableModel())
    with TestClient(app) as client:
        response = client.post("/api/chat", json={"message": "你好"})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "PROVIDER_TIMEOUT"


class SequenceModel(ScriptedModel):
    def __init__(self, responses):
        self.responses = iter(responses)

    async def complete(self, messages, tools, emit=None):
        return next(self.responses)


@pytest.mark.parametrize("failure", [False, True])
def test_transfer_context_is_not_replaced_by_balance_table(tmp_path, failure):
    responses = [call("list_accounts", {}), call("find_recipients", {"query": "张伟"})]
    if failure:
        responses.append(call("create_transfer_plan", {
            "source_account_id": "demo-checking-001", "recipient_id": "demo-recipient-chen",
            "amount_minor": 99999999}))
    responses.append({"role": "assistant", "content": "有两位张伟，请选择银行和尾号。"})
    app = create_app(f"sqlite:///{(tmp_path / 'context.sqlite').as_posix()}", model=SequenceModel(responses))
    with TestClient(app) as client:
        before = client.get("/api/overview").json()["accounts"]
        result = client.post("/api/chat", json={"message": "给张伟转账"}).json()
        assert result["plan"] is None
        assert "余额如下" not in result["reply"]
        assert ("不足" in result["reply"]) if failure else ("两位张伟" in result["reply"])
        assert client.get("/api/overview").json()["accounts"] == before


def test_transfer_balance_precheck_preserves_next_step(tmp_path):
    model = SequenceModel([call("list_accounts", {}),
                           {"role": "assistant", "content": "账户余额不足，请调整金额后再生成预览。"}])
    app = create_app(f"sqlite:///{(tmp_path / 'precheck.sqlite').as_posix()}", model=model)
    with TestClient(app) as client:
        result = client.post("/api/chat", json={"message": "用日常账户转给陈晨，请生成预览"}).json()
        assert result["reply"] == "账户余额不足，请调整金额后再生成预览。"
        assert result["plan"] is None
