"""Thread continuity, visible receipts and action ownership integration."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

import pytest
from app.agent import _humanize
from app.conversations import MAIN_ID, Conversation, ConversationStore
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_api import ScriptedModel, SequenceModel, call


@pytest.fixture
def client(tmp_path):
    with TestClient(
        create_app(f"sqlite:///{(tmp_path / 'p2.sqlite').as_posix()}", model=ScriptedModel())
    ) as client:
        yield client


def new_thread(client):
    return client.post("/api/conversations", json={}).json()["id"]


def test_main_unique_and_history_rename_restore(tmp_path):
    url = f"sqlite:///{(tmp_path / 'restore.sqlite').as_posix()}"
    with TestClient(create_app(url, model=ScriptedModel())) as client:
        identity = new_thread(client)
        response = client.post(
            "/api/chat", json={"message": "停止代扣就等于取消会员吗", "conversation_id": identity}
        ).json()
        assert response["message_id"]
        detail = client.get(f"/api/conversations/{identity}").json()
        assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
        assert detail["messages"][-1]["sources"] == response["sources"]
        client.patch(f"/api/conversations/{identity}", json={"title": "会员事项"})
    with TestClient(create_app(url, model=ScriptedModel())) as client:
        threads = client.get("/api/conversations").json()["conversations"]
        assert threads[0]["id"] == MAIN_ID
        assert sum(t["kind"] == "main" for t in threads) == 1
        detail = client.get(f"/api/conversations/{identity}").json()
        assert detail["conversation"]["title"] == "会员事项"
        assert detail["messages"][-1]["sources"]
        assert not any("tool_call_id" in m or "_event_id" in m for m in detail["messages"])


def test_transfer_other_thread_does_not_invalidate_and_wrong_scope_rejected(client):
    first, second = new_thread(client), new_thread(client)
    preview = client.post("/api/chat", json={"message": "给陈晨转账500元", "conversation_id": first}).json()
    action = preview["action"]
    assert action["kind"] == "transfer"
    client.post("/api/chat", json={"message": "查一下余额", "conversation_id": second})
    assert client.get(f"/api/conversations/{first}").json()["pending_action"]["id"] == action["id"]
    path = f"/api/plans/{action['id']}/decision"
    before = client.get("/api/overview").json()
    wrong = client.post(path, json={"decision": "approve", "conversation_id": second})
    assert wrong.status_code == 409
    assert client.get("/api/overview").json() == before
    receipt = client.post(path, json={"decision": "approve", "conversation_id": first}).json()
    assert receipt["status"] == "completed"
    assert client.post(path, json={"decision": "approve", "conversation_id": first}).json() == receipt
    detail = client.get(f"/api/conversations/{first}").json()
    assert detail["pending_action"] is None
    assert detail["messages"][-1]["receipt"] == receipt
    notices = client.get("/api/notifications").json()["notifications"]
    assert sum(n.get("operation_id") == receipt["operation_id"] for n in notices) == 1
    assert any(
        (m.get("receipt") or {}).get("operation_id") == receipt["operation_id"]
        for m in client.get(f"/api/conversations/{MAIN_ID}").json()["messages"]
    )
    client.post(f"/api/conversations/{MAIN_ID}/read")
    assert all(n["read"] for n in client.get("/api/notifications").json()["notifications"])


def test_subscription_preview_persists_decline_and_new_message_invalidates(client):
    identity = new_thread(client)
    before = client.get("/api/subscriptions").json()["subscriptions"]
    action = client.post(
        "/api/subscriptions/demo-sub-video/preview",
        json={"action": "stop_autopay", "conversation_id": identity},
    ).json()
    detail = client.get(f"/api/conversations/{identity}").json()
    assert detail["pending_action"]["id"] == action["id"]
    assert detail["messages"][-1]["action"]["id"] == action["id"]
    path = f"/api/subscription-plans/{action['id']}/decision"
    assert (
        client.post(path, json={"decision": "decline", "conversation_id": identity}).json()["status"]
        == "declined"
    )
    assert client.get("/api/subscriptions").json()["subscriptions"] == before
    action = client.post(
        "/api/subscriptions/demo-sub-video/preview",
        json={"action": "stop_autopay", "conversation_id": identity},
    ).json()
    client.post("/api/chat", json={"message": "查一下余额", "conversation_id": identity})
    assert client.get(f"/api/conversations/{identity}").json()["pending_action"] is None
    assert (
        client.post(
            f"/api/subscription-plans/{action['id']}/decision", json={"decision": "approve"}
        ).status_code
        == 409
    )


def test_subscription_completion_recovered_once_after_restart(tmp_path):
    url = f"sqlite:///{(tmp_path / 'sub-restore.sqlite').as_posix()}"
    with TestClient(create_app(url, model=ScriptedModel())) as client:
        identity = new_thread(client)
        action = client.post(
            "/api/subscriptions/demo-sub-video/preview",
            json={"action": "stop_autopay", "conversation_id": identity},
        ).json()
        # Simulate a crash after the operation committed but before UI notice publication.
        receipt = client.app.state.subscriptions.decide_plan(action["id"], "approve")
    with TestClient(create_app(url, model=ScriptedModel())) as client:
        for _ in range(2):
            notices = client.get("/api/notifications").json()["notifications"]
            assert sum(n.get("operation_id") == receipt["operation_id"] for n in notices) == 1
        contract = next(
            s for s in client.get("/api/subscriptions").json()["subscriptions"] if s["id"] == "demo-sub-video"
        )
        assert contract["status"] == "active" and contract["autopay_enabled"] is False
        assert (
            client.get(f"/api/conversations/{identity}").json()["messages"][-1]["receipt"]["status"]
            == "completed"
        )


def test_event_arrival_is_not_overwritten_by_inflight_history_save(tmp_path):
    store = ConversationStore(f"sqlite:///{(tmp_path / 'event.sqlite').as_posix()}")
    _, snapshot = store.load(MAIN_ID)
    event = {
        "id": "test-event",
        "title": "已完成",
        "text": "处理成功",
        "kind": "subscription",
        "source": "test",
    }
    assert store.publish(event) is True
    assert store.publish(event) is False
    store.save(MAIN_ID, snapshot + [{"role": "user", "content": "你好"}])
    _, raw = store.load(MAIN_ID)
    assert sum(m.get("_event_id") == "test-event" for m in raw) == 1
    assert len(store.detail(MAIN_ID)["messages"]) == 1
    store.close()


def test_legacy_history_preserves_order_without_tool_messages(tmp_path):
    url = f"sqlite:///{(tmp_path / 'legacy.sqlite').as_posix()}"
    store = ConversationStore(url)
    history = [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "content": json.dumps({"accounts": []})},
        {"role": "assistant", "content": "答案"},
    ]
    with Session(store.engine) as session, session.begin():
        session.add(
            Conversation(id="legacy-id", messages=json.dumps(history), updated_at="2026-10-01T00:00:00+00:00")
        )
    store.close()
    store = ConversationStore(url)
    assert [m["text"] for m in store.detail("legacy-id")["messages"]] == ["问题", "答案"]
    with Session(store.engine) as session:
        assert session.scalar(select(Conversation).where(Conversation.id == "legacy-id"))
    store.close()


def test_model_subscription_preview_and_internal_fields_stripped(tmp_path):
    model = SequenceModel(
        [
            call("list_subscriptions", {}),
            call(
                "create_subscription_preview",
                {"subscription_id": "demo-sub-music", "action": "cancel_subscription"},
            ),
            {"role": "assistant", "content": "订阅已经取消了"},
        ]
    )
    with TestClient(
        create_app(f"sqlite:///{(tmp_path / 'model-sub.sqlite').as_posix()}", model=model)
    ) as client:
        identity = new_thread(client)
        result = client.post(
            "/api/chat", json={"message": "取消云舟音乐订阅", "conversation_id": identity}
        ).json()
        assert result["action"]["kind"] == "subscription"
        assert result["plan"] is None
        assert "已经取消" not in result["reply"]
        assert (
            client.get(f"/api/conversations/{identity}").json()["pending_action"]["id"]
            == result["action"]["id"]
        )
        assert all(s["status"] == "active" for s in client.get("/api/subscriptions").json()["subscriptions"])


def test_missing_thread_readable(client):
    assert client.get("/api/conversations/missing").status_code == 404
    assert client.patch(f"/api/conversations/{MAIN_ID}", json={"title": "   "}).status_code == 422


def test_notification_metadata_never_sent_to_model(tmp_path):
    class CheckingModel(ScriptedModel):
        async def complete(self, messages, tools, emit=None):
            assert all(not any(key.startswith("_") for key in item) for item in messages)
            return await super().complete(messages, tools, emit)

    with TestClient(
        create_app(f"sqlite:///{(tmp_path / 'context-clean.sqlite').as_posix()}", model=CheckingModel())
    ) as client:
        assert (
            client.post("/api/chat", json={"message": "查一下余额", "conversation_id": MAIN_ID}).status_code
            == 200
        )


def test_unsupported_subscription_keeps_existing_transfer_preview(client):
    identity = new_thread(client)
    result = client.post("/api/chat", json={"message": "给陈晨转账500元", "conversation_id": identity}).json()
    before = client.get("/api/overview").json()
    response = client.post(
        "/api/subscriptions/demo-sub-cloud/preview",
        json={"action": "cancel_subscription", "conversation_id": identity},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CANCEL_NOT_SUPPORTED"
    assert (
        client.get(f"/api/conversations/{identity}").json()["pending_action"]["id"] == result["action"]["id"]
    )
    assert client.get("/api/overview").json() == before


def test_button_preview_waits_for_same_conversation_chat(tmp_path):
    started, release = Event(), Event()

    class WaitingModel(ScriptedModel):
        async def complete(self, messages, tools, emit=None):
            started.set()
            assert await asyncio.to_thread(release.wait, 5)
            return {"role": "assistant", "content": "你好，我在。"}

    with (
        TestClient(
            create_app(f"sqlite:///{(tmp_path / 'serialized.sqlite').as_posix()}", model=WaitingModel())
        ) as client,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        identity = new_thread(client)
        chat = pool.submit(client.post, "/api/chat", json={"message": "你好", "conversation_id": identity})
        assert started.wait(3)
        preview = pool.submit(
            client.post,
            "/api/subscriptions/demo-sub-video/preview",
            json={"action": "stop_autopay", "conversation_id": identity},
        )
        try:
            with pytest.raises(TimeoutError):
                preview.result(timeout=0.1)
        finally:
            release.set()
        assert chat.result(timeout=3).status_code == 200
        action = preview.result(timeout=3).json()
        detail = client.get(f"/api/conversations/{identity}").json()
        assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "user", "assistant"]
        assert detail["pending_action"]["id"] == action["id"]
        _, history = client.app.state.store.load(identity)
        assert len(history) == 4


def test_business_reply_uses_readable_states():
    assert _humanize("`stop_autopay` 后合同 active；`cancel_subscription` 后 cancelled。") == (
        "停止代扣 后合同 有效；取消订阅 后 已取消。"
    )
    assert _humanize("proactive 不是状态。") == "proactive 不是状态。"
