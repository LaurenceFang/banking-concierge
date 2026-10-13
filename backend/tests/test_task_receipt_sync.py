from app.conversations import ConversationStore


def test_task_progress_preserves_confirmation_status_and_rejects_old_versions(tmp_path):
    store = ConversationStore(f"sqlite:///{(tmp_path / 'events.sqlite').as_posix()}")
    try:
        identity = store.create()["id"]
        store.append_assistant(identity, {
            "reply": "请确认",
            "action": {"id": "plan-1", "kind": "task", "status": "pending"},
        })
        scheduled = {"task_id": "task-1", "task_status": "scheduled", "state_version": 1}
        store.note_decision("plan-1", "已安排，尚未扣款", scheduled)
        message = store.detail(identity)["messages"][0]
        assert message["action"]["status"] == "completed"
        assert message["receipt"]["task_status"] == "scheduled"
        assert message["receipt"]["message"] == "已安排，尚未扣款"
        completed = {"task_id": "task-1", "task_status": "completed", "state_version": 2}
        store.note_decision("plan-1", "到期转账已完成", completed)
        store.note_decision("plan-1", "已安排，尚未扣款", scheduled)
        latest = store.detail(identity)["messages"][0]["receipt"]
        assert latest["state_version"] == 2
        assert latest["task_status"] == "completed"
        assert latest["message"] == "到期转账已完成"
    finally:
        store.close()
