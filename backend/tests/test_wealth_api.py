"""Cross-domain API and settlement-continuity checks, using isolated stores."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from app.conversations import ConversationStore
from app.main import create_app
from fastapi.testclient import TestClient
from test_api import ScriptedModel, SequenceModel, call

R3 = {
    "horizon": "medium",
    "loss_tolerance": "moderate",
    "experience": "basic",
    "liquidity_need": "medium",
    "objective": "balanced",
}
R1 = {
    "horizon": "short",
    "loss_tolerance": "none",
    "experience": "none",
    "liquidity_need": "high",
    "objective": "preserve",
}


@pytest.fixture
def client(tmp_path):
    with TestClient(
        create_app(
            f"sqlite:///{(tmp_path / 'wealth-api.sqlite').as_posix()}",
            model=ScriptedModel(),
            enable_p3_actions=True,
        )
    ) as client:
        yield client


def new_thread(client):
    return client.post("/api/conversations", json={}).json()["id"]


def purchase(client, identity, product="demo-wealth-flex", amount=100_000):
    return client.post(
        "/api/wealth/preview",
        json={
            "action": "purchase",
            "conversation_id": identity,
            "product_id": product,
            "account_id": "demo-checking-001",
            "amount_minor": amount,
        },
    )


def approve(client, action, identity):
    return client.post(
        f"/api/wealth-plans/{action['id']}/decision",
        json={"decision": "approve", "conversation_id": identity},
    )


def test_missing_risk_and_invalid_parameters_leave_cash_unchanged(client):
    identity = new_thread(client)
    before = client.get("/api/overview").json()
    assert client.get("/api/wealth").json()["risk_profile"] is None
    assert all(not p["eligible"] for p in client.get("/api/wealth").json()["products"])
    assert purchase(client, identity).status_code == 409
    assert client.post("/api/wealth/risk-profile", json={"answers": {"horizon": "short"}}).status_code == 409
    result = client.post("/api/wealth/risk-profile", json={"answers": R1}).json()
    assert result["risk_level"] == 1
    assert purchase(client, identity, "demo-wealth-growth", 1_000_000).status_code == 409
    assert purchase(client, identity, amount=100_000.0).status_code == 422
    assert client.post("/api/wealth/demo/advance", json={"business_days": True}).status_code == 422
    assert client.get("/api/overview").json() == before
    assert client.get("/api/wealth").json()["orders"] == []


def test_purchase_redemption_and_receipts_are_durable_without_early_cash(tmp_path):
    url = f"sqlite:///{(tmp_path / 'wealth-durable.sqlite').as_posix()}"
    with TestClient(create_app(url, model=ScriptedModel(), enable_p3_actions=True)) as client:
        identity = new_thread(client)
        client.post("/api/wealth/risk-profile", json={"answers": R3})
        before = client.get("/api/overview").json()["accounts"]
        action = purchase(client, identity).json()
        assert client.get("/api/overview").json()["accounts"] == before
        other = new_thread(client)
        assert approve(client, action, other).status_code == 409
        receipt = approve(client, action, identity).json()
        assert receipt["status"] == "completed" and receipt["order_status"] == "pending_settlement"
        assert approve(client, action, identity).json() == receipt
        overview = client.get("/api/wealth").json()
        assert overview["positions"] == []
        assert (
            client.get("/api/overview").json()["accounts"][0]["balance_minor"]
            == before[0]["balance_minor"] - 100_000
        )
        client.post("/api/wealth/demo/advance", json={"business_days": 1})
        detail = client.get(f"/api/conversations/{identity}").json()
        assert detail["messages"][-1]["receipt"]["order_status"] == "settled"
        # A concurrent GET might have captured this pre-settlement acceptance event.
        client.app.state.store.note_decision(action["id"], receipt["message"], receipt)
        assert client.app.state.store.detail(identity)["messages"][-1]["receipt"]["order_status"] == "settled"
        _, raw = client.app.state.store.load(identity)
        assert "已结算" in next(m["content"] for m in raw if m.get("_event_id") == f"decision:{action['id']}")
    with TestClient(create_app(url, model=ScriptedModel(), enable_p3_actions=True)) as client:
        holding = client.get("/api/wealth").json()["positions"][0]
        cash = client.get("/api/overview").json()["accounts"][0]["balance_minor"]
        redeem = client.post(
            "/api/wealth/preview",
            json={
                "action": "redeem",
                "conversation_id": identity,
                "position_id": holding["id"],
                "amount_minor": 100_000,
            },
        ).json()
        receipt = approve(client, redeem, identity).json()
        assert receipt["order_status"] == "pending_settlement"
        assert client.get("/api/overview").json()["accounts"][0]["balance_minor"] == cash
        assert client.get("/api/wealth").json()["positions"][0]["available_minor"] == 0
        client.post("/api/wealth/demo/advance", json={"business_days": 1})
        assert client.get("/api/overview").json()["accounts"] == before
        for _ in range(2):
            notices = client.get("/api/notifications").json()["notifications"]
            assert len([n for n in notices if n["kind"] == "wealth"]) == 4
        assert client.get(f"/api/wealth/orders/{receipt['order_id']}").json()["status"] == "settled"
        assert client.get("/api/wealth/orders/missing").status_code == 404


def test_wealth_principal_excluded_from_bills_and_fees_included(client):
    identity = new_thread(client)
    client.post("/api/wealth/risk-profile", json={"answers": R3})
    baseline = client.get("/api/billing/report").json()
    action = purchase(client, identity, "demo-wealth-balance", 500_000).json()
    assert action["fee_minor"] == 1500
    approve(client, action, identity)
    report = client.get("/api/billing/report").json()
    assert report["income_minor"] == baseline["income_minor"]
    assert report["net_expense_minor"] == baseline["net_expense_minor"] + 1500
    client.post("/api/wealth/demo/advance", json={"business_days": 1})
    holding = client.get("/api/wealth").json()["positions"][0]
    payload = {
        "action": "redeem",
        "conversation_id": identity,
        "position_id": holding["id"],
        "amount_minor": 500_000,
    }
    assert client.post("/api/wealth/preview", json=payload).status_code == 409
    client.post("/api/wealth/demo/advance", json={"business_days": 200})
    redeem = client.post("/api/wealth/preview", json=payload).json()
    assert redeem["fee_minor"] == 500 and redeem["payout_minor"] == 499_500
    approve(client, redeem, identity)
    client.post("/api/wealth/demo/advance", json={"business_days": 1})
    report = client.get("/api/billing/report").json()
    assert report["income_minor"] == baseline["income_minor"]
    assert report["net_expense_minor"] == baseline["net_expense_minor"] + 2000
    assert next(c["amount_minor"] for c in report["categories"] if c["category"] == "金融服务") == 2000


def test_risk_change_between_preview_and_approval_does_not_debit(client):
    identity = new_thread(client)
    client.post("/api/wealth/risk-profile", json={"answers": R3})
    action = purchase(client, identity, "demo-wealth-balance", 500_000).json()
    before = client.get("/api/overview").json()
    client.post("/api/wealth/risk-profile", json={"answers": R1})
    result = approve(client, action, identity).json()
    assert result["status"] == "declined"
    assert client.get("/api/overview").json() == before
    assert client.get("/api/wealth").json()["orders"] == []


def test_p3_model_tools_use_service_previews_and_never_approval(tmp_path):
    model = SequenceModel(
        [
            call("list_cards", {}),
            call("create_card_preview", {"action": "freeze", "card_id": "demo-card-debit-001"}),
            {"role": "assistant", "content": "冻结已经完成"},
            call("get_wealth_overview", {}),
            call(
                "create_wealth_preview",
                {"action": "purchase", "product_id": "demo-wealth-flex", "amount_minor": 100_000},
            ),
            {"role": "assistant", "content": "申购已经到账"},
        ]
    )
    with TestClient(
        create_app(f"sqlite:///{(tmp_path / 'tools.sqlite').as_posix()}", model=model, enable_p3_actions=True)
    ) as client:
        identity = new_thread(client)
        cards = client.post("/api/chat", json={"message": "冻结日常卡", "conversation_id": identity}).json()
        assert cards["action"]["kind"] == "card"
        assert "已经完成" not in cards["reply"]
        assert all(c["status"] == "active" for c in client.get("/api/cards").json()["cards"])
        client.post("/api/wealth/risk-profile", json={"answers": R3})
        before = client.get("/api/overview").json()
        wealth = client.post(
            "/api/chat", json={"message": "申购灵活资金1000元", "conversation_id": identity}
        ).json()
        assert wealth["action"]["kind"] == "wealth"
        assert "已经到账" not in wealth["reply"]
        assert client.get("/api/overview").json() == before
        assert (
            client.get(f"/api/conversations/{identity}").json()["pending_action"]["id"]
            == wealth["action"]["id"]
        )
        assert client.app.state.cards.get_plan(cards["action"]["id"])["status"] == "invalidated"


def test_disabled_model_cannot_leak_unsupported_action(tmp_path):
    model = SequenceModel(
        [
            call(
                "create_wealth_preview",
                {"action": "purchase", "product_id": "demo-wealth-flex", "amount_minor": 100_000},
            ),
            {"role": "assistant", "content": "已经为你申购成功。"},
        ]
    )
    with TestClient(create_app(f"sqlite:///{(tmp_path / 'off.sqlite').as_posix()}", model=model)) as client:
        result = client.post("/api/chat", json={"message": "申购1000元"}).json()
        assert result["action"] is None
        assert result["reply"] == "当前不能执行这项操作。"


def test_clock_and_settlement_do_not_undo_newer_thread_event(tmp_path):
    store = ConversationStore(f"sqlite:///{(tmp_path / 'merge.sqlite').as_posix()}")
    identity = store.create()["id"]
    store.append_assistant(
        identity, {"reply": "确认单", "action": {"id": "test-plan", "kind": "wealth", "status": "pending"}}
    )
    pending = {
        "status": "completed",
        "order_id": "order-one",
        "order_status": "pending_settlement",
        "message": "已受理",
    }
    settled = {**pending, "order_status": "settled", "message": "已结算，持仓已入账"}
    store.note_decision("test-plan", pending["message"], pending)
    _, in_flight = store.load(identity)
    store.note_decision("test-plan", settled["message"], settled)
    store.save(identity, in_flight)
    _, merged = store.load(identity)
    assert "已结算" in merged[-1]["content"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(store.note_decision, "test-plan", pending["message"], pending) for _ in range(3)
        ]
        for future in futures:
            future.result()
    assert store.detail(identity)["messages"][0]["receipt"] == settled
    assert "已结算" in store.load(identity)[1][-1]["content"]
    before_refresh = store.detail(identity)["conversation"]["updated_at"]
    store.note_decision("test-plan", settled["message"], settled)
    assert store.detail(identity)["conversation"]["updated_at"] == before_refresh
    store.close()
