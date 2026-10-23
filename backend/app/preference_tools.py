"""Native model tools for explicit personal billing category preferences."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo

from .billing.service import BillingError

_KIND_VALUES = ("expense", "refund", "income", "transfer", "opening")
_KIND_TEXT = {
    "expense": ("支出", "消费", "扣款"),
    "refund": ("退款", "退货", "返还"),
    "income": ("收入", "入账", "工资", "利息"),
    "transfer": ("转账", "转入", "转出"),
    "opening": ("期初", "期初余额"),
}
_LONG_TERM = re.compile(r"以后|今后|往后|之后都|以后都|今后都|长期|默认|下次起|以后遇到")
_CATEGORY_ACTION = re.compile(r"归为|归到|分类(?:为|到|成|是|改为|改成)|记为|算作|都算|分到")
_DIRECT_RULE_EDIT = re.compile(r"(?:规则|偏好|长期分类).{0,8}(?:改|更|调|删|移除|取消)|(?:改|更|调|删|移除).{0,8}(?:规则|偏好|长期分类)")
_QUESTION_OR_HYPOTHETICAL = re.compile(
    r"[?？]|吗(?:[，,。.!！]|$)|是否|能否|可不可以|能不能|行不行|怎么(?:办|样)|如何|如果|假如|假设|要是|会不会"
)
_NEGATIVE = re.compile(r"不要|别(?:再)?|不再|不需要|不用|不是|不算|没必要|不要把|别把")
_SINGLE_ENTRY = re.compile(r"这笔|这一笔|本笔|这次|本次|这条|当前账单|单笔|刚刚|刚才")
_FIELD_LABELS = {
    "merchant": "商户",
    "category": "分类",
    "note_contains": "备注匹配文字",
    "effective_from": "生效日期",
}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


PREFERENCE_TOOL_DEFINITIONS = [
    _tool(
        "list_category_rules",
        "查看用户明确保存的个人商户分类规则。规则不是模型训练。",
        {},
        [],
    ),
    _tool(
        "save_category_rule",
        "只有当前用户原话明确表达长期分类意图时才调用，例如‘以后星云视频都归为数字娱乐’。不要用于单笔调整、否定、假设或询问。merchant/category/note条件必须逐字来自原话；不要传client_request_id。",
        {
            "merchant": {"type": "string", "minLength": 1, "maxLength": 160},
            "category": {"type": "string", "minLength": 1, "maxLength": 80},
            "transaction_kind": {"type": "string", "enum": list(_KIND_VALUES)},
            "note_contains": {"type": "string", "minLength": 1, "maxLength": 500},
            "effective_from": {"type": "string", "description": "仅用户原话明确给出日期时填写YYYY-MM-DD；否则由服务端设为北京时间今天。"},
        },
        ["merchant", "category"],
    ),
    _tool(
        "update_category_rule",
        "修改已存在的个人分类规则。先列出规则并核对目标商户；只提交用户原话明确要求变化的字段。merchant/category/note条件与日期必须能在当前原话中核验。",
        {
            "rule_id": {"type": "string"},
            "version": {"type": "integer", "minimum": 1},
            "merchant": {"type": "string", "minLength": 1, "maxLength": 160},
            "category": {"type": "string", "minLength": 1, "maxLength": 80},
            "transaction_kind": {"type": ["string", "null"], "enum": [*_KIND_VALUES, None]},
            "note_contains": {"type": ["string", "null"], "maxLength": 500},
            "effective_from": {"type": "string", "description": "仅原话指定生效日期时传入YYYY-MM-DD。"},
        },
        ["rule_id", "version"],
    ),
    _tool(
        "delete_category_rule",
        "删除用户明确指名商户的长期分类规则。先列出规则并核对目标商户；单笔修改、假设或询问不得删除。",
        {"rule_id": {"type": "string"}, "version": {"type": "integer", "minimum": 1}},
        ["rule_id", "version"],
    ),
]
PREFERENCE_TOOL_NAMES = {item["function"]["name"] for item in PREFERENCE_TOOL_DEFINITIONS}


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _failure(code: str, message: str) -> dict[str, str]:
    return {"error": code, "message": message}


def _is_question_or_hypothetical(message: str) -> bool:
    return bool(_QUESTION_OR_HYPOTHETICAL.search(message))


def _has_single_entry_scope(message: str) -> bool:
    return bool(_SINGLE_ENTRY.search(message))


def _has_long_term_category_intent(message: str) -> bool:
    return bool(_LONG_TERM.search(message) and _CATEGORY_ACTION.search(message))


def _contains_exact(message: str, value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and _normalized_text(value) in _normalized_text(message)


def _merchant_category_pair_matches(
    message: str, merchant: str, category: str, *, allow_direct_edit: bool = False
) -> bool:
    text, merchant_text, category_text = (
        _normalized_text(message),
        _normalized_text(merchant),
        _normalized_text(category),
    )
    merchant_at = text.find(merchant_text)
    if merchant_at < 0:
        return False
    category_at = text.find(category_text, merchant_at + len(merchant_text))
    if category_at < 0:
        return False
    relation = text[merchant_at + len(merchant_text) : category_at + len(category_text)]
    if re.search(r"[，,。；;！？?]", relation):
        return False
    context = text[max(0, merchant_at - 24) : category_at + len(category_text)]
    action_matches = bool(_CATEGORY_ACTION.search(relation)) or (
        allow_direct_edit and bool(re.search(r"改为|改成|调整为|设置为|更新为", relation))
    )
    intent_matches = bool(_LONG_TERM.search(context)) or (
        allow_direct_edit and _direct_rule_edit_intent(message)
    )
    return bool(action_matches and intent_matches)


def _validate_persistent_intent(message: str) -> dict[str, str] | None:
    if _is_question_or_hypothetical(message):
        return _failure(
            "INTENT_UNCLEAR",
            "这句话像是在询问或假设，我先不保存。要建立长期偏好，请直接告诉我‘以后某商户归为某分类’。",
        )
    if _has_single_entry_scope(message):
        return _failure(
            "SINGLE_ENTRY_ONLY",
            "这句话指的是单笔账单，我不会把它推广到以后。若要保存长期偏好，请明确说‘以后/今后该商户归为某分类’。",
        )
    if _NEGATIVE.search(message):
        return _failure(
            "NEGATIVE_INTENT",
            "这句话没有表达新增或修改长期分类偏好；我先不改规则。请直接说明商户、分类和‘以后如何归类’。",
        )
    if not _has_long_term_category_intent(message):
        return _failure(
            "LONG_TERM_INTENT_REQUIRED",
            "我需要确认这是长期偏好。请明确说‘以后/今后某商户归为某分类’，单笔分类不会自动变成规则。",
        )
    return None


def _validate_rule_values(message: str, arguments: dict[str, Any], *, require_merchant: bool = True) -> dict[str, str] | None:
    merchant = arguments.get("merchant")
    category = arguments.get("category")
    note_contains = arguments.get("note_contains")
    if require_merchant and not _contains_exact(message, merchant):
        return _failure("MERCHANT_NOT_IN_MESSAGE", "商户名称无法从你刚才的话中核对，请直接说出商户名后再试。")
    if category is not None and not _contains_exact(message, category):
        return _failure("CATEGORY_NOT_IN_MESSAGE", "分类名称无法从你刚才的话中核对，请直接说出要归入的分类。")
    if category is not None and not _merchant_category_pair_matches(message, str(merchant), str(category)):
        return _failure("VALUES_NOT_LINKED", "原话没有明确把这个分类对应到该商户，我先不保存，请直接说明两者关系。")
    if note_contains is not None:
        if not _contains_exact(message, note_contains):
            return _failure("NOTE_CONDITION_NOT_IN_MESSAGE", "备注匹配条件无法从你刚才的话中核对，请直接说出要匹配的文字。")
        if not str(note_contains).strip():
            return _failure("INVALID_ARGUMENTS", "备注匹配条件不能为空；不需要备注条件时请省略它。")
    kind = arguments.get("transaction_kind")
    if kind is not None and not any(
        phrase in message for phrase in _KIND_TEXT.get(str(kind), ())
    ):
        return _failure("KIND_NOT_IN_MESSAGE", "交易类型条件无法从你刚才的话中核对，请明确说出消费、收入、退款等类型。")
    effective_from = arguments.get("effective_from")
    if effective_from is not None and not _contains_exact(message, effective_from):
        return _failure("DATE_NOT_IN_MESSAGE", "生效日期不是你刚才明确说出的日期，请省略日期使用北京时间今天，或直接提供YYYY-MM-DD。")
    return None


def _request_key(request_id: str, name: str, arguments: dict[str, Any]) -> str:
    serialized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if request_id:
        return f"pref-{uuid5(NAMESPACE_URL, request_id + '|' + name + '|' + serialized).hex[:20]}"
    return f"pref-{uuid4().hex[:20]}"


def _get_rule(billing: Any, rule_id: Any) -> dict[str, Any] | None:
    if not isinstance(rule_id, str) or not rule_id.strip():
        return None
    return next((rule for rule in billing.list_category_rules() if rule["id"] == rule_id), None)


def _direct_rule_edit_intent(message: str) -> bool:
    return bool(_DIRECT_RULE_EDIT.search(message) or re.search(r"删除.{0,12}(?:分类|商户).{0,12}规则", message))


def _validate_update_intent(message: str, rule: dict[str, Any], arguments: dict[str, Any]) -> dict[str, str] | None:
    if _is_question_or_hypothetical(message) or _has_single_entry_scope(message) or _NEGATIVE.search(message):
        return _failure("INTENT_UNCLEAR", "这句话没有明确授权修改这条长期规则，我先不改。请直接说要怎样调整该商户的分类规则。")
    if not (_has_long_term_category_intent(message) or _direct_rule_edit_intent(message)):
        return _failure("LONG_TERM_INTENT_REQUIRED", "请明确说要修改长期分类规则，并说明商户和新的分类条件。")
    if not _contains_exact(message, rule["merchant"]):
        return _failure("MERCHANT_NOT_IN_MESSAGE", "规则对应的商户无法从你刚才的话中核对，未作修改。")
    if "merchant" in arguments and not _contains_exact(message, arguments["merchant"]):
        return _failure("MERCHANT_NOT_IN_MESSAGE", "新的商户名称无法从你刚才的话中核对，未作修改。")
    if "category" in arguments and not _merchant_category_pair_matches(
        message,
        arguments.get("merchant") or rule["merchant"],
        str(arguments["category"]),
        allow_direct_edit=True,
    ):
        return _failure("VALUES_NOT_LINKED", "原话没有明确把新分类对应到该商户，未作修改。")
    for field in ("category", "note_contains", "effective_from"):
        value = arguments.get(field)
        if value is not None and not _contains_exact(message, value):
            return _failure("FIELD_NOT_IN_MESSAGE", f"新的{_FIELD_LABELS[field]}无法从你刚才的话中核对，未作修改。")
    kind = arguments.get("transaction_kind")
    if kind is not None and not any(phrase in message for phrase in _KIND_TEXT.get(str(kind), ())):
        return _failure("KIND_NOT_IN_MESSAGE", "新的交易类型条件无法从你刚才的话中核对，未作修改。")
    if "note_contains" in arguments and arguments["note_contains"] is None and not re.search(r"取消备注条件|不限定备注|移除备注条件", message):
        return _failure("FIELD_NOT_IN_MESSAGE", "原话没有要求移除备注条件，未作修改。")
    if "transaction_kind" in arguments and kind is None and not re.search(r"不限制类型|不限交易类型|所有类型", message):
        return _failure("FIELD_NOT_IN_MESSAGE", "原话没有要求移除交易类型条件，未作修改。")
    return None


def _same_merchant_rules(rules: list[dict[str, Any]], target: dict[str, Any]) -> list[dict[str, Any]]:
    target_merchant = _normalized_text(str(target.get("merchant", "")))
    return [rule for rule in rules if _normalized_text(str(rule.get("merchant", ""))) == target_merchant]


def _rule_disambiguated(message: str, target: dict[str, Any], candidates: list[dict[str, Any]]) -> bool:
    for field in ("category", "note_contains", "effective_from"):
        value = target.get(field)
        if value and any(candidate.get(field) != value for candidate in candidates) and _contains_exact(message, str(value)):
            return True
    kind = target.get("transaction_kind")
    if (
        kind
        and any(candidate.get("transaction_kind") != kind for candidate in candidates)
        and any(phrase in message for phrase in _KIND_TEXT.get(str(kind), ()))
    ):
        return True
    if (
        kind is None
        and any(candidate.get("transaction_kind") is not None for candidate in candidates)
        and re.search(r"不限(?:交易)?类型|不限制(?:交易)?类型|所有类型", message)
    ):
        return True
    return bool(
        target.get("note_contains") is None
        and any(candidate.get("note_contains") is not None for candidate in candidates)
        and re.search(r"不限定备注|没有备注条件|无备注条件|不按备注", message)
    )


def _validate_delete_intent(message: str, rule: dict[str, Any]) -> dict[str, str] | None:
    if _is_question_or_hypothetical(message) or _has_single_entry_scope(message):
        return _failure("INTENT_UNCLEAR", "这句话像是在询问或只谈一笔账单，我先不删除长期规则。")
    if _NEGATIVE.search(message):
        return _failure("NEGATIVE_INTENT", "这句话没有明确要求删除分类规则，我先不做修改。")
    if not _direct_rule_edit_intent(message) or not re.search(r"删除|移除|取消", message):
        return _failure("DELETE_INTENT_REQUIRED", "要删除长期规则，请明确说‘删除某商户的分类规则’。")
    if not _contains_exact(message, rule["merchant"]):
        return _failure("MERCHANT_NOT_IN_MESSAGE", "规则对应的商户名称不在你刚才的话中，未作删除。")
    return None


def execute_preference_tool(
    billing: Any,
    name: str,
    arguments: dict[str, Any],
    *,
    message: str,
    request_id: str,
) -> dict[str, Any]:
    """Execute one preference tool, validating persistent writes against the user message."""
    if not isinstance(arguments, dict):
        return _failure("INVALID_ARGUMENTS", "工具参数必须是对象，请核对后重试。")
    try:
        if name == "list_category_rules":
            return {"rules": billing.list_category_rules()}

        if name == "save_category_rule":
            allowed = {"merchant", "category", "transaction_kind", "note_contains", "effective_from"}
            if set(arguments) - allowed or not {"merchant", "category"}.issubset(arguments):
                return _failure("INVALID_ARGUMENTS", "创建规则参数不完整或包含未支持字段，请核对后重试。")
            error = _validate_persistent_intent(message)
            if error:
                return error
            error = _validate_rule_values(message, arguments)
            if error:
                return error
            effective_from = arguments.get("effective_from")
            if effective_from is None:
                effective_from = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            rule = billing.create_category_rule(
                client_request_id=_request_key(request_id, name, arguments),
                merchant=arguments["merchant"],
                category=arguments["category"],
                effective_from=effective_from,
                transaction_kind=arguments.get("transaction_kind"),
                note_contains=arguments.get("note_contains"),
            )
            return {
                "rule": rule,
                "message": f"已记录长期分类偏好：{rule['merchant']}归为“{rule['category']}”，从{rule['effective_from']}起生效。",
            }

        if name == "update_category_rule":
            allowed = {"rule_id", "version", "merchant", "category", "transaction_kind", "note_contains", "effective_from"}
            if set(arguments) - allowed or not {"rule_id", "version"}.issubset(arguments):
                return _failure("INVALID_ARGUMENTS", "修改规则参数不完整或包含不支持字段。")
            rule = _get_rule(billing, arguments["rule_id"])
            if rule is None:
                return _failure("CATEGORY_RULE_NOT_FOUND", "没有找到要修改的个人分类规则；请先查看当前规则。")
            if rule["version"] != arguments["version"]:
                return _failure("VERSION_CONFLICT", "这条规则已更新，请重新查看后再修改。")
            candidates = _same_merchant_rules(billing.list_category_rules(), rule)
            if len(candidates) > 1 and not _rule_disambiguated(message, rule, candidates):
                return _failure(
                    "CATEGORY_RULE_AMBIGUOUS",
                    "这个商户有多条分类规则，请说明交易类型、备注匹配文字、现有分类或生效日期，再修改其中一条。",
                )
            error = _validate_update_intent(message, rule, arguments)
            if error:
                return error
            changes = {key: value for key, value in arguments.items() if key not in {"rule_id", "version"}}
            if not changes:
                return _failure("INVALID_ARGUMENTS", "没有提供要修改的规则字段。")
            updated = billing.update_category_rule(rule["id"], rule["version"], **changes)
            return {
                "rule": updated,
                "message": f"已更新{updated['merchant']}的长期分类规则：归为“{updated['category']}”。",
            }

        if name == "delete_category_rule":
            allowed = {"rule_id", "version"}
            if set(arguments) != allowed:
                return _failure("INVALID_ARGUMENTS", "删除规则只需要规则编号和版本，请核对后重试。")
            rule = _get_rule(billing, arguments["rule_id"])
            if rule is None:
                return _failure("CATEGORY_RULE_NOT_FOUND", "没有找到要删除的个人分类规则；请先查看当前规则。")
            if rule["version"] != arguments["version"]:
                return _failure("VERSION_CONFLICT", "这条规则已更新，请重新查看后再删除。")
            error = _validate_delete_intent(message, rule)
            if error:
                return error
            candidates = _same_merchant_rules(billing.list_category_rules(), rule)
            if len(candidates) > 1:
                if re.search(r"全部|所有|全都|全部都", message):
                    return _failure(
                        "CATEGORY_RULE_BULK_DELETE_UNSUPPORTED",
                        "为避免误删，我会逐条删除。请指出交易类型、备注匹配文字、现有分类或生效日期，再删除对应规则。",
                    )
                if not _rule_disambiguated(message, rule, candidates):
                    return _failure(
                        "CATEGORY_RULE_AMBIGUOUS",
                        "这个商户有多条分类规则，请说明交易类型、备注匹配文字、现有分类或生效日期，再删除其中一条。",
                    )
            billing.delete_category_rule(rule["id"], rule["version"])
            return {
                "rule": rule,
                "message": f"已删除{rule['merchant']}的长期分类规则；这不会移除已有的单笔分类标注。",
            }

        return _failure("UNKNOWN_TOOL", "当前不能执行这项分类偏好操作。")
    except BillingError as error:
        return _failure(error.code, error.message)
    except (TypeError, ValueError, KeyError):
        return _failure("INVALID_ARGUMENTS", "分类规则内容不完整或格式不正确，请核对后重试。")
