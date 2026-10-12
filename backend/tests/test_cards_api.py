from __future__ import annotations

import json

import pytest
from app.main import create_app
from fastapi.testclient import TestClient


class OfflineModel:
    configured = True

    def __init__(self, *, create_transfer: bool = False) -> None:
        self.create_transfer = create_transfer
        self.tool_calls: list[list[str]] = []

    async def close(self) -> None:
        pass

    async def complete(self, messages, tools, emit=None):
        names = [item["function"]["name"] for item in tools]
        self.tool_calls.append(names)
        current = messages[-1]
        if self.create_transfer and current.get("role") == "user" and "转账" in current.get("content", ""):
            arguments = {
                "source_account_id": "demo-checking-001",
                "recipient_id": "demo-recipient-chen",
                "amount_minor": 250,
            }
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "offline-transfer-1",
                        "type": "function",
                        "function": {
                            "name": "create_transfer_plan",
                            "arguments": json.dumps(arguments, ensure_ascii=False),
                        },
                    }
                ],
            }
        return {"role": "assistant", "content": "离线测试模型。"}


@pytest.fixture
def p3_client(tmp_path):
    model = OfflineModel(create_transfer=True)
    app = create_app(
        f"sqlite:///{(tmp_path / 'cards-api-p3.sqlite3').as_posix()}",
        model=model,
        enable_p3_actions=True,
    )
    with TestClient(app) as client:
        yield client, model


def _conversation(client: TestClient, title: str) -> str:
    response = client.post("/api/conversations", json={"title": title})
    assert response.status_code == 200
    return response.json()["id"]


