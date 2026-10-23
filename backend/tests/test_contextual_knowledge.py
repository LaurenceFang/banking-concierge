from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from app.knowledge import KnowledgeService
from app.main import create_app
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_KNOWLEDGE_DIR = REPO_ROOT / "knowledge"


def test_contextual_follow_up_inherits_transfer_subject() -> None:
    service = KnowledgeService(SEED_KNOWLEDGE_DIR)

    results = service.search_contextual(
        "那手续费呢？",
        ["我想从日常借记卡给陈晨转3.21元，先看一下确认单。"],
        limit=4,
    )

    assert results
    assert any("模拟转账手续费" in row["title"] for row in results)


def test_contextual_follow_up_inherits_wealth_subject() -> None:
    service = KnowledgeService(SEED_KNOWLEDGE_DIR)

    results = service.search_contextual(
        "那大概多久到账？",
        ["我想了解平衡180计划赎回后什么时候到账。"],
        limit=4,
    )

    assert results
    assert any("赎回后什么时候到账" in row["title"] for row in results)


def test_contextual_follow_up_inherits_csv_undo_subject() -> None:
    service = KnowledgeService(SEED_KNOWLEDGE_DIR)

    results = service.search_contextual(
        "这批怎么撤销？",
        ["我导入了一份CSV账单，里面有重复行。"],
        limit=4,
    )

    assert results
    assert any("撤销 CSV 导入" in row["title"] for row in results)


def test_contextual_follow_up_inherits_scheduled_transfer_subject() -> None:
    service = KnowledgeService(SEED_KNOWLEDGE_DIR)

    results = service.search_contextual(
        "那能撤销吗？",
        ["我安排了下周给李娜定时转账。"],
        limit=4,
    )

    assert results
    assert any("定时转账如何取消" in row["title"] for row in results)


def test_explicit_new_domain_uses_only_current_query() -> None:
    service = KnowledgeService(SEED_KNOWLEDGE_DIR)
    query = "现在信用卡冻结后怎么解锁？"
    history = ["我想从日常借记卡给陈晨转3.21元，先看一下确认单。"]

    assert service.search_contextual(query, history, limit=4) == service.search(query, limit=4)


def test_new_domain_is_not_hidden_by_a_shared_transfer_term() -> None:
    service = KnowledgeService(SEED_KNOWLEDGE_DIR)
    query = "那信用卡冻结后可以转账吗？"
    history = ["我想给陈晨做一笔转账，手续费怎么算？"]

    assert service.search_contextual(query, history, limit=4) == service.search(query, limit=4)


def test_explicit_topic_switch_uses_only_current_query() -> None:
    service = KnowledgeService(SEED_KNOWLEDGE_DIR)
    query = "换个话题，账单导入错了怎么撤销？"
    history = ["我想从日常借记卡给陈晨转3.21元，先看一下确认单。"]

    assert service.search_contextual(query, history, limit=4) == service.search(query, limit=4)


def test_self_contained_query_and_empty_history_match_baseline() -> None:
    service = KnowledgeService(SEED_KNOWLEDGE_DIR)
    query = "信用卡冻结后怎么解锁？"

    assert service.search_contextual(query, [], limit=3) == service.search(query, limit=3)
    assert service.search_contextual(query, None, limit=3) == service.search(query, limit=3)


def test_return_shape_matches_baseline_and_respects_limit() -> None:
    service = KnowledgeService(SEED_KNOWLEDGE_DIR)
    results = service.search_contextual("那手续费呢？", ["我想给联系人陈晨转账。"], limit=2)

    assert len(results) <= 2
    assert results
    assert set(results[0]) == {"doc_id", "title", "excerpt", "version", "score"}


def test_context_uses_most_recent_domain_message_after_acknowledgement() -> None:
    service = KnowledgeService(SEED_KNOWLEDGE_DIR)

    results = service.search_contextual(
        "那多久到账？",
        ["信用卡怎么解锁？", "我想赎回平衡180，结算要多久？", "好的谢谢。"],
        limit=4,
    )

    assert results
    assert any("赎回后什么时候到账" in row["title"] for row in results)


def test_workflow_refund_follow_up_retrieves_current_compensation_policy() -> None:
    service = KnowledgeService(SEED_KNOWLEDGE_DIR)
    results = service.search_contextual(
        "这个安排失败能退款吗？",
        [
            "用日常账户给伴侣安排鲜花和蛋糕各1份，预算500元，确认后立即开始。",
            "蛋糕不要了，花保留，其他不变。",
        ],
        limit=4,
    )

    refund_context = "\n".join(str(row["excerpt"]) for row in results)
    assert "待确认预览尚未下单或扣款" in refund_context
    assert "自动为已付款且支持取消的模拟订单发起退款" in refund_context
    assert "退款待处理" in refund_context
    assert "本版本没有通用商户编排或自动回滚能力" not in refund_context


class _ContextCaptureModel:
    configured = True

    def __init__(self) -> None:
        self.messages: list[list[dict[str, Any]]] = []

    async def complete(self, messages, tools, emit=None):
        self.messages.append(messages)
        return {"role": "assistant", "content": "待确认预览目前仍有效。"}

    async def close(self) -> None:
        return None


def test_refund_policy_is_inserted_in_chat_model_context_and_keeps_pending_preview(
    tmp_path: Path,
) -> None:
    model = _ContextCaptureModel()
    database_url = f"sqlite:///{(tmp_path / 'refund-context.sqlite3').as_posix()}"
    app = create_app(
        database_url,
        model=model,
        enable_workflow_actions=True,
        task_poll_seconds=3600,
    )

    with TestClient(app) as client:
        conversation = client.post(
            "/api/conversations",
            json={"title": "退款规则上下文回归"},
        ).json()
        conversation_id = conversation["id"]
        pending = app.state.workflows.create_action_plan(
            "arrange",
            conversation_id,
            account_id="demo-checking-001",
            recipient_label="伴侣",
            budget_minor=50_000,
            title="生日安排",
            items=[{"product_id": "flowers-basic", "quantity": 1}],
        )
        app.state.store.save(
            conversation_id,
            [
                {
                    "role": "user",
                    "content": "用日常账户给伴侣安排鲜花和蛋糕各1份，预算500元，确认后立即开始。",
                },
                {"role": "assistant", "content": "已准备鲜花和蛋糕的待确认预览。"},
                {"role": "user", "content": "蛋糕不要了，花保留，其他不变。"},
                {"role": "assistant", "content": "预览已更新，只保留鲜花。"},
            ],
        )

        response = client.post(
            "/api/chat",
            json={
                "message": "这个安排失败能退款吗？",
                "conversation_id": conversation_id,
                "client_request_id": f"refund-context-{uuid4().hex}",
            },
        )

        assert response.status_code == 200
        system_context = "\n".join(
            row["content"] for row in model.messages[-1] if row["role"] == "system"
        )
        assert "待确认预览尚未下单或扣款" in system_context
        assert "自动为已付款且支持取消的模拟订单发起退款" in system_context
        assert "本版本没有通用商户编排或自动回滚能力" not in system_context
        still_pending = app.state.workflows.pending_for_conversation(conversation_id)
        assert still_pending is not None
        assert still_pending["id"] == pending["id"]
        assert still_pending["status"] == "pending"
