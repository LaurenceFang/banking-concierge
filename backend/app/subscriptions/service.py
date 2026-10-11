"""Deterministic recurring-charge candidates and safe demo subscription actions."""

from __future__ import annotations

import calendar
import hashlib
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.bank import BankError

from .models import Base, RenewalNotification, SubscriptionActionPlan, SubscriptionContract

PLAN_TTL = timedelta(minutes=10)
SHANGHAI = ZoneInfo("Asia/Shanghai")
NEAR_DUPLICATE_DAYS = 4

DEMO_CONTRACTS: tuple[dict[str, Any], ...] = (
    {
        "id": "demo-sub-video",
        "merchant": "星云视频",
        "amount_minor": 2_390,
        "charge_day": 15,
        "cancel_supported": True,
    },
    {
        "id": "demo-sub-music",
        "merchant": "云舟音乐",
        "amount_minor": 1_500,
        "charge_day": 21,
        "cancel_supported": True,
    },
    {
        "id": "demo-sub-cloud",
        "merchant": "轻云存储",
        "amount_minor": 1_800,
        "charge_day": 28,
        "cancel_supported": False,
    },
)

_ALIASES: dict[str, str] = {}
for _canonical, _values in {
    "星云视频": (
        "星云视频",
        "星云视频会员",
        "星云视频VIP",
        "星云视听",
        "星云影视",
        "Nebula Video",
        "Xingyun Video",
    ),
    "云舟音乐": (
        "云舟音乐",
        "云舟音乐会员",
        "云舟音乐VIP",
        "云舟Music",
        "Yunzhou Music",
    ),
    "轻云存储": (
        "轻云存储",
        "轻云云盘",
        "轻云存储会员",
        "Qingyun Storage",
        "Qingyun Drive",
    ),
}.items():
    for _alias in _values:
        _ALIASES[re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", _alias).casefold())] = (
            _canonical
        )

_KNOWN_CONTRACT_MERCHANTS = {row["merchant"] for row in DEMO_CONTRACTS}
_RENT_TERMS = ("公寓", "房租", "租金", "月租", "apartment", "rent")


