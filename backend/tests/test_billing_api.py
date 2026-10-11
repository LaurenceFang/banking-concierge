from __future__ import annotations

import csv
import io

import pytest
from app.main import create_app
from fastapi.testclient import TestClient


class OfflineModel:
    """A network-free model stub; billing API checks do not call the model."""

    configured = True

    async def close(self) -> None:
        pass

    async def complete(self, messages, tools, emit=None):
        return {"role": "assistant", "content": "离线测试模型。"}


@pytest.fixture
def client(tmp_path):
    app = create_app(
        f"sqlite:///{(tmp_path / 'billing-api.sqlite3').as_posix()}",
        model=OfflineModel(),
    )
    with TestClient(app) as test_client:
        yield test_client


def _csv(rows: list[list[str]], headers: list[str] | None = None) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(
        headers
        or [
            "transaction_id",
            "date",
            "merchant",
            "amount",
            "currency",
            "account_id",
            "transaction_type",
            "category",
            "notes",
        ]
    )
    writer.writerows(rows)
    return buffer.getvalue()


def test_invalid_billing_date_returns_readable_422(client: TestClient):
    response = client.get("/api/billing/report", params={"start": "03/04/2026"})

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "INVALID_DATE",
        "message": "start 日期无效：日期格式不明确，请使用 YYYY-MM-DD 或 YYYY/MM/DD。",
    }


def test_duplicate_csv_import_does_not_change_bank_accounts_or_transactions(client: TestClient):
    before = client.get("/api/overview").json()
    content = _csv(
        [["api-import-1", "2099-07-01", "陌生商户", "-18.50", "CNY", "card-api", "expense", "", "memo"]]
    )

    first = client.post("/api/billing/import", json={"filename": "账单.csv", "content": content})
    second = client.post("/api/billing/import", json={"filename": "same.csv", "content": content})
    after = client.get("/api/overview").json()

    assert first.status_code == second.status_code == 200
    assert first.json()["imported"] == 1
    assert first.json()["duplicates"] == 0
    assert second.json()["imported"] == 0
    assert second.json()["duplicates"] == 1
    assert after["accounts"] == before["accounts"]
    assert after["transactions"] == before["transactions"]


def test_billing_report_category_total_equals_net_expense(client: TestClient):
    content = _csv(
        [
            ["api-expense", "2099-08-01", "木棉咖啡", "-100.00", "CNY", "card-api", "expense", "餐饮", ""],
            ["api-refund", "2099-08-02", "木棉咖啡", "25.00", "CNY", "card-api", "refund", "餐饮", ""],
            ["api-transfer", "2099-08-03", "内部转账", "-88.00", "CNY", "card-api", "transfer", "转账", ""],
        ]
    )
    imported = client.post("/api/billing/import", json={"filename": "aug.csv", "content": content})
    response = client.get("/api/billing/report", params={"start": "2099-08-01", "end": "2099-08-31"})

    assert imported.status_code == 200
    assert imported.json()["imported"] == 3
    assert response.status_code == 200
    report = response.json()
    assert report["expense_minor"] == 10_000
    assert report["refund_minor"] == 2_500
    assert report["net_expense_minor"] == 7_500
    assert sum(item["amount_minor"] for item in report["categories"]) == report["net_expense_minor"]


def test_api_exposes_rule_provenance_without_claiming_model_classification(client: TestClient):
    content = _csv(
        [["api-alias", "2099-09-01", "Xingyun Video", "-23.90", "CNY", "card-api", "expense", "", ""]]
    )
    imported = client.post("/api/billing/import", json={"filename": "alias.csv", "content": content})
    response = client.get("/api/billing/entries", params={"start": "2099-09-01", "end": "2099-09-01"})

    assert imported.status_code == 200
    assert response.status_code == 200
    entries = response.json()["entries"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["merchant"] == "星云视频"
    assert entry["raw_merchant"] == "Xingyun Video"
    assert entry["category"] == "数字娱乐"
    assert entry["confidence"] == 0.96
    assert "商户别名表" in entry["reason"]
    assert "数字娱乐关键词规则" in entry["reason"]
    assert "模型" not in entry["reason"]
    assert "model" not in entry["reason"].casefold()
    assert entry["account_id"] == "card-api"
    assert set(entry) == {
        "id",
        "date",
        "merchant",
        "raw_merchant",
        "amount_minor",
        "currency",
        "account_id",
        "category",
        "transaction_kind",
        "source",
        "confidence",
        "reason",
    }
