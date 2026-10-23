from app.bank import BankService
from app.billing import BillingService
from app.billing.service import _classify_category
from app.preference_routes import preference_router
from fastapi import FastAPI
from fastapi.testclient import TestClient


def make_services(tmp_path):
    url = f"sqlite:///{(tmp_path / 'category-rules.sqlite').as_posix()}"
    bank = BankService(url)
    bank.seed()
    billing = BillingService(url)
    billing.seed_demo()
    return url, bank, billing


def close_services(bank, billing):
    billing.close()
    bank.dispose()


def import_one(billing, transaction_id, day, merchant, amount="-12.00"):
    csv = (
        "transaction_id,date,merchant,amount,type\n"
        f"{transaction_id},{day},{merchant},{amount},expense\n"
    )
    return billing.import_csv(csv, client_request_id=f"import-{transaction_id}")


def test_category_rules_are_idempotent_versioned_and_persisted(tmp_path):
    url, bank, billing = make_services(tmp_path)
    created = billing.create_category_rule(
        client_request_id="category-rule-create-1",
        merchant="  Nori   Studio ",
        category="商务餐饮",
        transaction_kind="expense",
        note_contains="客户",
        effective_from="2099-02-01",
    )
    retry = billing.create_category_rule(
        client_request_id="category-rule-create-1",
        merchant="Nori Studio",
        category="商务餐饮",
        transaction_kind="expense",
        note_contains="客户",
        effective_from="2099-02-01",
    )
    assert retry["id"] == created["id"]
    assert retry["version"] == 1
    assert billing.list_category_rules() == [created]

    changed = billing.update_category_rule(created["id"], 1, category="客户招待")
    assert changed["version"] == 2
    assert changed["category"] == "客户招待"
    try:
        billing.update_category_rule(created["id"], 1, category="旧分类")
    except ValueError as error:
        assert error.code == "VERSION_CONFLICT"
    else:
        raise AssertionError("stale category-rule update should conflict")

    close_services(bank, billing)
    bank = BankService(url)
    billing = BillingService(url)
    assert billing.list_category_rules() == [changed]
    assert billing.delete_category_rule(changed["id"], 2) == {
        "id": changed["id"],
        "deleted": True,
        "version": 2,
    }
    assert billing.list_category_rules() == []
    close_services(bank, billing)


def test_rules_match_exact_normalized_merchant_after_cutoff_and_preserve_annotations(tmp_path):
    _url, bank, billing = make_services(tmp_path)
    # A row entered before the rule stays at its baseline even though its transaction date
    # is later than the requested effective date.
    import_one(billing, "preexisting", "2099-03-02", "Nori Studio")
    preexisting_id = next(
        row["id"] for row in billing.entries()
        if row["source"] == "import" and row["merchant"] == "Nori Studio" and row["date"] == "2099-03-02"
    )
    before_bank = {row["id"]: row["balance_minor"] for row in bank.overview()["accounts"]}
    rule = billing.create_category_rule(
        client_request_id="rule-for-nori",
        merchant=" Nori   Studio ",
        category="日常社交",
        effective_from="2099-03-02",
        transaction_kind="expense",
    )
    preexisting = next(row for row in billing.entries() if row["id"] == preexisting_id)
    assert preexisting["category"] == "其他消费"

    import_one(billing, "matching", "2099-03-02", "nori studio", amount="-13.00")
    import_one(billing, "before-effective", "2099-03-01", "Nori Studio")
    import_one(billing, "near-match", "2099-03-02", "Nori Studio Extra")
    matching = next(
        row for row in billing.entries()
        if row["source"] == "import" and row["merchant"] == "nori studio" and row["date"] == "2099-03-02"
    )
    assert matching["category"] == "日常社交"
    assert matching["category_source"] == "personal_rule"
    assert matching["category_rule_id"] == rule["id"]
    prior = next(
        row for row in billing.entries()
        if row["source"] == "import" and row["merchant"] == "Nori Studio" and row["date"] == "2099-03-01"
    )
    assert prior["category"] == "其他消费"
    near = next(row for row in billing.entries() if row["merchant"] == "Nori Studio Extra")
    assert near["category"] == "其他消费"

    explicit = billing.update_entry(
        matching["id"], matching["version"], category="单笔分类", note="这条人工核对"
    )
    assert explicit["category"] == "单笔分类"
    assert explicit["category_source"] == "single_entry"
    billing.delete_category_rule(rule["id"], rule["version"])
    removed = next(row for row in billing.entries() if row["id"] == matching["id"])
    assert removed["category"] == "单笔分类"
    assert removed["note"] == "这条人工核对"
    assert next(row for row in billing.entries() if row["id"] == prior["id"])["category"] == "其他消费"
    assert {row["id"]: row["balance_minor"] for row in bank.overview()["accounts"]} == before_bank
    close_services(bank, billing)


