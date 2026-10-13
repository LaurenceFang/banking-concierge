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


def test_readonly_query_preserves_old_preview(client):
    first = client.post("/api/chat", json={"message": "给陈晨转账500元"}).json()
    client.post("/api/chat", json={"message": "查一下余额", "conversation_id": first["conversation_id"]})
    detail = client.get(f"/api/conversations/{first['conversation_id']}").json()
    assert detail["pending_action"]["id"] == first["plan"]["id"]
    response = client.post(
        f"/api/plans/{first['plan']['id']}/decision",
        json={"decision": "approve", "conversation_id": first["conversation_id"]},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "completed"


def test_stream_events_and_input_validation(client):
    response = client.post("/api/chat/stream", json={"message": "停止代扣就是取消会员吗"})
    assert response.status_code == 200
    assert "event: conversation" in response.text
    assert "event: status" in response.text
    assert "event: result" in response.text and "event: done" in response.text
    assert client.post("/api/chat", json={"message": "   "}).status_code == 422
    assert client.post("/api/chat", json={"message": "你好", "conversation_id": "../other"}).status_code == 422


def test_changed_transfer_preview_invalidates_the_old_confirmation(tmp_path):
    class TwoTransferPreviews(ScriptedModel):
        def __init__(self):
            self.responses = iter(
                [
                    call("create_transfer_plan", {
                        "source_account_id": "demo-checking-001",
                        "recipient_id": "demo-recipient-chen",
                        "amount_minor": 50_000,
                    }),
                    {"role": "assistant", "content": "请核对转账确认单。"},
                    call("create_transfer_plan", {
                        "source_account_id": "demo-checking-001",
                        "recipient_id": "demo-recipient-chen",
                        "amount_minor": 75_000,
                    }),
                    {"role": "assistant", "content": "请核对更新后的转账确认单。"},
                ]
            )

        async def complete(self, messages, tools, emit=None):
            return next(self.responses)

    url = f"sqlite:///{(tmp_path / 'changed-preview.sqlite').as_posix()}"
    with TestClient(create_app(url, model=TwoTransferPreviews())) as client:
        conversation_id = client.post("/api/conversations", json={}).json()["id"]
        old = client.post(
            "/api/chat", json={"message": "给陈晨转500元", "conversation_id": conversation_id}
        ).json()
        new = client.post(
            "/api/chat", json={"message": "改成给陈晨转750元", "conversation_id": conversation_id}
        ).json()
        assert old["action"]["amount_minor"] == 50_000
        assert new["action"]["amount_minor"] == 75_000
        assert client.get(f"/api/conversations/{conversation_id}").json()["pending_action"]["id"] == new["action"]["id"]
        stale = client.post(
            f"/api/plans/{old['action']['id']}/decision",
            json={"decision": "approve", "conversation_id": conversation_id},
        )
        assert stale.status_code == 409


def test_provider_error_is_user_readable(tmp_path):
    app = create_app(f"sqlite:///{(tmp_path / 'timeout.sqlite').as_posix()}", model=UnavailableModel())
    with TestClient(app) as client:
        response = client.post("/api/chat", json={"message": "你好"})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "PROVIDER_TIMEOUT"


def test_failed_first_send_retry_keeps_one_user_message_and_does_not_call_model_again(tmp_path):
    class CountingUnavailableModel(UnavailableModel):
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, tools, emit=None):
            self.calls += 1
            return await super().complete(messages, tools, emit)

    model = CountingUnavailableModel()
    url = f"sqlite:///{(tmp_path / 'failed-first-send.sqlite').as_posix()}"
    payload = {
        "message": "查一下账单",
        "scope": "bills/entries",
        "client_request_id": "failed-send-20261013-001",
    }
    with TestClient(create_app(url, model=model)) as client:
        first = client.post("/api/chat", json=payload)
        retry = client.post("/api/chat", json=payload)
        assert first.status_code == retry.status_code == 503
        assert first.json() == retry.json()
        assert model.calls == 1
        items = client.get("/api/conversations?scope=bills/entries").json()["conversations"]
        ordinary = [item for item in items if item["kind"] != "main"]
        assert len(ordinary) == 1
        detail = client.get(f"/api/conversations/{ordinary[0]['id']}").json()
        assert [message["role"] for message in detail["messages"]] == ["user"]


def test_explicit_reply_retry_reuses_the_pending_action_and_does_not_append_user_again(tmp_path):
    class FailAfterCreatingTransfer(ScriptedModel):
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, tools, emit=None):
            self.calls += 1
            if self.calls == 1:
                return call("create_transfer_plan", {
                    "source_account_id": "demo-checking-001",
                    "recipient_id": "demo-recipient-chen",
                    "amount_minor": 50_000,
                })
            raise ModelError("PROVIDER_TIMEOUT", "模型暂时超时，请重试。")

    model = FailAfterCreatingTransfer()
    url = f"sqlite:///{(tmp_path / 'retry-existing-plan.sqlite').as_posix()}"
    payload = {
        "message": "给陈晨转账500元",
        "scope": "bills/payments",
        "client_request_id": "transfer-reply-retry-001",
    }
    with TestClient(create_app(url, model=model)) as client:
        first = client.post("/api/chat/stream", json=payload)
        assert "event: conversation" in first.text
        assert "event: error" in first.text
        conversation_id = client.app.state.store.request_conversation_id(None, payload["client_request_id"])
        original = client.app.state.bank.pending_for_conversation(conversation_id)
        assert original is not None

        retried = client.post("/api/chat", json={**payload, "retry_failed": True})
        assert retried.status_code == 200
        result = retried.json()
        assert result["action"]["id"] == original["id"]
        assert model.calls == 2
        detail = client.get(f"/api/conversations/{conversation_id}").json()
        assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]
        assert detail["pending_action"]["id"] == original["id"]
        _, raw_history = client.app.state.store.load(conversation_id)
        assert [message["role"] for message in raw_history] == ["user", "assistant"]
        cached = client.post("/api/chat", json=payload)
        assert cached.status_code == 200 and cached.json() == result
        assert model.calls == 2


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
