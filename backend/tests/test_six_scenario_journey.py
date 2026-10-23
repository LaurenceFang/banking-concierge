"""One persistent application journey across domains; model behavior tested separately."""

from datetime import datetime, timedelta

from app.main import create_app
from fastapi.testclient import TestClient
from test_api import ScriptedModel
from test_wealth_api import R3


def test_shared_conversation_six_domains_and_restart(tmp_path):
    url = f"sqlite:///{(tmp_path / 'journey.sqlite').as_posix()}"

    def application():
        return create_app(
            url,
            model=ScriptedModel(),
            enable_p3_actions=True,
            enable_tasks_actions=True,
            enable_workflow_actions=True,
            workflow_fault_mode="refund_failure",
            task_poll_seconds=3600,
        )

    def post(client, path, data):
        response = client.post(path, json=data)
        assert response.status_code == 200, response.text
        return response.json()

    def cash(client):
        return next(
            a for a in client.get("/api/overview").json()["accounts"] if a["id"] == "demo-checking-001"
        )

    def decide(client, kind, plan, conversation, decision="approve"):
        prefix = "plans" if kind == "transfer" else f"{kind}-plans"
        return post(
            client,
            f"/api/{prefix}/{plan['id']}/decision",
            {"decision": decision, "conversation_id": conversation},
        )

    with TestClient(application()) as client:
        cid = post(client, "/api/conversations", {})["id"]
        other = post(client, "/api/conversations", {})["id"]
        baseline = cash(client)["balance_minor"]
        bill_before = client.get("/api/billing/report").json()["net_expense_minor"]
        transfer = post(client, "/api/chat", {"message": "给陈晨转账500元", "conversation_id": cid})["plan"]
        post(client, "/api/chat", {"message": "查余额", "conversation_id": cid})
        assert client.get(f"/api/conversations/{cid}").json()["pending_action"]["id"] == transfer["id"]
        receipt = decide(client, "transfer", transfer, cid)
        assert decide(client, "transfer", transfer, cid)["operation_id"] == receipt["operation_id"]
        assert cash(client)["balance_minor"] == baseline - 50000
        # Transfers appear in the ledger, but are excluded from consumption totals.
        assert client.get("/api/billing/report").json()["net_expense_minor"] == bill_before
        entry = next(
            row
            for row in client.get("/api/billing/entries").json()["entries"]
            if row["id"] == f"{receipt['operation_id']}-debit"
        )
        assert entry["amount_minor"] == -50000 and entry["transaction_kind"] == "transfer"

        # A valid new domain invalidates only this conversation's unconfirmed action.
        stale = post(client, "/api/chat", {"message": "再给陈晨转账500元", "conversation_id": cid})["plan"]
        independent = post(client, "/api/chat", {"message": "给陈晨转账500元", "conversation_id": other})[
            "plan"
        ]
        card = post(
            client,
            "/api/cards/preview",
            {"conversation_id": cid, "action": "freeze", "card_id": "demo-card-debit-001"},
        )
        assert (
            client.post(
                f"/api/plans/{stale['id']}/decision", json={"decision": "approve", "conversation_id": cid}
            ).status_code
            == 409
        )
        assert client.get(f"/api/conversations/{other}").json()["pending_action"]["id"] == independent["id"]
        assert (
            client.post(
                f"/api/card-plans/{card['id']}/decision",
                json={"decision": "approve", "conversation_id": other},
            ).status_code
            == 409
        )
        decide(client, "card", card, cid)
        assert (
            next(c for c in client.get("/api/cards").json()["cards"] if c["id"] == "demo-card-debit-001")[
                "status"
            ]
            == "frozen"
        )

        sub = post(
            client,
            "/api/subscriptions/demo-sub-video/preview",
            {"conversation_id": cid, "action": "stop_autopay"},
        )
        decide(client, "subscription", sub, cid)
        subscription = next(
            s for s in client.get("/api/subscriptions").json()["subscriptions"] if s["id"] == "demo-sub-video"
        )
        assert subscription["status"] == "active" and subscription["autopay_enabled"] is False

        post(client, "/api/wealth/risk-profile", {"answers": R3})
        wealth = post(
            client,
            "/api/wealth/preview",
            {
                "conversation_id": cid,
                "action": "purchase",
                "product_id": "demo-wealth-flex",
                "account_id": "demo-checking-001",
                "amount_minor": 100000,
            },
        )
        assert decide(client, "wealth", wealth, cid)["order_status"] == "pending_settlement"
        assert cash(client)["balance_minor"] == baseline - 150000

        workflow = post(
            client,
            "/api/workflows/preview",
            {
                "conversation_id": cid,
                "action": "arrange",
                "recipient_label": "家人",
                "account_id": "demo-checking-001",
                "budget_minor": 50000,
                "items": [
                    {"product_id": "flowers-basic", "quantity": 1},
                    {"product_id": "cake-basic", "quantity": 1},
                ],
            },
        )
        decide(client, "workflow", workflow, cid)
        for _ in range(8):
            client.app.state.task_tick()
        task = client.get("/api/workflows").json()["tasks"][0]
        assert task["refund_pending_minor"] == 19900
        assert task["net_spent_minor"] == 19900
        assert cash(client)["reserved_minor"] == 0
        workflow_id = task["id"]

        clock = datetime.fromisoformat(client.get("/api/tasks/clock").json()["now"])
        scheduled = post(
            client,
            "/api/tasks/preview",
            {
                "conversation_id": cid,
                "action": "schedule_transfer",
                "account_id": "demo-checking-001",
                "recipient_id": "demo-recipient-chen",
                "amount_minor": 500,
                "execute_at": (clock + timedelta(minutes=30)).isoformat(),
            },
        )
        decide(client, "task", scheduled, cid)
        assert cash(client)["reserved_minor"] == 500
        # A subsequent preview must not cancel the already-authorized schedule.
        cancelled = post(
            client,
            "/api/cards/preview",
            {"conversation_id": cid, "action": "unfreeze", "card_id": "demo-card-debit-001"},
        )
        decide(client, "card", cancelled, cid, "decline")
        assert cash(client)["reserved_minor"] == 500
        # Insufficient funds must leave other conversations and cash intact.
        before_invalid = cash(client)
        denied = client.post(
            "/api/workflows/preview",
            json={
                "conversation_id": cid,
                "action": "arrange",
                "recipient_label": "家人",
                "account_id": "demo-checking-001",
                "budget_minor": 1,
                "items": [{"product_id": "flowers-basic", "quantity": 1}],
            },
        )
        assert denied.status_code == 409
        assert cash(client) == before_invalid

    # Reopen the actual same database with every feature enabled.
    with TestClient(application()) as client:
        assert cash(client)["reserved_minor"] == 500
        post(client, f"/api/workflows/{workflow_id}/recover", {})
        task = client.get("/api/workflows").json()["tasks"][0]
        assert task["status"] == "failed"
        assert task["refunded_minor"] == 19900 and task["net_spent_minor"] == 0
        assert len([s for s in task["steps"] if s["order_id"]]) == 1
        post(client, "/api/tasks/demo/advance", {"minutes": 31})
        assert cash(client)["reserved_minor"] == 0
        assert cash(client)["balance_minor"] == baseline - 150500
        post(client, "/api/wealth/demo/advance", {"business_days": 1})
        holding = client.get("/api/wealth").json()["positions"][0]
        redeem = post(
            client,
            "/api/wealth/preview",
            {
                "conversation_id": cid,
                "action": "redeem",
                "position_id": holding["id"],
                "amount_minor": 100000,
            },
        )
        decide(client, "wealth", redeem, cid)
        assert cash(client)["balance_minor"] == baseline - 150500
        post(client, "/api/wealth/demo/advance", {"business_days": 1})
        assert cash(client)["balance_minor"] == baseline - 50500
        assert client.get("/api/billing/report").json()["net_expense_minor"] == bill_before
        detail = client.get(f"/api/conversations/{cid}").json()
        receipts = {
            m["action"]["id"]: m["receipt"]
            for m in detail["messages"]
            if m.get("action") and m.get("receipt")
        }
        assert receipts[workflow["id"]]["refunded_minor"] == 19900
        assert receipts[wealth["id"]]["order_status"] == "settled"
        assert detail["pending_action"] is None
        assert client.get(f"/api/conversations/{other}").json()["pending_action"]["id"] == independent["id"]
