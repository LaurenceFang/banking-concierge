from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from app.knowledge import KnowledgeService

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_KNOWLEDGE_DIR = REPO_ROOT / "knowledge"


@pytest.mark.parametrize(
    ("query", "expected_heading"),
    [
        ("多位候选怎么确认收款人", "转账 / 收款人怎么确认？"),
        ("月报年报缺少数据的月份怎么处理", "账单 / 月报或年报能看什么？"),
        ("赎回后多久到账", "理财 / 赎回后什么时候到账？"),
        ("挂失丢卡和冻结有什么区别", "卡片 / 冻结和挂失有什么区别？"),
        ("停止自动扣费是否取消商户服务", "订阅 / 停止代扣会同时取消商户服务吗？"),
        ("某个步骤失败会自动退款吗", "跨场景安排 / 一家商户失败后，其他步骤会自动撤销吗？"),
    ],
)
def test_each_of_six_scenarios_retrieves_a_useful_faq(
    query: str, expected_heading: str
) -> None:
    service = KnowledgeService(SEED_KNOWLEDGE_DIR)

    results = service.search(query, limit=1, category="business_faq")

    assert results
    assert expected_heading in results[0]["title"]
    assert expected_heading in results[0]["excerpt"]
    assert "\n" in results[0]["excerpt"]
    assert results[0]["excerpt"].split("\n", 1)[1].strip()
    assert results[0]["version"] == "0.4"
    assert results[0]["score"] > 0


def test_manifest_current_draft_is_searchable_but_expired_and_retired_are_not(
    tmp_path: Path,
) -> None:
    today = date.today()
    documents = [
        {
            "doc_id": "current-draft",
            "path": "current.md",
            "source": "project_authored_simulation",
            "version": "1.0",
            "category": "help",
            "scope": "demo_public",
            "status": "draft",
            "valid_from": (today - timedelta(days=1)).isoformat(),
        },
        {
            "doc_id": "expired",
            "path": "expired.md",
            "version": "1.0",
            "category": "help",
            "status": "published",
            "valid_until": (today - timedelta(days=1)).isoformat(),
        },
        {
            "doc_id": "retired",
            "path": "retired.md",
            "version": "1.0",
            "category": "help",
            "status": "retired",
        },
        {
            "doc_id": "future",
            "path": "future.md",
            "version": "1.0",
            "category": "help",
            "status": "published",
            "valid_from": (today + timedelta(days=1)).isoformat(),
        },
    ]
    for entry in documents:
        (tmp_path / entry["path"]).write_text(
            f"# {entry['doc_id']}\n\n今天可以查找的专属词 {entry['doc_id']}。",
            encoding="utf-8",
        )
    (tmp_path / "manifest.json").write_text(
        json.dumps({"documents": documents}, ensure_ascii=False), encoding="utf-8"
    )

    service = KnowledgeService(tmp_path)

    assert service.document_count == 1
    assert [item["doc_id"] for item in service.search("current-draft")] == [
        "current-draft"
    ]
    assert service.search("expired") == []
    assert service.search("retired") == []
    assert service.search("future") == []


def test_reload_picks_up_new_document_and_replaced_version(tmp_path: Path) -> None:
    (tmp_path / "seed.md").write_text(
        "# 初始帮助\n\n旧版专用词 oldtoken77。", encoding="utf-8"
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "doc_id": "help",
                        "path": "seed.md",
                        "source": "project_authored_simulation",
                        "version": "1.0",
                        "category": "help",
                        "status": "draft",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    service = KnowledgeService(tmp_path)
    assert service.document_count == 1
    assert service.search("oldtoken77")

    (tmp_path / "seed.md").write_text(
        "# 更新后的帮助\n\n新版专用词 newtoken88。", encoding="utf-8"
    )
    (tmp_path / "extra.md").write_text(
        "# 新增说明\n\n新增专用词 addedterm55。", encoding="utf-8"
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "doc_id": "help",
                        "path": "seed.md",
                        "source": "project_authored_simulation",
                        "version": "2.0",
                        "category": "help",
                        "status": "published",
                    },
                    {
                        "doc_id": "extra",
                        "path": "extra.md",
                        "version": "1.0",
                        "category": "faq",
                        "status": "active",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    assert service.reload() == 2
    assert service.search("oldtoken77") == []
    replaced = service.search("newtoken88", category="help")
    assert replaced[0]["doc_id"] == "help"
    assert replaced[0]["version"] == "2.0"
    added = service.search("addedterm55", category="faq")
    assert added[0]["doc_id"] == "extra"
    assert service.search("addedterm55", category="help") == []


def test_unrelated_question_returns_no_results() -> None:
    service = KnowledgeService(SEED_KNOWLEDGE_DIR)

    assert service.search("银河系量子咖啡合同") == []
    assert service.search("   ") == []
    assert service.search("转账", limit=0) == []


@pytest.mark.parametrize(
    ("query", "expected_excerpt"),
    [
        ("银行卡冻结或挂失", "是否可以解除"),
        ("现在账户余额是多少", "实时余额"),
        ("退款会自动回退吗", "退款只在对应工具确认后更新"),
        ("产品收费有哪些", "费用"),
        ("取消会员后代扣会停止吗", "取消会员或订阅服务"),
    ],
)
def test_common_synonyms_return_answer_snippets(
    query: str, expected_excerpt: str
) -> None:
    service = KnowledgeService(SEED_KNOWLEDGE_DIR)

    results = service.search(query, limit=3, category="business_faq")

    assert results
    assert any(expected_excerpt in result["excerpt"] for result in results)


def test_repeated_debit_is_not_presented_as_a_confirmed_subscription() -> None:
    service = KnowledgeService(SEED_KNOWLEDGE_DIR)

    results = service.search("重复扣款代表已经订阅了吗", limit=1, category="business_faq")

    assert results
    assert "周期性消费也可能是普通消费" in results[0]["excerpt"]
