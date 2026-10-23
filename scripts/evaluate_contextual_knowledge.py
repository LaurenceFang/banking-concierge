"""Compare first-turn BM25 with bounded conversational retrieval on fixed cases."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.knowledge import KnowledgeService

CASES = (
    {
        "name": "transfer-fee-follow-up",
        "history": ["我准备从日常借记卡给陈晨转3.21元，先看确认单。"],
        "query": "那手续费呢？",
        "target": "模拟转账手续费",
    },
    {
        "name": "wealth-settlement-follow-up",
        "history": ["我想赎回平衡180计划，资金结算时间怎么算？"],
        "query": "那大概多久到账？",
        "target": "赎回后什么时候到账",
    },
    {
        "name": "import-batch-undo-follow-up",
        "history": ["我导入了一份CSV账单，里面有重复行。"],
        "query": "这批怎么撤销？",
        "target": "撤销 CSV 导入",
    },
    {
        "name": "personal-reminder-edit-follow-up",
        "history": ["我记了一条个人订阅提醒，想修改下次续费日期。"],
        "query": "那怎么改日期？",
        "target": "个人订阅记录会自动扣款",
    },
    {
        "name": "candidate-classification-follow-up",
        "history": ["账单把几笔白鹭阅读扣款识别成周期订阅候选。"],
        "query": "那误判了怎么办？",
        "target": "识别成订阅候选后发现误判",
    },
    {
        "name": "card-unfreeze-follow-up",
        "history": ["我的日常借记卡刚才冻结了。"],
        "query": "普通解锁可以吗？",
        "target": "冻结和挂失有什么区别",
    },
    {
        "name": "schedule-cancel-follow-up",
        "history": ["我安排了下周给李娜定时转账。"],
        "query": "那能撤销吗？",
        "target": "定时转账如何取消",
    },
    {
        "name": "aa-status-follow-up",
        "history": ["刚才创建了朋友聚餐的AA收款请求。"],
        "query": "那会马上扣钱吗？",
        "target": "AA 请求什么时候会扣款",
    },
)

NEW_TOPIC_CASES = (
    {
        "history": ["我想给陈晨转3.21元，先看确认单。"],
        "query": "换个话题，信用卡冻结后怎么解锁？",
    },
    {
        "history": ["我的借记卡冻结了。"],
        "query": "另外账单导入后怎么撤销？",
    },
    {
        "history": ["我想赎回平衡180。"],
        "query": "现在个人订阅记录怎么修改？",
    },
    {
        "history": ["我想给陈晨转3.21元，先看确认单。"],
        "query": "那信用卡冻结后可以转账吗？",
    },
)


def _hit(rows: list[dict[str, str | float]], target: str) -> bool:
    return any(target in str(row["title"]) for row in rows)


def main() -> None:
    service = KnowledgeService(REPO_ROOT / "knowledge")
    baseline_1 = baseline_3 = contextual_1 = contextual_3 = 0
    print("case | BM25@1 | contextual@1 | BM25@3 | contextual@3")
    print("--- | --- | --- | --- | ---")
    for case in CASES:
        baseline = service.search(case["query"], limit=3)
        contextual = service.search_contextual(case["query"], history=case["history"], limit=3)
        target = case["target"]
        b1 = _hit(baseline[:1], target)
        c1 = _hit(contextual[:1], target)
        b3 = _hit(baseline, target)
        c3 = _hit(contextual, target)
        baseline_1 += b1
        contextual_1 += c1
        baseline_3 += b3
        contextual_3 += c3
        print(
            f"{case['name']} | {'hit' if b1 else 'miss'} | "
            f"{'hit' if c1 else 'miss'} | {'hit' if b3 else 'miss'} | "
            f"{'hit' if c3 else 'miss'}"
        )

    unchanged = sum(
        service.search_contextual(case["query"], case["history"], limit=4)
        == service.search(case["query"], limit=4)
        for case in NEW_TOPIC_CASES
    )
    count = len(CASES)
    print(
        f"TOTAL ({count} cases) | {baseline_1}/{count} | {contextual_1}/{count} | "
        f"{baseline_3}/{count} | {contextual_3}/{count}"
    )
    print(f"Explicit topic-switch/boundary cases unchanged from BM25: {unchanged}/{len(NEW_TOPIC_CASES)}")
    print(
        "Metric: expected section-title substring retrieved in top 1 or top 3; no model answer was generated."
    )


if __name__ == "__main__":
    main()