def test_note_kind_specificity_and_single_entry_category_precedence(tmp_path):
    _url, bank, billing = make_services(tmp_path)
    general = billing.create_category_rule(
        client_request_id="rule-nori-general",
        merchant="Nori Studio",
        category="日常社交",
        effective_from="2099-04-01",
        transaction_kind="expense",
    )
    specific = billing.create_category_rule(
        client_request_id="rule-nori-note",
        merchant="Nori Studio",
        category="商务招待",
        effective_from="2099-04-01",
        transaction_kind="expense",
        note_contains="客户",
    )
    import_one(billing, "nori-note", "2099-04-01", "Nori Studio")
    row = next(
        item for item in billing.entries()
        if item["source"] == "import" and item["date"] == "2099-04-01" and item["merchant"] == "Nori Studio"
    )
    annotated = billing.update_entry(row["id"], row["version"], note="客户晚餐")
    assert annotated["category"] == "商务招待"
    assert annotated["category_rule_id"] == specific["id"]
    explicit = billing.update_entry(row["id"], annotated["version"], category="自己指定")
    assert explicit["category"] == "自己指定"
    assert explicit["category_source"] == "single_entry"

    billing.import_csv(
        "transaction_id,date,merchant,amount,type,category\n"
        "nori-csv-category,2099-04-03,Nori Studio,-4.00,expense,账单原类别\n",
        client_request_id="import-nori-explicit-category",
    )
    source_category = next(
        item for item in billing.entries()
        if item["source"] == "import" and item["date"] == "2099-04-03"
        and item["merchant"] == "Nori Studio"
    )
    assert source_category["category"] == "账单原类别"
    assert source_category["category_source"] == "source_input"

    billing.import_csv(
        "transaction_id,date,merchant,amount,type\nnori-income,2099-04-02,Nori Studio,90.00,income\n",
        client_request_id="import-nori-income-real",
    )
    income_row = next(
        item for item in billing.entries()
        if item["source"] == "import" and item["transaction_kind"] == "income" and item["merchant"] == "Nori Studio"
    )
    assert income_row["category"] == "其他收入"
    assert general["id"] != specific["id"]
    close_services(bank, billing)


def test_personal_rules_on_future_time_split_improve_only_the_tiny_known_merchant_holdout(tmp_path):
    """Rules are user preferences, evaluated on later rows from unseen dates."""
    _url, bank, billing = make_services(tmp_path)
    cutoff = "2099-05-01"
    # Pre-cutoff examples are explicitly corrected by the user; their labels are the
    # only inputs used to define preferences below.
    import_one(billing, "train-nori", "2099-04-20", "Nori Studio")
    import_one(billing, "train-veloc", "2099-04-21", "VeloCity")
    training = {}
    for merchant, transaction_id, category in (
        ("Nori Studio", "train-nori", "商务餐饮"),
        ("VeloCity", "train-veloc", "通勤"),
    ):
        baseline_row = next(
            row for row in billing.entries()
            if row["source"] == "import" and row["merchant"] == merchant
            and row["date"].startswith("2099-04")
        )
        labeled = billing.update_entry(
            baseline_row["id"], baseline_row["version"], category=category
        )
        training[merchant] = labeled["category"]
    for index, (merchant, category) in enumerate(training.items()):
        billing.create_category_rule(
            client_request_id=f"preference-from-training-{index}",
            merchant=merchant,
            category=category,
            effective_from=cutoff,
            transaction_kind="expense",
        )
    heldout = [
        ("holdout-nori-1", "2099-05-02", "Nori Studio", "商务餐饮"),
        ("holdout-nori-2", "2099-05-12", "Nori Studio", "商务餐饮"),
        ("holdout-veloc", "2099-05-08", "VeloCity", "通勤"),
    ]
    for transaction_id, day, merchant, _expected in heldout:
        import_one(billing, transaction_id, day, merchant)

    baseline_correct = 0
    rule_correct = 0
    for transaction_id, _day, merchant, expected in heldout:
        baseline, _reason, _confidence = _classify_category(merchant, "expense")
        # Resolve each transaction by merchant plus date, keeping the target labels in
        # the fixture separate from the rules created above.
        target = next(item for item in heldout if item[0] == transaction_id)
        matching = next(
            entry for entry in billing.entries()
            if entry["source"] == "import" and entry["date"] == target[1]
            and entry["merchant"] == merchant
        )
        baseline_correct += int(baseline == expected)
        rule_correct += int(matching["category"] == expected)
    assert baseline_correct == 0
    assert rule_correct == 3
    close_services(bank, billing)


def test_category_rule_routes_expose_versioned_crud(tmp_path):
    _url, bank, billing = make_services(tmp_path)
    app = FastAPI()
    app.state.billing = billing
    app.include_router(preference_router(app))
    with TestClient(app) as client:
        created = client.post(
            "/api/billing/category-rules",
            json={
                "client_request_id": "route-rule-1",
                "merchant": "Nori Studio",
                "category": "商务餐饮",
                "effective_from": "2099-06-01",
            },
        )
        assert created.status_code == 200
        rule = created.json()
        assert client.get("/api/billing/category-rules").json() == {"rules": [rule]}
        update = client.patch(
            f"/api/billing/category-rules/{rule['id']}",
            json={"version": 1, "category": "客户招待"},
        )
        assert update.status_code == 200 and update.json()["version"] == 2
        stale = client.patch(
            f"/api/billing/category-rules/{rule['id']}",
            json={"version": 1, "category": "旧内容"},
        )
        assert stale.status_code == 409
        removed = client.delete(f"/api/billing/category-rules/{rule['id']}?version=2")
        assert removed.status_code == 200 and removed.json()["deleted"] is True
    close_services(bank, billing)
