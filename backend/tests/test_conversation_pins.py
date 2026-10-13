"""Pinning changes a conversation's placement, never its history or business scope."""
import pytest
from app.conversations import MAIN_ID, ConversationError, ConversationStore
from app.pin_routes import conversation_pin_router
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_pin_survives_restart_and_filters_without_changing_activity(tmp_path):
    url = f"sqlite:///{(tmp_path / 'pin.sqlite').as_posix()}"
    store = ConversationStore(url)
    first = store.create("聚餐AA", scope="bills/payments")
    store.associate_scope(first["id"], "bills/entries")
    store.append_user(first["id"], "这是原来的消息")
    before = store.detail(first["id"])
    pinned = store.set_pinned(first["id"], True)
    assert pinned["pinned"] is True
    assert pinned["updated_at"] == before["conversation"]["updated_at"]
    assert store.set_pinned(first["id"], True)["pinned_at"] == pinned["pinned_at"]
    store.close()
    store = ConversationStore(url)
    try:
        for scope in (None, "bills/payments", "bills/entries"):
            items = [c for c in store.list(scope) if c["id"] == first["id"]]
            assert len(items) == 1 and items[0]["pinned_at"] == pinned["pinned_at"]
        assert first["id"] not in {c["id"] for c in store.list("accounts/cards")}
        assert store.detail(first["id"])["messages"] == before["messages"]
        assert store.set_pinned(first["id"], False)["pinned_at"] is None
        assert store.set_pinned(first["id"], False)["pinned"] is False
        assert store.detail(first["id"])["conversation"]["updated_at"] == pinned["updated_at"]
    finally:
        store.close()


def test_pin_router_requires_boolean_and_rejects_fixed_main(tmp_path):
    store = ConversationStore(f"sqlite:///{(tmp_path / 'router.sqlite').as_posix()}")
    app = FastAPI()
    app.state.store = store
    app.include_router(conversation_pin_router(app))
    item = store.create("订阅", scope="life/subscriptions")
    try:
        with TestClient(app) as client:
            path = f"/api/conversations/{item['id']}/pin"
            for invalid in ("true", 1, None):
                assert client.patch(path, json={"pinned": invalid}).status_code == 422
            assert client.patch(path, json={"pinned": True}).json()["pinned"] is True
            assert client.patch(path, json={"pinned": False}).json()["pinned"] is False
            response = client.patch(f"/api/conversations/{MAIN_ID}/pin", json={"pinned": True})
            assert response.status_code == 409
        with pytest.raises(ConversationError, match="没有找到"):
            store.set_pinned("not-a-conversation", True)
        assert not store.detail(MAIN_ID)["conversation"]["pinned"]
    finally:
        store.close()