class SubscriptionService:
    """Persist fictional contracts without reading or changing bank ledger rows."""

    def __init__(self, database_url: str) -> None:
        if database_url.startswith("postgres://"):
            database_url = "postgresql+psycopg://" + database_url.removeprefix("postgres://")
        elif database_url.startswith("postgresql://"):
            database_url = "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
        self.database_url = database_url
        engine_options: dict[str, Any] = {"pool_pre_ping": True}
        if database_url.startswith("sqlite"):
            engine_options["connect_args"] = {"check_same_thread": False, "timeout": 10}
            if database_url.endswith((":memory:", ":memory:?cache=shared")):
                engine_options["poolclass"] = StaticPool
        self.engine: Engine = create_engine(database_url, **engine_options)
        if self.engine.dialect.name == "sqlite":
            self._configure_sqlite(self.engine)
        self._session_factory = sessionmaker(self.engine, expire_on_commit=False, class_=Session)
        Base.metadata.create_all(self.engine)

    @staticmethod
    def _configure_sqlite(engine: Engine) -> None:
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.close()

    @property
    def dialect_name(self) -> str:
        return self.engine.dialect.name

    def close(self) -> None:
        """Release pooled database connections."""

        self.engine.dispose()

    def _begin_write(self, session: Session) -> None:
        # PostgreSQL serializes decisions with row locks. SQLite has no FOR UPDATE,
        # so acquire its reserved write lock before reading decision state.
        if self.dialect_name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        with self._session_factory() as session, session.begin():
            self._begin_write(session)
            yield session

    def seed_demo(self) -> None:
        """Insert the three exact fictional demo contracts once, without overwrites."""

        today = datetime.now(SHANGHAI).date()
        with self._transaction() as session:
            for row in DEMO_CONTRACTS:
                if session.get(SubscriptionContract, row["id"]) is not None:
                    continue
                next_charge = _next_day_in_month(today, row["charge_day"])
                session.add(
                    SubscriptionContract(
                        id=row["id"],
                        merchant=row["merchant"],
                        amount_minor=row["amount_minor"],
                        currency="CNY",
                        cycle="monthly",
                        next_charge_on=next_charge,
                        status="active",
                        autopay_enabled=True,
                        cancel_supported=row["cancel_supported"],
                        source="demo_contract",
                    )
                )

    def detect(
        self,
        entries: Iterable[dict[str, Any]],
        as_of: date | datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        """Find recurring expense patterns, keeping each as an unconfirmed candidate."""

        reference_date = _as_local_date(as_of) if as_of is not None else datetime.now(SHANGHAI).date()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        names: dict[str, str] = {}
        for index, entry in enumerate(entries):
            normalized = _candidate_entry(entry, index)
            if normalized is None:
                continue
            key, merchant, has_rent_marker = normalized
            if merchant in _KNOWN_CONTRACT_MERCHANTS:
                continue
            grouped[key].append({**entry, "_merchant": merchant, "_rent_marker": has_rent_marker})
            names[key] = merchant

        candidates: list[dict[str, Any]] = []
        for key, rows in grouped.items():
            rows.sort(key=lambda row: (_parse_date(row.get("date")) or date.min, str(row.get("id", ""))))
            distinct: dict[str, dict[str, Any]] = {}
            for index, row in enumerate(rows):
                entry_id = str(row.get("id") or f"row-{index}")
                distinct.setdefault(entry_id, row)
            evidence = list(distinct.values())
            if len(evidence) < 2:
                continue
            parsed = [(row, _parse_date(row.get("date"))) for row in evidence]
            parsed = [(row, event_date) for row, event_date in parsed if event_date is not None]
            if len(parsed) < 2:
                continue
            parsed.sort(key=lambda item: item[1])
            event_clusters = _cluster_near_duplicate_entries(parsed)
            if len(event_clusters) < 2:
                continue
            event_dates = [cluster["date"] for cluster in event_clusters]
            amounts = [cluster["amount_minor"] for cluster in event_clusters]
            intervals = [(event_dates[i] - event_dates[i - 1]).days for i in range(1, len(event_dates))]
            pattern = _infer_cycle(event_dates, intervals)
            if pattern is None:
                continue
            cycle, cycle_counts = pattern
            missing_cycles = sum(max(0, count - 1) for count in cycle_counts)
            price_change = len(set(amounts)) > 1
            confidence = _candidate_confidence(
                len(event_clusters), len(intervals), missing_cycles, price_change
            )
            rent = any(bool(row["_rent_marker"]) for row, _event_date in parsed)
            merchant = names[key]
            is_subscription = not rent and confidence >= 0.55
            next_charge = _next_expected_charge(event_dates[-1], cycle, reference_date)
            reason = _candidate_reason(
                merchant=merchant,
                cycle=cycle,
                event_count=len(event_clusters),
                missing_cycles=missing_cycles,
                price_change=price_change,
                first_amount=amounts[0],
                latest_amount=amounts[-1],
                rent=rent,
                confidence=confidence,
                merged_entry_count=len(parsed) - len(event_clusters),
            )
            candidate_key = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
            candidates.append(
                {
                    "id": f"candidate-{candidate_key}",
                    "merchant": merchant,
                    "cycle": cycle,
                    "amount_minor": amounts[-1],
                    "confidence": confidence,
                    "next_charge_on": next_charge.isoformat(),
                    "price_change": price_change,
                    "missing_cycles": missing_cycles,
                    "reason": reason,
                    "entry_ids": [str(row.get("id") or "") for row, _event_date in parsed],
                    "is_subscription_candidate": is_subscription,
                }
            )
        return sorted(candidates, key=lambda item: (-item["confidence"], item["merchant"].casefold()))

    def list_subscriptions(
        self,
        entries: Iterable[dict[str, Any]],
        as_of: date | datetime | str | None = None,
    ) -> dict[str, Any]:
        with self._session_factory() as session:
            contracts = session.scalars(
                select(SubscriptionContract).order_by(SubscriptionContract.merchant)
            ).all()
            return {
                "subscriptions": [_contract_dict(contract) for contract in contracts],
                "candidates": self.detect(entries, as_of=as_of),
            }

    def create_action_plan(
        self,
        subscription_id: str,
        action: str,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        if action not in {"cancel_subscription", "stop_autopay"}:
            raise BankError("INVALID_ACTION", "订阅操作只能是 cancel_subscription 或 stop_autopay。")
        if conversation_id is not None and len(conversation_id) > 128:
            raise BankError("INVALID_CONVERSATION", "会话标识长度无效。")

        now = _utcnow()
        plan_id = f"subscription-plan-{uuid4()}"
        with self._transaction() as session:
            contract = self._get_contract_for_update(session, subscription_id)
            if contract is None:
                raise BankError("SUBSCRIPTION_NOT_FOUND", "未找到这份模拟订阅合同。")
            if contract.status != "active":
                raise BankError("SUBSCRIPTION_NOT_ACTIVE", "该订阅合同已取消，无法创建新的操作预览。")
            if action == "cancel_subscription" and not contract.cancel_supported:
                raise BankError(
                    "CANCEL_NOT_SUPPORTED",
                    f"{contract.merchant}不支持商户合同取消；合同和代扣状态均未改变。若要阻止账户代扣，请单独预览并确认“停止代扣”。",
                )
            if action == "stop_autopay" and not contract.autopay_enabled:
                raise BankError("AUTOPAY_ALREADY_DISABLED", "该合同的代扣已关闭，合同状态未改变。")

            if conversation_id:
                self._invalidate_pending_in_session(session, conversation_id, now)
            plan = SubscriptionActionPlan(
                id=plan_id,
                subscription_id=contract.id,
                action=action,
                status="pending",
                conversation_id=conversation_id,
                created_at=now,
                expires_at=now + PLAN_TTL,
            )
            session.add(plan)
            session.flush()
            return _action_dict(plan, contract)

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        if not plan_id:
            return None
        now = _utcnow()
        with self._transaction() as session:
            plan = self._get_plan_for_update(session, plan_id)
            if plan is None:
                return None
            if plan.status == "pending" and _as_utc(plan.expires_at) <= now:
                self._mark_expired(plan, now)
            contract = session.get(SubscriptionContract, plan.subscription_id)
            if contract is None:
                return None
            return _action_dict(plan, contract)

    def pending_for_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        if not conversation_id:
            return None
        now = _utcnow()
        with self._transaction() as session:
            plans = self._plans_for_conversation(session, conversation_id, pending_only=True)
            for plan in plans:
                if _as_utc(plan.expires_at) <= now:
                    self._mark_expired(plan, now)
            active = [plan for plan in plans if plan.status == "pending"]
            if not active:
                return None
            plan = max(active, key=lambda row: (row.created_at, row.id))
            contract = session.get(SubscriptionContract, plan.subscription_id)
            return _action_dict(plan, contract) if contract is not None else None

    def invalidate_pending_plans(self, conversation_id: str) -> int:
        if not conversation_id:
            return 0
        now = _utcnow()
        with self._transaction() as session:
            return self._invalidate_pending_in_session(session, conversation_id, now)

    def decide_plan(self, plan_id: str, decision: str) -> dict[str, Any]:
        if decision not in {"approve", "decline"}:
            raise BankError("INVALID_DECISION", "订阅操作只能确认 approve 或拒绝 decline。")
        now = _utcnow()
        with self._transaction() as session:
            plan = self._get_plan_for_update(session, plan_id)
            if plan is None:
                raise BankError("PLAN_NOT_FOUND", "未找到这份订阅操作预览。")

            if plan.status in {"completed", "declined", "expired"} and plan.outcome_status is not None:
                if plan.decision is not None and plan.decision != decision:
                    raise BankError("PLAN_ALREADY_DECIDED", "该订阅操作预览已完成决定，不能更改。")
                if plan.status == "expired" and plan.decision is None:
                    plan.decision = decision
                    plan.decided_at = now
                return _decision_result(plan)
            if plan.status == "invalidated":
                raise BankError("PLAN_INVALIDATED", "订阅操作信息已更新，旧预览已失效。")
            if plan.status != "pending":
                raise BankError("PLAN_STATE_INVALID", "订阅操作预览状态已变化，请重新发起。")
            if _as_utc(plan.expires_at) <= now:
                self._mark_expired(plan, now, decision=decision)
                return _decision_result(plan)

            if decision == "decline":
                plan.status = "declined"
                plan.decision = "decline"
                plan.outcome_status = "declined"
                plan.outcome_code = None
                plan.outcome_message = "已取消本次订阅操作；合同状态和代扣授权均未改变。"
                plan.decided_at = now
                return _decision_result(plan)

            contract = self._get_contract_for_update(session, plan.subscription_id)
            if contract is None:
                raise BankError("SUBSCRIPTION_NOT_FOUND", "未找到这份模拟订阅合同。")
            if contract.status != "active":
                self._decline_state_changed(plan, now)
                return _decision_result(plan)
            if plan.action == "cancel_subscription":
                if not contract.cancel_supported:
                    self._decline_unsupported(plan, contract, now)
                    return _decision_result(plan)
                contract.status = "cancelled"
                contract.autopay_enabled = False
                plan.outcome_message = f"已在模拟商户侧取消{contract.merchant}合同，后续续费和代扣均已停止。"
            elif plan.action == "stop_autopay":
                if not contract.autopay_enabled:
                    self._decline_state_changed(plan, now)
                    return _decision_result(plan)
                contract.autopay_enabled = False
                # Stopping the payment mandate does not cancel the merchant service.
                plan.outcome_message = (
                    f"已关闭{contract.merchant}的模拟代扣授权；订阅合同仍有效，商户服务并未取消。"
                )
            else:
                raise BankError("PLAN_STATE_INVALID", "订阅操作预览中的操作类型无效。")

            plan.status = "completed"
            plan.decision = "approve"
            plan.outcome_status = "completed"
            plan.outcome_code = None
            plan.operation_id = f"subscription-op-{uuid4()}"
            plan.decided_at = now
            return _decision_result(plan)

    def completed_events(self) -> list[dict[str, Any]]:
        """Rebuild idempotent completion events from committed action-plan rows."""

        with self._session_factory() as session:
            plans = session.scalars(
                select(SubscriptionActionPlan)
                .where(
                    SubscriptionActionPlan.status == "completed",
                    SubscriptionActionPlan.outcome_status == "completed",
                    SubscriptionActionPlan.operation_id.is_not(None),
                )
                .order_by(SubscriptionActionPlan.decided_at, SubscriptionActionPlan.id)
            ).all()
            events: list[dict[str, Any]] = []
            for plan in plans:
                contract = session.get(SubscriptionContract, plan.subscription_id)
                if contract is None or plan.operation_id is None:
                    continue
                if plan.action == "cancel_subscription":
                    title = f"{contract.merchant}订阅已取消"
                else:
                    title = f"{contract.merchant}代扣已停止"
                message = plan.outcome_message or "模拟订阅操作已完成。"
                receipt = {
                    "status": "completed",
                    "kind": "subscription",
                    "operation_id": plan.operation_id,
                    "action": plan.action,
                    "subscription_id": contract.id,
                    "merchant": contract.merchant,
                    "amount_minor": contract.amount_minor,
                    "currency": contract.currency,
                    "message": message,
                }
                events.append(
                    {
                        "id": f"subscription:{plan.operation_id}",
                        "title": title,
                        "text": message,
                        "kind": "subscription",
                        "source": "subscriptions",
                        "created_at": _iso_utc(plan.decided_at or plan.created_at),
                        "plan_id": plan.id,
                        "conversation_id": plan.conversation_id,
                        "operation_id": plan.operation_id,
                        "receipt": receipt,
                    }
                )
            return events

    def persisted_notifications(self) -> list[dict[str, Any]]:
        """Return every durable renewal notice so ConversationStore can recover them."""

        with self._session_factory() as session:
            rows = session.scalars(
                select(RenewalNotification).order_by(
                    RenewalNotification.created_at, RenewalNotification.id
                )
            ).all()
            return [_notification_dict(row) for row in rows]

    def renewal_notifications(
        self,
        entries: Iterable[dict[str, Any]],
        as_of: date | datetime | str | None = None,
        window_days: int = 7,
    ) -> list[dict[str, Any]]:
        """Persist and return newly due demo-contract reminders; never charge an account."""

        del entries  # Bill evidence is used for candidates, never to authorize contract reminders.
        if isinstance(window_days, bool) or not isinstance(window_days, int) or window_days < 0:
            raise BankError("INVALID_WINDOW", "续费提醒窗口必须是大于或等于零的整数天。")
        today = _as_local_date(as_of) if as_of is not None else datetime.now(SHANGHAI).date()
        end_date = today + timedelta(days=window_days)
        now = _utcnow()
        created: list[dict[str, Any]] = []
        with self._transaction() as session:
            query = select(SubscriptionContract).where(
                SubscriptionContract.status == "active",
                SubscriptionContract.autopay_enabled.is_(True),
                SubscriptionContract.source == "demo_contract",
                SubscriptionContract.next_charge_on >= today,
                SubscriptionContract.next_charge_on <= end_date,
            )
            if self.dialect_name == "postgresql":
                query = query.with_for_update()
            contracts = session.scalars(
                query.order_by(SubscriptionContract.next_charge_on, SubscriptionContract.id)
            ).all()
            for contract in contracts:
                notification_id = f"renewal-{contract.id}-{contract.next_charge_on.isoformat()}"
                if session.get(RenewalNotification, notification_id) is not None:
                    continue
                title = f"{contract.merchant}即将续费"
                text = (
                    f"{contract.merchant}模拟订阅预计于{contract.next_charge_on.isoformat()}续费，"
                    f"金额为{_format_cny(contract.amount_minor)}。这是续费提醒，不会自动扣款。"
                )
                row = RenewalNotification(
                    id=notification_id,
                    subscription_id=contract.id,
                    next_charge_on=contract.next_charge_on,
                    title=title,
                    text=text,
                    kind="renewal",
                    source=contract.source,
                    created_at=now,
                )
                session.add(row)
                session.flush()
                created.append(_notification_dict(row))
        return created

    def _get_plan_for_update(self, session: Session, plan_id: str) -> SubscriptionActionPlan | None:
        query = select(SubscriptionActionPlan).where(SubscriptionActionPlan.id == plan_id)
        if self.dialect_name == "postgresql":
            query = query.with_for_update()
        return session.scalar(query)

    def _get_contract_for_update(
        self, session: Session, subscription_id: str
    ) -> SubscriptionContract | None:
        query = select(SubscriptionContract).where(SubscriptionContract.id == subscription_id)
        if self.dialect_name == "postgresql":
            query = query.with_for_update()
        return session.scalar(query)

    def _plans_for_conversation(
        self, session: Session, conversation_id: str, *, pending_only: bool
    ) -> list[SubscriptionActionPlan]:
        query = select(SubscriptionActionPlan).where(
            SubscriptionActionPlan.conversation_id == conversation_id
        )
        if pending_only:
            query = query.where(SubscriptionActionPlan.status == "pending")
        query = query.order_by(SubscriptionActionPlan.created_at, SubscriptionActionPlan.id)
        if self.dialect_name == "postgresql":
            query = query.with_for_update()
        return list(session.scalars(query).all())

    def _invalidate_pending_in_session(
        self, session: Session, conversation_id: str, now: datetime
    ) -> int:
        plans = self._plans_for_conversation(session, conversation_id, pending_only=True)
        count = 0
        for plan in plans:
            if _as_utc(plan.expires_at) <= now:
                self._mark_expired(plan, now)
                continue
            plan.status = "invalidated"
            plan.outcome_status = "declined"
            plan.outcome_code = "PLAN_INVALIDATED"
            plan.outcome_message = "订阅操作信息已更新，旧预览已失效。"
            plan.decided_at = now
            count += 1
        return count

    @staticmethod
    def _mark_expired(
        plan: SubscriptionActionPlan,
        now: datetime,
        decision: str | None = None,
    ) -> None:
        plan.status = "expired"
        if decision is not None:
            plan.decision = decision
        plan.outcome_status = "declined"
        plan.outcome_code = "PLAN_EXPIRED"
        plan.outcome_message = "订阅操作预览已过期，合同和代扣授权均未改变。"
        plan.decided_at = now

    @staticmethod
    def _decline_state_changed(plan: SubscriptionActionPlan, now: datetime) -> None:
        plan.status = "declined"
        plan.decision = "approve"
        plan.outcome_status = "declined"
        plan.outcome_code = "SUBSCRIPTION_STATE_CHANGED"
        plan.outcome_message = "订阅合同或代扣状态已变化，本次操作未执行，请重新查询后再操作。"
        plan.decided_at = now

    @staticmethod
    def _decline_unsupported(
        plan: SubscriptionActionPlan, contract: SubscriptionContract, now: datetime
    ) -> None:
        plan.status = "declined"
        plan.decision = "approve"
        plan.outcome_status = "declined"
        plan.outcome_code = "CANCEL_NOT_SUPPORTED"
        plan.outcome_message = (
            f"{contract.merchant}不支持模拟商户取消，合同和代扣状态未改变。"
            "如需阻止账户代扣，请单独预览并确认停止代扣。"
        )
        plan.decided_at = now


def _candidate_entry(entry: dict[str, Any], index: int) -> tuple[str, str, bool] | None:
    kind = str(entry.get("transaction_kind", "")).casefold()
    try:
        amount = int(entry.get("amount_minor"))
    except (TypeError, ValueError):
        return None
    if kind != "expense" or amount >= 0 or _parse_date(entry.get("date")) is None:
        return None

    source_names = [str(entry.get(key) or "").strip() for key in ("merchant", "raw_merchant")]
    source_names = [name for name in source_names if name]
    if not source_names:
        return None
    canonical = None
    for name in source_names:
        normalized = _normalize_merchant(name)
        for alias, merchant in sorted(_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
            if alias and alias in normalized:
                canonical = merchant
                break
        if canonical:
            break
    merchant = canonical or source_names[0]
    normalized_merchant = _normalize_merchant(merchant)
    if not normalized_merchant:
        normalized_merchant = f"unknown-{index}"
    rent_marker = any(
        term in unicodedata.normalize("NFKC", name).casefold() for name in source_names for term in _RENT_TERMS
    )
    return normalized_merchant, merchant, rent_marker


def _cluster_near_duplicate_entries(
    rows: list[tuple[dict[str, Any], date]],
) -> list[dict[str, Any]]:
    """Treat same-merchant charges within four days as one cycle event.

    The last row supplies that event's representative date and amount; entry_ids
    are still collected from every source row for auditability. A weekly cadence
    remains visible because separate weekly events are at least seven days apart.
    """

    clusters: list[dict[str, Any]] = []
    for row, event_date in rows:
        if clusters and (event_date - clusters[-1]["last_date"]).days <= NEAR_DUPLICATE_DAYS:
            cluster = clusters[-1]
            cluster["last_date"] = event_date
            cluster["date"] = event_date
            cluster["amount_minor"] = abs(int(row["amount_minor"]))
            continue
        clusters.append(
            {
                "last_date": event_date,
                "date": event_date,
                "amount_minor": abs(int(row["amount_minor"])),
            }
        )
    return clusters


def _infer_cycle(
    event_dates: list[date], intervals: list[int]
) -> tuple[str, list[int]] | None:
    if len(event_dates) < 2 or not intervals or any(days <= 0 for days in intervals):
        return None

    # Only classify a short cadence as weekly when at least one observed gap is
    # within two weeks. This avoids calling a lone 28-day interval “four weeks”.
    weekly_counts = [max(1, round(days / 7)) for days in intervals]
    if min(intervals) <= 14 and all(abs(days - count * 7) <= 1 for days, count in zip(intervals, weekly_counts)):
        return "weekly", weekly_counts

    month_counts: list[int] = []
    for start, end in pairwise(event_dates):
        count = _matching_month_count(start, end)
        if count is None:
            return None
        month_counts.append(count)

    if all(count >= 12 and count % 12 == 0 for count in month_counts):
        return "yearly", [count // 12 for count in month_counts]
    if all(count >= 3 and count % 3 == 0 for count in month_counts):
        return "quarterly", [count // 3 for count in month_counts]
    if min(month_counts) <= 2:
        return "monthly", month_counts
    return None


def _matching_month_count(start: date, end: date) -> int | None:
    month_delta = (end.year - start.year) * 12 + end.month - start.month
    if month_delta <= 0:
        return None
    anchor_eom = start.day == calendar.monthrange(start.year, start.month)[1]
    for count in (month_delta, month_delta - 1, month_delta + 1):
        if count <= 0 or count > 240:
            continue
        expected = _add_months(start, count, anchor_eom=anchor_eom)
        if abs((end - expected).days) <= 3:
            return count
    return None


def _candidate_confidence(
    event_count: int,
    interval_count: int,
    missing_cycles: int,
    price_change: bool,
) -> float:
    value = 0.60 + 0.09 * max(0, event_count - 2) + 0.04 * max(0, interval_count - 1)
    value -= min(0.15, 0.035 * missing_cycles)
    value -= 0.05 if price_change else 0
    return round(min(0.97, max(0.40, value)), 2)


def _candidate_reason(
    *,
    merchant: str,
    cycle: str,
    event_count: int,
    missing_cycles: int,
    price_change: bool,
    first_amount: int,
    latest_amount: int,
    rent: bool,
    confidence: float,
    merged_entry_count: int,
) -> str:
    cycle_name = {"weekly": "每周", "monthly": "每月", "quarterly": "每季度", "yearly": "每年"}[cycle]
    detail = f"{event_count} 笔支出呈{cycle_name}节奏，估计置信度 {confidence:.2f}。"
    if merged_entry_count:
        detail += f"同日或相邻4天内的多笔记录合并为周期事件，仍保留全部流水证据（{merged_entry_count}笔额外记录）。"
    if missing_cycles:
        detail += f"观察区间内约漏过 {missing_cycles} 个周期。"
    if price_change:
        detail += f"金额从 {_format_cny(first_amount)} 变为 {_format_cny(latest_amount)}，按最近金额估算。"
    if rent:
        return f"{detail}{merchant}含公寓/租金标识，属于周期性居住消费，不视为订阅合同。"
    return f"{detail}这是账单证据推测的订阅候选，尚未确认存在商户合同。"


def _next_expected_charge(last_event: date, cycle: str, as_of: date) -> date:
    candidate = _advance_cycle(last_event, cycle)
    for _ in range(500):
        if candidate >= as_of:
            return candidate
        candidate = _advance_cycle(candidate, cycle)
    return candidate


def _advance_cycle(value: date, cycle: str) -> date:
    if cycle == "weekly":
        return value + timedelta(days=7)
    multiplier = {"monthly": 1, "quarterly": 3, "yearly": 12}[cycle]
    return _add_months(value, multiplier, anchor_eom=value.day == calendar.monthrange(value.year, value.month)[1])


def _add_months(value: date, count: int, *, anchor_eom: bool) -> date:
    month_index = value.year * 12 + (value.month - 1) + count
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    last_day = calendar.monthrange(year, month)[1]
    day = last_day if anchor_eom else min(value.day, last_day)
    return date(year, month, day)


def _next_day_in_month(today: date, charge_day: int) -> date:
    day = min(charge_day, calendar.monthrange(today.year, today.month)[1])
    this_month = date(today.year, today.month, day)
    if this_month >= today:
        return this_month
    return _add_months(this_month, 1, anchor_eom=False)


def _normalize_merchant(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return _as_local_date(value)
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _as_local_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.date()
        return value.astimezone(SHANGHAI).date()
    if isinstance(value, date):
        return value
    parsed = _parse_date(value)
    if parsed is None:
        raise BankError("INVALID_DATE", "日期必须是有效的 YYYY-MM-DD 日期。")
    return parsed


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _format_cny(amount_minor: int) -> str:
    return f"¥{Decimal(amount_minor) / Decimal(100):,.2f}"


def _contract_dict(contract: SubscriptionContract) -> dict[str, Any]:
    return {
        "id": contract.id,
        "merchant": contract.merchant,
        "amount_minor": contract.amount_minor,
        "currency": contract.currency,
        "cycle": contract.cycle,
        "next_charge_on": contract.next_charge_on.isoformat(),
        "status": contract.status,
        "autopay_enabled": contract.autopay_enabled,
        "cancel_supported": contract.cancel_supported,
        "source": contract.source,
    }


def _action_dict(
    plan: SubscriptionActionPlan, contract: SubscriptionContract
) -> dict[str, Any]:
    if plan.action == "cancel_subscription":
        title = f"取消{contract.merchant}订阅"
        description = (
            f"确认后将在模拟商户侧取消{contract.merchant}合同（{_format_cny(contract.amount_minor)}/月）；"
            "合同将标记为已取消，后续续费停止。"
        )
    else:
        title = f"停止{contract.merchant}代扣"
        description = (
            f"确认后仅关闭{contract.merchant}的银行侧代扣授权（{_format_cny(contract.amount_minor)}/月）；"
            "订阅合同仍有效，商户服务不会因此取消。"
        )
    result: dict[str, Any] = {
        "id": plan.id,
        "kind": "subscription",
        "action": plan.action,
        "subscription_id": contract.id,
        "merchant": contract.merchant,
        "title": title,
        "description": description,
        "status": plan.status,
        "expires_at": _iso_utc(plan.expires_at),
    }
    if plan.conversation_id is not None:
        result["conversation_id"] = plan.conversation_id
    return result


def _decision_result(plan: SubscriptionActionPlan) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": plan.outcome_status or "declined",
        "message": plan.outcome_message or "订阅操作未执行。",
    }
    if plan.operation_id is not None:
        result["operation_id"] = plan.operation_id
    if plan.outcome_code is not None:
        result["code"] = plan.outcome_code
    if plan.conversation_id is not None:
        result["conversation_id"] = plan.conversation_id
    return result


def _notification_dict(row: RenewalNotification) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "text": row.text,
        "kind": row.kind,
        "source": row.source,
        "created_at": _iso_utc(row.created_at),
        "subscription_id": row.subscription_id,
        "next_charge_on": row.next_charge_on.isoformat(),
    }
