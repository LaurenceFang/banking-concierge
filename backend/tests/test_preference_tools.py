from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from app.bank import BankService
from app.billing import BillingService
from app.preference_tools import (
    PREFERENCE_TOOL_DEFINITIONS,
    PREFERENCE_TOOL_NAMES,
    execute_preference_tool,
)


@pytest.fixture
def services(tmp_path):
    url = f"sqlite:///{(tmp_path / 'preference-tools.sqlite').as_posix()}"
    bank = BankService(url)
    bank.seed()
    billing = BillingService(url)
    yield bank, billing
    billing.close()
    bank.dispose()


def call(billing, name, arguments, message, request_id="chat-request-1"):
    return execute_preference_tool(
        billing,
        name,
        arguments,
        message=message,
        request_id=request_id,
    )


def test_native_tool_definitions_are_complete_and_do_not_accept_client_request_ids():
    assert PREFERENCE_TOOL_NAMES == {
        "list_category_rules",
        "save_category_rule",
        "update_category_rule",
        "delete_category_rule",
    }
    schemas = {item["function"]["name"]: item["function"]["parameters"] for item in PREFERENCE_TOOL_DEFINITIONS}
    assert "client_request_id" not in schemas["save_category_rule"]["properties"]
    assert schemas["save_category_rule"]["additionalProperties"] is False


def test_save_requires_explicit_long_term_intent_and_uses_server_date_and_idempotency(services):
    _bank, billing = services
    args = {"merchant": "星云视频", "category": "数字娱乐"}
    message = "以后星云视频都归为数字娱乐。"
    supplied_id = call(
        billing,
        "save_category_rule",
        {**args, "client_request_id": "model-controlled"},
        message,
    )
    assert supplied_id["error"] == "INVALID_ARGUMENTS"
    first = call(billing, "save_category_rule", args, message)
    retry = call(billing, "save_category_rule", args, message)
    assert first["rule"]["id"] == retry["rule"]["id"]
    assert first["rule"]["effective_from"] == datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    assert first["rule"]["version"] == 1
    assert "长期分类偏好" in first["message"]
    assert call(billing, "list_category_rules", {}, "看看规则") == {
        "rules": [first["rule"]]
    }


@pytest.mark.parametrize(
    ("message", "expected_error"),
    [
        ("把这笔星云视频记为数字娱乐", "SINGLE_ENTRY_ONLY"),
        ("以后不要把星云视频归为数字娱乐", "NEGATIVE_INTENT"),
        ("如果星云视频归为数字娱乐会怎样", "INTENT_UNCLEAR"),
        ("以后星云视频归为数字娱乐可以吗？", "INTENT_UNCLEAR"),
    ],
)
def test_save_rejects_single_negative_hypothetical_and_question(
    services, message, expected_error
):
    _bank, billing = services
    result = call(
        billing,
        "save_category_rule",
        {"merchant": "星云视频", "category": "数字娱乐"},
        message,
    )
    assert result["error"] == expected_error
    assert billing.list_category_rules() == []


def test_save_checks_each_value_against_original_message(services):
    _bank, billing = services
    result = call(
        billing,
        "save_category_rule",
        {"merchant": "星云视频", "category": "办公用品"},
        "以后星云视频都归为数字娱乐。",
    )
    assert result["error"] == "CATEGORY_NOT_IN_MESSAGE"
    result = call(
        billing,
        "save_category_rule",
        {"merchant": "星云视频", "category": "数字娱乐", "note_contains": "开会"},
        "以后星云视频都归为数字娱乐。",
    )
    assert result["error"] == "NOTE_CONDITION_NOT_IN_MESSAGE"
    result = call(
        billing,
        "save_category_rule",
        {"merchant": "星云视频", "category": "数字娱乐", "transaction_kind": "income"},
        "以后星云视频消费都归为数字娱乐。",
    )
    assert result["error"] == "KIND_NOT_IN_MESSAGE"
    result = call(
        billing,
        "save_category_rule",
        {"merchant": "星云视频", "category": "餐饮"},
        "以后星云视频归为数字娱乐，云舟音乐归为餐饮。",
    )
    assert result["error"] == "VALUES_NOT_LINKED"
    assert billing.list_category_rules() == []


def test_duplicate_condition_fails_cleanly_and_update_must_match_selected_rule(services):
    _bank, billing = services
    message = "以后星云视频都归为数字娱乐。"
    args = {"merchant": "星云视频", "category": "数字娱乐"}
    first = call(billing, "save_category_rule", args, message, "request-one")
    duplicate = call(billing, "save_category_rule", args, message, "request-two")
    assert first["rule"]["id"] != duplicate.get("rule", {}).get("id")
    assert duplicate["error"] == "CATEGORY_RULE_EXISTS"

    wrong_target = call(
        billing,
        "update_category_rule",
        {"rule_id": first["rule"]["id"], "version": 1, "category": "出行"},
        "以后云舟音乐都归为出行。",
        "request-update",
    )
    assert wrong_target["error"] == "MERCHANT_NOT_IN_MESSAGE"
    assert billing.list_category_rules()[0]["category"] == "数字娱乐"

    changed = call(
        billing,
        "update_category_rule",
        {"rule_id": first["rule"]["id"], "version": 1, "category": "影音娱乐"},
        "以后星云视频都归为影音娱乐。",
        "request-update-ok",
    )
    assert changed["rule"]["category"] == "影音娱乐"
    assert changed["rule"]["version"] == 2


