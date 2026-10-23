"""Full application wiring for SPEC28; deterministic tools, not provider quality scores."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.main import create_app
from fastapi.testclient import TestClient
from test_workflow_api import WorkflowModel, call


def _post(client, path, data):
    result = client.post(path, json=data)
    assert result.status_code == 200, result.text
    return result.json()


def test_plan_revision_chat_readonly_and_restart(tmp_path):
    url = f"sqlite:///{(tmp_path / 'innovation.sqlite').as_posix()}"
    model = WorkflowModel()

    def app():
        return create_app(url, model=model, enable_workflow_actions=True, task_poll_seconds=3600)

    with TestClient(app()) as client:
        cid = _post(client, "/api/conversations", {"scope": "life/arrangements"})["id"]
        original = _post(client, "/api/workflows/preview", {
            "conversation_id": cid, "action": "arrange", "account_id": "demo-checking-001",
            "recipient_label": "伴侣", "budget_minor": 50000,
            "items": [{"product_id": "flowers-basic", "quantity": 1},
                      {"product_id": "cake-basic", "quantity": 1}],
        })
        accounts = client.get("/api/overview").json()["accounts"]
        model.responses = iter([
            call("get_current_workflow_preview", {}),
            call("revise_workflow_preview", {
                "plan_id": original["id"], "items": [{"product_id": "flowers-basic", "quantity": 1}],
            }),
            {"role": "assistant", "content": "已移除蛋糕。"},
        ])
        changed = _post(client, "/api/chat", {
            "conversation_id": cid, "message": "蛋糕不要了，花保留，其他不变。",
        })["action"]
        assert changed["total_minor"] == 19900
        assert changed["recipient_label"] == "伴侣" and changed["budget_minor"] == 50000
        assert changed["revision"]["source_plan_id"] == original["id"]
        assert changed["revision"]["before_total_minor"] == 32800
        assert changed["revision"]["after_total_minor"] == 19900
        answer = _post(client, "/api/chat", {"conversation_id": cid, "message": "这个安排失败能退款吗？"})
        assert answer["sources"]
        assert client.get(f"/api/conversations/{cid}").json()["pending_action"]["id"] == changed["id"]
        invalid = client.post(f"/api/workflow-plans/{changed['id']}/revise", json={
            "conversation_id": cid, "budget_minor": 1,
        })
        assert invalid.status_code == 409
        assert client.get(f"/api/conversations/{cid}").json()["pending_action"]["id"] == changed["id"]
        revised = _post(client, f"/api/workflow-plans/{changed['id']}/revise", {
            "conversation_id": cid, "budget_minor": 30000,
        })
        history = client.get(f"/api/conversations/{cid}").json()
        assert history["messages"][-1]["action"]["id"] == revised["id"]
        assert client.get("/api/overview").json()["accounts"] == accounts

    with TestClient(app()) as client:
        current = client.get(f"/api/conversations/{cid}").json()["pending_action"]
        assert current["id"] == revised["id"] and current["revision"]
        receipt = _post(client, f"/api/workflow-plans/{current['id']}/decision", {
            "conversation_id": cid, "decision": "approve",
        })
        assert receipt
        assert client.post(f"/api/workflow-plans/{current['id']}/revise", json={
            "conversation_id": cid, "budget_minor": 40000,
        }).status_code == 409


def test_rule_chat_applies_to_new_import_and_explicit_delete(tmp_path):
    model = WorkflowModel([
        call("save_category_rule", {"merchant": "木棉咖啡", "category": "工作支出"}),
        {"role": "assistant", "content": "已保存。"},
    ])
    url = f"sqlite:///{(tmp_path / 'preferences.sqlite').as_posix()}"
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    with TestClient(create_app(url, model=model)) as client:
        before = client.get("/api/overview").json()["accounts"]
        turn = _post(client, "/api/chat", {
            "message": "以后木棉咖啡都归为工作支出", "client_request_id": "learn-pref",
        })
        rules = client.get("/api/billing/category-rules").json()["rules"]
        assert len(rules) == 1 and "工作支出" in turn["reply"]
        _post(client, "/api/billing/import", {
            "filename": "new.csv", "client_request_id": "future-bill",
            "content": f"transaction_id,date,merchant,amount,currency,transaction_type\nnew-coffee,{today},木棉咖啡,-12.50,CNY,expense\n",
        })
        imported = client.get("/api/billing/entries?source=import").json()["entries"]
        assert len(imported) == 1 and imported[0]["category"] == "工作支出"
        model.responses = iter([
            call("save_category_rule", {"merchant": "木棉咖啡", "category": "餐饮"}),
            {"role": "assistant", "content": "已保存。"},
        ])
        _post(client, "/api/chat", {"message": "这笔木棉咖啡归为餐饮", "conversation_id": turn["conversation_id"]})
        assert client.get("/api/billing/category-rules").json()["rules"] == rules
        model.responses = iter([
            call("delete_category_rule", {"rule_id": rules[0]["id"], "version": rules[0]["version"]}),
            {"role": "assistant", "content": "已删除。"},
        ])
        _post(client, "/api/chat", {"message": "删除木棉咖啡的分类规则", "conversation_id": turn["conversation_id"]})
        assert client.get("/api/billing/category-rules").json()["rules"] == []
        assert client.get("/api/overview").json()["accounts"] == before


def test_contextual_question_uses_user_history_and_can_switch_baseline(tmp_path):
    class Capture(WorkflowModel):
        async def complete(self, messages, tools, emit=None):
            self.last_context = messages
            return {"role": "assistant", "content": "请参考产品规则。"}

    model = Capture()
    with TestClient(create_app(f"sqlite:///{tmp_path / 'rag.sqlite'}", model=model)) as client:
        turn = _post(client, "/api/chat", {"message": "我想赎回理财产品"})
        result = _post(client, "/api/chat", {"message": "那多久到？", "conversation_id": turn["conversation_id"]})
        assert any("赎回" in row["excerpt"] for row in result["sources"])
        switched = _post(client, "/api/chat", {"message": "换个话题，银行卡挂失怎么办？", "conversation_id": turn["conversation_id"]})
        assert "挂失" in switched["sources"][0]["excerpt"]

    with TestClient(create_app(f"sqlite:///{tmp_path / 'baseline.sqlite'}", model=Capture(), contextual_knowledge=False)) as client:
        assert client.app.state.agent.contextual_knowledge is False
