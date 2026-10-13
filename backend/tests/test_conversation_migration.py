"""Preserve conversations and retry receipts when upgrading early v2 databases."""
from app.conversations import ConversationStore
from sqlalchemy import create_engine, inspect, text


def test_upgrade_adds_retry_action_without_losing_history(tmp_path):
    url = f"sqlite:///{(tmp_path / 'old-workspace.sqlite').as_posix()}"
    store = ConversationStore(url)
    original = store.create("保留的对话", client_request_id="original-create")
    store.append_user(original["id"], "已有内容")
    store.close()
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE chat_client_request_receipts DROP COLUMN pending_action"))
    engine.dispose()
    for _ in range(2):
        upgraded = ConversationStore(url)
        try:
            assert "pending_action" in {c["name"] for c in inspect(upgraded.engine).get_columns("chat_client_request_receipts")}
            assert upgraded.create("保留的对话", client_request_id="original-create")["id"] == original["id"]
            assert upgraded.detail(original["id"])["messages"][0]["text"] == "已有内容"
        finally:
            upgraded.close()
