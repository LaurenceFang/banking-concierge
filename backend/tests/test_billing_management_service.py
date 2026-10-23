import pytest
from app.bank import BankService
from app.billing import BillingService


def services(tmp_path):
    url = f"sqlite:///{(tmp_path / 'management-billing.sqlite').as_posix()}"
    bank = BankService(url)
    bank.seed()
    bill = BillingService(url)
    bill.seed_demo()
    return url, bank, bill


def close(bank, bill):
    bill.close()
    bank.dispose()


def test_annotations_correct_import_and_bank_projection_without_changing_bank_fact(tmp_path):
    url, bank, bill = services(tmp_path)
    imported = bill.import_csv("date,merchant,amount,type\n2099-01-01,Food Shop,-12.00,expense\n")
    assert imported["imported"] == 1 and imported["batch_id"]
    row = next(item for item in bill.entries() if item["source"] == "import")
    first = bill.update_entry(
        row["id"], row["version"], category="食品杂货", tags=["家庭", "每周"], note="周日采买"
    )
    assert first["category"] == "食品杂货" and first["tags"] == ["家庭", "每周"]
    assert first["note"] == "周日采买" and first["raw_merchant"] == "Food Shop"
    with pytest.raises(ValueError, match="已在其他位置更新"):
        bill.update_entry(row["id"], row["version"], note="旧写入")
    bankrow = next(item for item in bill.entries() if item["source"] == "bank")
    original = bankrow.copy()
    projected = bill.update_entry(bankrow["id"], bankrow["version"], category="其他消费", note="人工复核")
    assert projected["amount_minor"] == original["amount_minor"]
    assert (
        projected["date"] == original["date"]
        and projected["transaction_kind"] == original["transaction_kind"]
    )
    transaction = next(item for item in bank.list_transactions() if item["id"] == bankrow["id"])
    assert (
        transaction["amount_minor"] == original["amount_minor"]
        and transaction["description"] == original["raw_merchant"]
    )
    assert bill.entries(query="周日采买")[0]["id"] == row["id"]
    assert row["id"] in {entry["id"] for entry in bill.entries(category="食品杂货")}
    assert bill.entries(source="bank", query="人工复核")[0]["id"] == bankrow["id"]
    before = bill.entries()
    bill.seed_demo()
    assert next(item for item in bill.entries() if item["id"] == bankrow["id"])["note"] == "人工复核"
    assert len(before) == len(bill.entries())
    close(bank, bill)
    bank = BankService(url)
    bill = BillingService(url)
    assert next(item for item in bill.entries() if item["id"] == bankrow["id"])["category"] == "其他消费"
    close(bank, bill)


def test_manual_entries_are_idempotent_versioned_and_never_change_bank_balance(tmp_path):
    _url, bank, bill = services(tmp_path)
    before = {item["id"]: item["balance_minor"] for item in bank.overview()["accounts"]}
    row = bill.create_manual(
        client_request_id="manual-form-one",
        entry_date="2099-02-02",
        merchant="园区早餐",
        amount_minor=1234,
        transaction_kind="expense",
        tags=["早餐"],
        note="现金",
    )
    retry = bill.create_manual(
        client_request_id="manual-form-one",
        entry_date="2099-02-02",
        merchant="园区早餐",
        amount_minor=1234,
        transaction_kind="expense",
        tags=["早餐"],
        note="现金",
    )
    assert retry["id"] == row["id"] and row["amount_minor"] == -1234 and row["source"] == "manual"
    assert bill.report("2099-02-02", "2099-02-02")["net_expense_minor"] == 1234
    edited = bill.update_entry(
        row["id"],
        row["version"],
        merchant="园区食堂",
        amount_minor=-1500,
        transaction_kind="expense",
        note="补充说明",
    )
    assert edited["version"] == 2 and edited["amount_minor"] == -1500
    with pytest.raises(ValueError, match="方向"):
        bill.update_entry(row["id"], edited["version"], amount_minor=1500, transaction_kind="expense")
    assert {item["id"]: item["balance_minor"] for item in bank.overview()["accounts"]} == before
    assert bill.entries(source="manual", query="补充说明")[0]["merchant"] == "园区食堂"
    assert bill.remove_entry(row["id"], 2)["removed"] is True
    close(bank, bill)


def test_import_batches_undo_only_rows_added_by_that_batch_and_can_be_reimported(tmp_path):
    _url, bank, bill = services(tmp_path)
    source = "transaction_id,date,merchant,amount,transaction_type\n"
    old = source + "old-row,2099-03-01,咖啡店,-10.00,expense\n"
    preexisting = bill.import_csv(old)
    one = source + "old-row,2099-03-01,咖啡店,-10.00,expense\nnew-row,2099-03-02,书店,-15.50,expense\n"
    first = bill.import_csv(one, "first.csv", client_request_id="csv-batch-first")
    assert first["imported"] == 1 and first["duplicates"] == 1 and first["batch_id"]
    assert (
        bill.import_csv(one, "renamed.csv", client_request_id="csv-batch-first")["already_processed"] is True
    )
    second = bill.import_csv(one, "second.csv", client_request_id="csv-batch-second")
    assert second["imported"] == 0 and second["duplicates"] == 2
    newly_added = next(item for item in bill.entries() if item["merchant"] == "书店")
    assert bill.remove_entry(newly_added["id"], newly_added["version"])["removed"] is True
    assert bill.entries(query="书店") == []
    undone = bill.undo_import_batch(first["batch_id"])
    assert undone["undone_entries"] == 0
    assert bill.undo_import_batch(first["batch_id"])["already_undone"] is True
    assert any(item["merchant"] == "咖啡店" for item in bill.entries())
    again = bill.import_csv(source + "new-row,2099-03-02,书店,-15.50,expense\n", "again.csv")
    assert again["imported"] == 1 and again["batch_id"] != first["batch_id"]
    batches = bill.list_import_batches()
    assert any(item["id"] == first["batch_id"] and item["undone"] for item in batches)
    assert preexisting["imported"] == 1
    close(bank, bill)