def _preview_card(
    client: TestClient,
    conversation_id: str,
    *,
    action: str = "freeze",
    card_id: str | None = "demo-card-debit-001",
    card_kind: str | None = None,
    amount_minor: int | None = None,
) -> dict:
    response = client.post(
        "/api/cards/preview",
        json={
            "action": action,
            "conversation_id": conversation_id,
            "card_id": card_id,
            "card_kind": card_kind,
            "amount_minor": amount_minor,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_cards_api_preview_decision_session_receipt_and_notification_dedup(p3_client):
    client, _model = p3_client
    conversation_id = _conversation(client, "卡片办理")

    overview = client.get("/api/cards")
    assert overview.status_code == 200
    assert len(overview.json()["cards"]) == 2
    assert overview.json()["applications"] == []

    preview = _preview_card(client, conversation_id)
    assert preview["kind"] == "card"
    assert preview["status"] == "pending"
    assert preview["conversation_id"] == conversation_id
    assert preview["before"] == {"status": "active"}
    assert preview["after"] == {"status": "frozen"}

    wrong_conversation = _conversation(client, "其他事项")
    refused = client.post(
        f"/api/card-plans/{preview['id']}/decision",
        json={"decision": "approve", "conversation_id": wrong_conversation},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "CONVERSATION_MISMATCH"
    assert client.app.state.cards.get_plan(preview["id"])["status"] == "pending"
    assert (
        next(
            card for card in client.get("/api/cards").json()["cards"] if card["id"] == "demo-card-debit-001"
        )["status"]
        == "active"
    )

    path = f"/api/card-plans/{preview['id']}/decision"
    confirmed = client.post(
        path,
        json={"decision": "approve", "conversation_id": conversation_id},
    )
    retried = client.post(
        path,
        json={"decision": "approve", "conversation_id": conversation_id},
    )
    assert confirmed.status_code == retried.status_code == 200
    assert confirmed.json() == retried.json()
    assert confirmed.json()["status"] == "completed"
    assert confirmed.json()["operation_id"]
    assert confirmed.json()["conversation_id"] == conversation_id

    detail = client.get(f"/api/conversations/{conversation_id}").json()
    original_message = next(
        message for message in detail["messages"] if (message.get("action") or {}).get("id") == preview["id"]
    )
    assert original_message["receipt"]["operation_id"] == confirmed.json()["operation_id"]
    assert original_message["receipt"]["status"] == "completed"

    notices = []
    for _ in range(2):
        response = client.get("/api/notifications")
        assert response.status_code == 200
        notices.append(
            [
                item
                for item in response.json()["notifications"]
                if item["id"] == f"card:{confirmed.json()['operation_id']}"
            ]
        )
    assert len(notices[0]) == len(notices[1]) == 1
    assert notices[0][0]["plan_id"] == preview["id"]
    assert notices[0][0]["receipt"]["operation_id"] == confirmed.json()["operation_id"]

    account_before = next(
        row for row in client.get("/api/overview").json()["accounts"] if row["id"] == "demo-checking-001"
    )["balance_minor"]
    transfer = client.post(
        "/api/chat",
        json={"message": "给陈晨转账", "conversation_id": wrong_conversation},
    )
    assert transfer.status_code == 200
    plan = transfer.json()["plan"]
    assert plan["amount_minor"] == 250
    assert (
        next(
            row for row in client.get("/api/overview").json()["accounts"] if row["id"] == "demo-checking-001"
        )["balance_minor"]
        == account_before
    )
    settled = client.post(
        f"/api/plans/{plan['id']}/decision",
        json={"decision": "approve", "conversation_id": wrong_conversation},
    )
    assert settled.status_code == 200
    assert settled.json()["status"] == "completed"
    assert (
        next(
            row for row in client.get("/api/overview").json()["accounts"] if row["id"] == "demo-checking-001"
        )["balance_minor"]
        == account_before - 250
    )
    assert (
        next(
            card for card in client.get("/api/cards").json()["cards"] if card["id"] == "demo-card-debit-001"
        )["status"]
        == "frozen"
    )


def test_card_application_api_submits_without_issuing_or_approving_a_card(p3_client):
    client, _model = p3_client
    conversation_id = _conversation(client, "申请信用卡")
    preview = _preview_card(
        client,
        conversation_id,
        action="apply_card",
        card_id=None,
        card_kind="credit",
    )
    assert preview["card_id"] is None
    assert preview["card_kind"] == "credit"
    assert preview["status"] == "pending"

    result = client.post(
        f"/api/card-plans/{preview['id']}/decision",
        json={"decision": "approve", "conversation_id": conversation_id},
    )
    assert result.status_code == 200
    assert result.json()["status"] == "completed"
    assert "待审核" in result.json()["message"]
    assert "不表示已经获批" in result.json()["message"]

    overview = client.get("/api/cards").json()
    assert len(overview["cards"]) == 2
    assert len(overview["applications"]) == 1
    assert overview["applications"][0]["card_kind"] == "credit"
    assert overview["applications"][0]["status"] == "submitted"
    assert overview["applications"][0]["operation_id"] == result.json()["operation_id"]


def test_card_preview_replaces_same_conversation_other_domain_only(p3_client):
    client, _model = p3_client
    first_conversation = _conversation(client, "第一条会话")
    second_conversation = _conversation(client, "第二条会话")

    first_subscription = client.post(
        "/api/subscriptions/demo-sub-video/preview",
        json={"action": "stop_autopay", "conversation_id": first_conversation},
    )
    second_subscription = client.post(
        "/api/subscriptions/demo-sub-music/preview",
        json={"action": "stop_autopay", "conversation_id": second_conversation},
    )
    assert first_subscription.status_code == second_subscription.status_code == 200
    first_plan_id = first_subscription.json()["id"]
    second_plan_id = second_subscription.json()["id"]

    card_preview = _preview_card(client, first_conversation)
    assert client.app.state.subscriptions.get_plan(first_plan_id)["status"] == "invalidated"
    assert client.app.state.subscriptions.get_plan(second_plan_id)["status"] == "pending"

    first_detail = client.get(f"/api/conversations/{first_conversation}").json()
    second_detail = client.get(f"/api/conversations/{second_conversation}").json()
    assert first_detail["pending_action"]["id"] == card_preview["id"]
    assert second_detail["pending_action"]["id"] == second_plan_id


def test_default_app_hides_card_actions_and_keeps_p3_tools_out_of_model_calls(tmp_path):
    model = OfflineModel()
    app = create_app(f"sqlite:///{(tmp_path / 'cards-api-p2.sqlite3').as_posix()}", model=model)
    with TestClient(app) as client:
        conversation_id = _conversation(client, "旧版界面会话")
        chat = client.post("/api/chat", json={"message": "你好", "conversation_id": conversation_id})
        assert chat.status_code == 200
        assert model.tool_calls
        p3_tool_names = {
            "list_cards",
            "create_card_preview",
            "get_wealth_overview",
            "create_wealth_preview",
            "cancel_action_preview",
        }
        assert p3_tool_names.isdisjoint(model.tool_calls[-1])
        assert p3_tool_names.isdisjoint({item["function"]["name"] for item in client.app.state.agent.tools})

        preview = _preview_card(client, conversation_id)
        assert client.app.state.cards.get_plan(preview["id"])["status"] == "pending"
        detail = client.get(f"/api/conversations/{conversation_id}")
        assert detail.status_code == 200
        assert detail.json()["pending_action"] is None
        card_message = next(
            message for message in detail.json()["messages"] if message.get("text") == preview["description"]
        )
        assert card_message["action"] is None
        assert card_message["plan"] is None
