from __future__ import annotations

from app.main import create_app
from fastapi.testclient import TestClient
from test_tasks_api import QuietModel


def test_contact_http_crud_and_restart_persistence(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'contacts-api.sqlite3').as_posix()}"
    payload = {
        "name": "新联系人",
        "note": "同事",
        "client_request_id": "contacts-api-create-1",
    }
    with TestClient(create_app(database_url, model=QuietModel(), enable_tasks_actions=True)) as client:
        created = client.post("/api/contacts", json=payload)
        assert created.status_code == 200
        contact = created.json()
        assert contact["can_transfer"] is False

        listed = client.get("/api/contacts", params={"query": "同事"}).json()["contacts"]
        assert [item["id"] for item in listed] == [contact["id"]]
        selectable = client.get("/api/tasks/recipients", params={"query": "新联系人"}).json()["recipients"]
        assert selectable[0]["id"] == contact["id"]
        assert selectable[0]["can_transfer"] is False

        renamed = client.patch(
            f"/api/contacts/{contact['id']}",
            json={"version": 1, "name": "更新联系人"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "更新联系人"
        assert renamed.json()["version"] == 2

        bound = client.patch(
            f"/api/contacts/{contact['id']}",
            json={"version": 2, "bank_name": "模拟银行", "account_last4": "7788"},
        )
        assert bound.status_code == 200
        assert bound.json()["can_transfer"] is True

        removed = client.delete(f"/api/contacts/{contact['id']}", params={"version": 3})
        assert removed.status_code == 200
        assert removed.json()["contact"]["active"] is False

    with TestClient(create_app(database_url, model=QuietModel(), enable_tasks_actions=True)) as restarted:
        active = restarted.get("/api/contacts", params={"query": "更新联系人"}).json()["contacts"]
        selectable = restarted.get("/api/tasks/recipients", params={"query": "更新联系人"}).json()["recipients"]
        assert active == []
        assert selectable == []
