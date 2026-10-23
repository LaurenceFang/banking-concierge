from app.main import create_app
from fastapi.testclient import TestClient
from test_tasks_api import QuietModel


def test_task_form_recipient_choices_are_real_scoped_and_disambiguated(tmp_path):
    url = f"sqlite:///{(tmp_path / 'recipients.sqlite').as_posix()}"
    with TestClient(create_app(url, model=QuietModel(), enable_tasks_actions=True)) as client:
        all_items = client.get("/api/tasks/recipients").json()["recipients"]
        assert {item["id"] for item in all_items} == {
            "demo-recipient-chen", "demo-recipient-zhang-1",
            "demo-recipient-zhang-2", "demo-recipient-li",
        }
        assert all(set(item) == {
            "id", "name", "bank_name", "account_hint", "note", "can_transfer", "version", "active"
        } for item in all_items)
        assert all(item["can_transfer"] for item in all_items)
        namesakes = client.get("/api/tasks/recipients", params={"query": "张伟"}).json()["recipients"]
        assert len(namesakes) == 2
        assert len({item["account_hint"] for item in namesakes}) == 2
        chosen = client.get("/api/tasks/recipients", params={"query": "7712"}).json()["recipients"]
        assert [item["id"] for item in chosen] == ["demo-recipient-zhang-1"]
        assert client.get("/api/tasks/recipients", params={"query": "x" * 121}).status_code == 422
        assert client.app.state.bank.find_recipients("") == []
    with TestClient(create_app(url, model=QuietModel())) as client:
        assert client.get("/api/tasks/recipients").status_code == 404