def test_delete_requires_the_actual_rule_target_and_preserves_deleted_result(services):
    _bank, billing = services
    saved = call(
        billing,
        "save_category_rule",
        {"merchant": "星云视频", "category": "数字娱乐"},
        "以后星云视频都归为数字娱乐。",
        "delete-test-save",
    )
    wrong = call(
        billing,
        "delete_category_rule",
        {"rule_id": saved["rule"]["id"], "version": 1},
        "删除云舟音乐的分类规则。",
    )
    assert wrong["error"] == "MERCHANT_NOT_IN_MESSAGE"
    deleted = call(
        billing,
        "delete_category_rule",
        {"rule_id": saved["rule"]["id"], "version": 1},
        "删除星云视频的分类规则。",
    )
    assert deleted["rule"]["id"] == saved["rule"]["id"]
    assert "已删除星云视频" in deleted["message"]
    assert billing.list_category_rules() == []


def test_delete_rejects_negative_language_and_ambiguous_or_bulk_target(services):
    _bank, billing = services
    expense = call(
        billing,
        "save_category_rule",
        {"merchant": "木棉咖啡", "category": "餐饮", "transaction_kind": "expense"},
        "以后木棉咖啡消费都归为餐饮。",
        "kapok-expense",
    )
    note_rule = call(
        billing,
        "save_category_rule",
        {"merchant": "木棉咖啡", "category": "商务餐饮", "note_contains": "客户"},
        "以后木棉咖啡备注含客户都归为商务餐饮。",
        "kapok-note",
    )
    assert expense.get("rule") and note_rule.get("rule")

    negated = call(
        billing,
        "delete_category_rule",
        {"rule_id": expense["rule"]["id"], "version": 1},
        "不要删除木棉咖啡的分类规则。",
    )
    assert negated["error"] == "NEGATIVE_INTENT"
    assert len(billing.list_category_rules()) == 2

    ambiguous = call(
        billing,
        "delete_category_rule",
        {"rule_id": expense["rule"]["id"], "version": 1},
        "删除木棉咖啡的分类规则。",
    )
    assert ambiguous["error"] == "CATEGORY_RULE_AMBIGUOUS"
    assert len(billing.list_category_rules()) == 2

    bulk = call(
        billing,
        "delete_category_rule",
        {"rule_id": expense["rule"]["id"], "version": 1},
        "删除木棉咖啡的所有分类规则。",
    )
    assert bulk["error"] == "CATEGORY_RULE_BULK_DELETE_UNSUPPORTED"
    assert len(billing.list_category_rules()) == 2

    deleted = call(
        billing,
        "delete_category_rule",
        {"rule_id": expense["rule"]["id"], "version": 1},
        "删除木棉咖啡消费的分类规则。",
    )
    assert deleted.get("rule", {}).get("id") == expense["rule"]["id"]
    assert [item["id"] for item in billing.list_category_rules()] == [note_rule["rule"]["id"]]


def test_update_requires_disambiguating_existing_condition_for_same_merchant(services):
    _bank, billing = services
    expense = call(
        billing,
        "save_category_rule",
        {"merchant": "木棉咖啡", "category": "餐饮", "transaction_kind": "expense"},
        "以后木棉咖啡消费都归为餐饮。",
        "kapok-update-expense",
    )
    call(
        billing,
        "save_category_rule",
        {"merchant": "木棉咖啡", "category": "商务餐饮", "note_contains": "客户"},
        "以后木棉咖啡备注含客户都归为商务餐饮。",
        "kapok-update-note",
    )
    ambiguous = call(
        billing,
        "update_category_rule",
        {"rule_id": expense["rule"]["id"], "version": 1, "category": "工作餐"},
        "以后木棉咖啡都归为工作餐。",
    )
    assert ambiguous["error"] == "CATEGORY_RULE_AMBIGUOUS"
    assert next(rule for rule in billing.list_category_rules() if rule["id"] == expense["rule"]["id"])["category"] == "餐饮"

    updated = call(
        billing,
        "update_category_rule",
        {"rule_id": expense["rule"]["id"], "version": 1, "category": "工作餐"},
        "以后木棉咖啡消费都归为工作餐。",
    )
    assert updated["rule"]["category"] == "工作餐"


def test_update_errors_use_user_facing_field_names(services):
    _bank, billing = services
    saved = call(
        billing,
        "save_category_rule",
        {"merchant": "星云视频", "category": "数字娱乐"},
        "以后星云视频都归为数字娱乐。",
        "friendly-field-save",
    )
    result = call(
        billing,
        "update_category_rule",
        {
            "rule_id": saved["rule"]["id"],
            "version": 1,
            "note_contains": "会议",
        },
        "把星云视频的分类规则改成备注含客户。",
    )
    assert result["error"] == "FIELD_NOT_IN_MESSAGE"
    assert "备注匹配文字" in result["message"]
    assert "note_contains" not in result["message"]
