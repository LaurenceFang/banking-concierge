"""Service layer for the fictional demo user's cards and card actions."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.bank.service import BankError

from .models import Base, Card, CardActionPlan, CardApplication

DEMO_USER_ID = "demo-user-001"
MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991
PLAN_TTL = timedelta(minutes=10)
DAILY_LIMIT_MIN_MINOR = 10_000
DAILY_LIMIT_MAX_MINOR = 2_000_000
CREDIT_LIMIT_MIN_MINOR = 300_000
CREDIT_LIMIT_MAX_MINOR = 5_000_000


class CardService:
    """Read and update only card-owned rows for the fixed demo user."""

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
        # PostgreSQL uses row locks at the card/plan rows being changed. SQLite
        # has no FOR UPDATE, so reserve the write lock before reading decision state.
        if self.dialect_name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        with self._session_factory() as session, session.begin():
            self._begin_write(session)
            yield session

    def _lock_conversation(self, session: Session, conversation_id: str) -> None:
        if self.dialect_name == "postgresql":
            # Serialize previews for one conversation even when no pending row
            # exists yet. The partial unique index is a second line of defense.
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:conversation_id, 0))"),
                {"conversation_id": conversation_id},
            )

    def seed_demo(self) -> None:
        """Insert the two fictional cards once and preserve any later card state."""

        rows = (
            {
                "id": "demo-card-debit-001",
                "name": "日常借记卡",
                "kind": "debit",
                "last4": "6028",
                "account_id": "demo-checking-001",
                "daily_limit_minor": 500_000,
                "credit_limit_minor": None,
                "outstanding_minor": 0,
            },
            {
                "id": "demo-card-credit-001",
                "name": "轻享信用卡",
                "kind": "credit",
                "last4": "7391",
                "account_id": "demo-checking-001",
                "daily_limit_minor": 300_000,
                "credit_limit_minor": 2_000_000,
                "outstanding_minor": 85_000,
            },
        )
        with self._transaction() as session:
            for row in rows:
                if session.get(Card, row["id"]) is not None:
                    continue
                session.add(Card(owner_id=DEMO_USER_ID, status="active", currency="CNY", version=1, **row))

    def list_cards(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(Card).where(Card.owner_id == DEMO_USER_ID).order_by(Card.kind, Card.id)
            ).all()
            return [_card_dict(row) for row in rows]

    def list_applications(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(CardApplication)
                .where(CardApplication.owner_id == DEMO_USER_ID)
                .order_by(CardApplication.submitted_at, CardApplication.id)
            ).all()
            return [_application_dict(row) for row in rows]

    def create_action_plan(
        self,
        action: str,
        conversation_id: str,
        card_id: str | None = None,
        amount_minor: int | None = None,
        card_kind: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(action, str) or action not in {
            "freeze",
            "unfreeze",
            "report_lost",
            "set_daily_limit",
            "set_credit_limit",
            "apply_card",
        }:
            raise BankError("INVALID_ACTION", "卡片操作类型无效。")
        _validate_conversation_id(conversation_id)
        _validate_action_arguments(action, card_id, amount_minor, card_kind)

        now = _utcnow()
        plan_id = f"card-plan-{uuid4()}"
        with self._transaction() as session:
            card: Card | None = None
            before: dict[str, object]
            after: dict[str, object]
            card_version: int | None = None
            if action == "apply_card":
                before = {"application_status": None}
                after = {"application_status": "submitted"}
            else:
                card = self._get_card_for_update(session, card_id or "")
                if card is None:
                    raise BankError("CARD_NOT_FOUND", "未找到当前演示用户的这张卡片。")
                before, after = _build_card_change(action, card, amount_minor)
                card_version = card.version

            self._lock_conversation(session, conversation_id)
            self._invalidate_pending_in_session(session, conversation_id, now)
            # Make old plans leave the partial pending-plan index before adding
            # this conversation's replacement plan.
            session.flush()
            plan = CardActionPlan(
                id=plan_id,
                owner_id=DEMO_USER_ID,
                action=action,
                card_id=card.id if card is not None else None,
                card_kind=card.kind if card is not None else card_kind,
                amount_minor=amount_minor,
                card_version=card_version,
                before_state=before,
                after_state=after,
                status="pending",
                conversation_id=conversation_id,
                created_at=now,
                expires_at=now + PLAN_TTL,
            )
            session.add(plan)
            session.flush()
            return _action_dict(plan, card)

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        if not isinstance(plan_id, str) or not plan_id:
            return None
        now = _utcnow()
        with self._transaction() as session:
            plan = self._get_plan_for_update(session, plan_id)
            if plan is None:
                return None
            if plan.status == "pending" and _as_utc(plan.expires_at) <= now:
                self._mark_expired(plan, now)
            card = session.get(Card, plan.card_id) if plan.card_id else None
            return _action_dict(plan, card)

    def pending_for_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        if not conversation_id:
            return None
        _validate_conversation_id(conversation_id)
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
            card = session.get(Card, plan.card_id) if plan.card_id else None
            return _action_dict(plan, card)

    def invalidate_pending_plans(self, conversation_id: str) -> int:
        if not conversation_id:
            return 0
        _validate_conversation_id(conversation_id)
        now = _utcnow()
        with self._transaction() as session:
            self._lock_conversation(session, conversation_id)
            return self._invalidate_pending_in_session(session, conversation_id, now)

    def decide_plan(self, plan_id: str, decision: str) -> dict[str, Any]:
        if not isinstance(plan_id, str) or not plan_id:
            raise BankError("PLAN_NOT_FOUND", "未找到当前演示用户的卡片操作预览。")
        if not isinstance(decision, str) or decision not in {"approve", "decline"}:
            raise BankError("INVALID_DECISION", "操作决定无效，只能确认执行或拒绝本次操作。")

        now = _utcnow()
        with self._transaction() as session:
            # Lock rows in card -> plan order. Competing plans for a card are
            # serialized on its version, and concurrent retries stay idempotent.
            card_id = session.scalar(
                select(CardActionPlan.card_id).where(
                    CardActionPlan.id == plan_id,
                    CardActionPlan.owner_id == DEMO_USER_ID,
                )
            )
            if card_id is None:
                plan_exists = session.scalar(
                    select(CardActionPlan.id).where(
                        CardActionPlan.id == plan_id,
                        CardActionPlan.owner_id == DEMO_USER_ID,
                    )
                )
                if plan_exists is None:
                    raise BankError("PLAN_NOT_FOUND", "未找到当前演示用户的卡片操作预览。")
                locked_card = None
            else:
                locked_card = self._get_card_for_update(session, card_id)

            plan = self._get_plan_for_update(session, plan_id)
            if plan is None or plan.owner_id != DEMO_USER_ID:
                raise BankError("PLAN_NOT_FOUND", "未找到当前演示用户的卡片操作预览。")

            if plan.status in {"completed", "declined", "expired"} and plan.outcome_status is not None:
                if plan.decision is not None and plan.decision != decision:
                    raise BankError("PLAN_ALREADY_DECIDED", "该卡片操作预览已完成决定，不能更改。")
                if plan.status == "expired" and plan.decision is None:
                    plan.decision = decision
                    plan.decided_at = now
                return _decision_result(plan)
            if plan.status == "invalidated":
                raise BankError("PLAN_INVALIDATED", "卡片操作信息已更新，旧预览已失效。")
            if plan.status != "pending":
                raise BankError("PLAN_STATE_INVALID", "卡片操作预览状态已变化，请重新发起。")
            if _as_utc(plan.expires_at) <= now:
                self._mark_expired(plan, now, decision=decision)
                return _decision_result(plan)

            if decision == "decline":
                plan.status = "declined"
                plan.decision = "decline"
                plan.outcome_status = "declined"
                plan.outcome_code = None
                plan.outcome_message = "已取消本次卡片操作，卡片和账户状态均未改变。"
                plan.decided_at = now
                return _decision_result(plan)

            if plan.action == "apply_card":
                if plan.card_id is not None or plan.card_kind not in {"debit", "credit"}:
                    raise BankError("PLAN_STATE_INVALID", "卡片申请预览信息无效，请重新发起。")
                operation_id = f"card-op-{uuid4()}"
                application = CardApplication(
                    id=f"card-application-{uuid4()}",
                    owner_id=DEMO_USER_ID,
                    card_kind=plan.card_kind,
                    status="submitted",
                    submitted_at=now,
                    operation_id=operation_id,
                )
                session.add(application)
                message = _application_confirmation_message(plan.card_kind)
            else:
                if (
                    plan.card_id is None
                    or locked_card is None
                    or locked_card.id != plan.card_id
                    or locked_card.owner_id != DEMO_USER_ID
                    or plan.card_version is None
                    or locked_card.version != plan.card_version
                    or not _before_matches_card(plan, locked_card)
                ):
                    self._decline_state_changed(plan, now)
                    return _decision_result(plan)
                message = self._apply_card_change(plan, locked_card)
                operation_id = f"card-op-{uuid4()}"
                locked_card.version += 1

            plan.status = "completed"
            plan.decision = "approve"
            plan.outcome_status = "completed"
            plan.outcome_code = None
            plan.outcome_message = message
            plan.operation_id = operation_id
            plan.decided_at = now
            return _decision_result(plan)

    def completed_events(self) -> list[dict[str, Any]]:
        """Rebuild stable completion events from committed card action rows."""

        with self._session_factory() as session:
            plans = session.scalars(
                select(CardActionPlan)
                .where(
                    CardActionPlan.owner_id == DEMO_USER_ID,
                    CardActionPlan.status == "completed",
                    CardActionPlan.outcome_status == "completed",
                    CardActionPlan.operation_id.is_not(None),
                )
                .order_by(CardActionPlan.decided_at, CardActionPlan.id)
            ).all()
            events: list[dict[str, Any]] = []
            for plan in plans:
                if plan.operation_id is None:
                    continue
                card = session.get(Card, plan.card_id) if plan.card_id else None
                application = session.scalar(
                    select(CardApplication).where(
                        CardApplication.owner_id == DEMO_USER_ID,
                        CardApplication.operation_id == plan.operation_id,
                    )
                )
                title = _event_title(plan, card)
                message = plan.outcome_message or "模拟卡片操作已完成。"
                receipt: dict[str, Any] = {
                    "status": "completed",
                    "kind": "card",
                    "operation_id": plan.operation_id,
                    "action": plan.action,
                    "card_id": plan.card_id,
                    "card_name": card.name if card is not None else None,
                    "card_kind": plan.card_kind,
                    "amount_minor": plan.amount_minor,
                    "before": plan.before_state,
                    "after": plan.after_state,
                    "message": message,
                }
                if application is not None:
                    receipt.update(
                        {
                            "application_id": application.id,
                            "application_status": application.status,
                        }
                    )
                events.append(
                    {
                        "id": f"card:{plan.operation_id}",
                        "plan_id": plan.id,
                        "conversation_id": plan.conversation_id,
                        "operation_id": plan.operation_id,
                        "title": title,
                        "text": message,
                        "kind": "card",
                        "source": "cards",
                        "created_at": _iso_utc(plan.decided_at or plan.created_at),
                        "receipt": receipt,
                    }
                )
            return events

    def authorize_charge(self, card_id: str, amount_minor: int) -> dict[str, Any]:
        """Read-only channel check; this method never changes card or account funds."""

        if not isinstance(card_id, str) or not card_id.strip():
            raise BankError("CARD_NOT_FOUND", "请提供有效的卡片标识。")
        _validate_positive_amount(amount_minor)
        with self._session_factory() as session:
            card = session.scalar(select(Card).where(Card.id == card_id, Card.owner_id == DEMO_USER_ID))
            if card is None:
                raise BankError("CARD_NOT_FOUND", "未找到当前演示用户的这张卡片。")

            blocked = {
                "frozen": ("CARD_FROZEN", "卡片已冻结，模拟刷卡渠道校验未通过；未发生扣款。"),
                "lost": ("CARD_LOST", "卡片已挂失，模拟刷卡渠道校验未通过；未发生扣款。"),
                "closed": ("CARD_CLOSED", "卡片已关闭，模拟刷卡渠道校验未通过；未发生扣款。"),
            }
            if card.status in blocked:
                code, message = blocked[card.status]
                return {"allowed": False, "code": code, "message": message}
            if amount_minor > card.daily_limit_minor:
                return {
                    "allowed": False,
                    "code": "DAILY_LIMIT_EXCEEDED",
                    "message": "金额超过该卡模拟单日消费限额；未发生扣款。",
                }
            if card.kind == "credit" and amount_minor > _available_credit(card):
                return {
                    "allowed": False,
                    "code": "INSUFFICIENT_CREDIT",
                    "message": "信用卡可用额度不足；模拟刷卡渠道校验未通过，未发生扣款。",
                }
            return {
                "allowed": True,
                "message": "模拟刷卡渠道校验通过；这不代表真实授权或已经扣款。",
            }

    def _get_card_for_update(self, session: Session, card_id: str) -> Card | None:
        query = select(Card).where(Card.id == card_id, Card.owner_id == DEMO_USER_ID)
        if self.dialect_name == "postgresql":
            query = query.with_for_update()
        return session.scalar(query)

    def _get_plan_for_update(self, session: Session, plan_id: str) -> CardActionPlan | None:
        query = select(CardActionPlan).where(
            CardActionPlan.id == plan_id,
            CardActionPlan.owner_id == DEMO_USER_ID,
        )
        if self.dialect_name == "postgresql":
            query = query.with_for_update()
        return session.scalar(query)

    def _plans_for_conversation(
        self,
        session: Session,
        conversation_id: str,
        *,
        pending_only: bool,
    ) -> list[CardActionPlan]:
        query = select(CardActionPlan).where(
            CardActionPlan.owner_id == DEMO_USER_ID,
            CardActionPlan.conversation_id == conversation_id,
        )
        if pending_only:
            query = query.where(CardActionPlan.status == "pending")
        query = query.order_by(CardActionPlan.created_at, CardActionPlan.id)
        if self.dialect_name == "postgresql":
            query = query.with_for_update()
        return list(session.scalars(query).all())

    def _invalidate_pending_in_session(
        self,
        session: Session,
        conversation_id: str,
        now: datetime,
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
            plan.outcome_message = "卡片操作信息已更新，旧预览已失效。"
            plan.decided_at = now
            count += 1
        return count

    @staticmethod
    def _mark_expired(
        plan: CardActionPlan,
        now: datetime,
        decision: str | None = None,
    ) -> None:
        plan.status = "expired"
        if decision is not None:
            plan.decision = decision
        plan.outcome_status = "declined"
        plan.outcome_code = "PLAN_EXPIRED"
        plan.outcome_message = "卡片操作预览已过期，卡片和账户状态均未改变。"
        plan.decided_at = now

    @staticmethod
    def _decline_state_changed(plan: CardActionPlan, now: datetime) -> None:
        plan.status = "declined"
        plan.decision = "approve"
        plan.outcome_status = "declined"
        plan.outcome_code = "CARD_STATE_CHANGED"
        plan.outcome_message = "卡片状态或额度版本已变化，本次操作未执行，请重新查询后再操作。"
        plan.decided_at = now

    @staticmethod
    def _apply_card_change(plan: CardActionPlan, card: Card) -> str:
        if plan.action == "freeze":
            card.status = "frozen"
            return f"已冻结{card.name}；冻结仅影响该卡的模拟刷卡渠道，不会冻结关联账户。"
        if plan.action == "unfreeze":
            card.status = "active"
            return f"已将{card.name}从冻结状态恢复为可用；账户余额没有变化。"
        if plan.action == "report_lost":
            card.status = "lost"
            return f"已挂失{card.name}；模拟刷卡渠道将拒绝后续校验，账户余额没有变化。"
        if plan.action == "set_daily_limit" and plan.amount_minor is not None:
            card.daily_limit_minor = plan.amount_minor
            return (
                f"已将{card.name}的模拟单日消费限额调整为{_format_cny(plan.amount_minor)}。"
                "该数值是演示规则，不代表真实银行政策。"
            )
        if plan.action == "set_credit_limit" and plan.amount_minor is not None:
            card.credit_limit_minor = plan.amount_minor
            return (
                f"已将{card.name}的模拟信用额度调整为{_format_cny(plan.amount_minor)}。"
                "该数值是演示规则，不代表银行授信或审批结果。"
            )
        raise BankError("PLAN_STATE_INVALID", "卡片操作预览中的操作类型或参数无效。")


def _validate_action_arguments(
    action: str,
    card_id: str | None,
    amount_minor: int | None,
    card_kind: str | None,
) -> None:
    if action == "apply_card":
        if (
            card_id is not None
            or amount_minor is not None
            or not isinstance(card_kind, str)
            or card_kind not in {"debit", "credit"}
        ):
            raise BankError(
                "INVALID_ACTION_ARGUMENTS", "申请时请选择借记卡或信用卡类型，不要提供卡片编号或金额。"
            )
        return
    if not isinstance(card_id, str) or not card_id.strip() or card_kind is not None:
        raise BankError("INVALID_ACTION_ARGUMENTS", "卡片操作需要有效卡片编号，且不能额外指定卡片类型。")
    if action in {"set_daily_limit", "set_credit_limit"}:
        _validate_positive_amount(amount_minor)
    elif amount_minor is not None:
        raise BankError("INVALID_ACTION_ARGUMENTS", "状态操作不需要提供金额。")


def _build_card_change(
    action: str,
    card: Card,
    amount_minor: int | None,
) -> tuple[dict[str, object], dict[str, object]]:
    if action == "freeze":
        if card.status == "frozen":
            raise BankError("CARD_ALREADY_FROZEN", "这张卡片已经冻结，无需重复操作。")
        if card.status != "active":
            raise BankError("CARD_STATE_INVALID", "只有正常状态的卡片可以冻结。")
        return {"status": card.status}, {"status": "frozen"}
    if action == "unfreeze":
        if card.status in {"lost", "closed"}:
            raise BankError("CARD_UNFREEZE_NOT_ALLOWED", "挂失或已关闭的卡片不能通过普通解锁恢复。")
        if card.status == "active":
            raise BankError("CARD_NOT_FROZEN", "这张卡片当前未冻结，无需解锁。")
        return {"status": card.status}, {"status": "active"}
    if action == "report_lost":
        if card.status == "lost":
            raise BankError("CARD_ALREADY_LOST", "这张卡片已经挂失，无需重复操作。")
        if card.status not in {"active", "frozen"}:
            raise BankError("CARD_STATE_INVALID", "已关闭的卡片不能再办理挂失。")
        return {"status": card.status}, {"status": "lost"}
    if action == "set_daily_limit":
        assert amount_minor is not None
        if card.daily_limit_minor == amount_minor:
            raise BankError("CARD_LIMIT_UNCHANGED", "新单日消费限额与当前数值相同，没有需要办理的变更。")
        if not DAILY_LIMIT_MIN_MINOR <= amount_minor <= DAILY_LIMIT_MAX_MINOR:
            raise BankError(
                "DAILY_LIMIT_OUT_OF_RANGE",
                "本演示规则允许的单日消费限额为100.00元至20,000.00元。",
            )
        return {"daily_limit_minor": card.daily_limit_minor}, {"daily_limit_minor": amount_minor}
    if action == "set_credit_limit":
        if card.kind != "credit":
            raise BankError("CARD_KIND_UNSUPPORTED", "信用额度调整仅适用于信用卡。")
        assert amount_minor is not None
        if card.credit_limit_minor == amount_minor:
            raise BankError("CARD_LIMIT_UNCHANGED", "新信用额度与当前数值相同，没有需要办理的变更。")
        if not CREDIT_LIMIT_MIN_MINOR <= amount_minor <= CREDIT_LIMIT_MAX_MINOR:
            raise BankError(
                "CREDIT_LIMIT_OUT_OF_RANGE",
                "本演示规则允许的信用额度为3,000.00元至50,000.00元。",
            )
        if amount_minor < card.outstanding_minor:
            raise BankError("CREDIT_LIMIT_BELOW_OUTSTANDING", "新信用额度不能低于当前已用额度。")
        return (
            {
                "credit_limit_minor": card.credit_limit_minor,
                "outstanding_minor": card.outstanding_minor,
                "available_credit_minor": _available_credit(card),
            },
            {
                "credit_limit_minor": amount_minor,
                "outstanding_minor": card.outstanding_minor,
                "available_credit_minor": amount_minor - card.outstanding_minor,
            },
        )
    raise BankError("INVALID_ACTION", "卡片操作类型无效。")


def _before_matches_card(plan: CardActionPlan, card: Card) -> bool:
    before = plan.before_state
    if plan.action in {"freeze", "unfreeze", "report_lost"}:
        return before.get("status") == card.status
    if plan.action == "set_daily_limit":
        return before.get("daily_limit_minor") == card.daily_limit_minor
    if plan.action == "set_credit_limit":
        return (
            before.get("credit_limit_minor") == card.credit_limit_minor
            and before.get("outstanding_minor") == card.outstanding_minor
        )
    return False


def _action_dict(plan: CardActionPlan, card: Card | None) -> dict[str, Any]:
    card_name = card.name if card is not None else None
    if plan.action == "apply_card":
        kind_name = _kind_name(plan.card_kind)
        title = f"申请{kind_name}"
        description = (
            f"确认后会提交一份模拟{kind_name}申请，申请状态为待审核；不会自动发卡，也不表示已经获批。"
        )
    elif plan.action == "freeze" and card is not None:
        title = f"冻结{card.name}"
        description = (
            f"确认后将冻结{card.name}。冻结仅影响该卡的模拟刷卡渠道，不会冻结关联账户或阻断账户转账。"
        )
    elif plan.action == "unfreeze" and card is not None:
        title = f"解锁{card.name}"
        description = f"确认后将{card.name}从冻结状态恢复为可用；不会改变账户余额。"
    elif plan.action == "report_lost" and card is not None:
        title = f"挂失{card.name}"
        description = f"确认后将挂失{card.name}，模拟刷卡渠道会拒绝后续校验；不会改变账户余额。"
    elif plan.action == "set_daily_limit" and card is not None:
        old_value = plan.before_state.get("daily_limit_minor")
        title = f"调整{card.name}单日限额"
        description = (
            f"确认后将单日消费限额从{_format_cny(int(old_value or 0))}调整为"
            f"{_format_cny(plan.amount_minor or 0)}。该数值是演示规则，不代表真实银行政策。"
        )
    elif plan.action == "set_credit_limit" and card is not None:
        old_value = plan.before_state.get("credit_limit_minor")
        title = f"调整{card.name}信用额度"
        description = (
            f"确认后将模拟信用额度从{_format_cny(int(old_value or 0))}调整为"
            f"{_format_cny(plan.amount_minor or 0)}。该数值不代表银行授信或审批结果。"
        )
    else:
        title = "卡片操作预览"
        description = "请重新查询卡片信息后发起操作。"
    result: dict[str, Any] = {
        "id": plan.id,
        "kind": "card",
        "action": plan.action,
        "title": title,
        "description": description,
        "status": plan.status,
        "expires_at": _iso_utc(plan.expires_at),
        "conversation_id": plan.conversation_id,
        "card_id": plan.card_id,
        "card_name": card_name,
        "card_kind": plan.card_kind,
        "amount_minor": plan.amount_minor,
        "before": plan.before_state,
        "after": plan.after_state,
    }
    return result


def _decision_result(plan: CardActionPlan) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": plan.outcome_status or "declined",
        "message": plan.outcome_message or "卡片操作未执行。",
    }
    if plan.operation_id is not None:
        result["operation_id"] = plan.operation_id
    if plan.outcome_code is not None:
        result["code"] = plan.outcome_code
    if plan.conversation_id is not None:
        result["conversation_id"] = plan.conversation_id
    return result


def _application_dict(application: CardApplication) -> dict[str, Any]:
    return {
        "id": application.id,
        "card_kind": application.card_kind,
        "status": application.status,
        "submitted_at": _iso_utc(application.submitted_at),
        "operation_id": application.operation_id,
    }


def _event_title(plan: CardActionPlan, card: Card | None) -> str:
    if plan.action == "apply_card":
        return f"{_kind_name(plan.card_kind)}申请已提交"
    name = card.name if card is not None else "卡片"
    return {
        "freeze": f"{name}已冻结",
        "unfreeze": f"{name}已解锁",
        "report_lost": f"{name}已挂失",
        "set_daily_limit": f"{name}单日消费限额已调整",
        "set_credit_limit": f"{name}信用额度已调整",
    }.get(plan.action, "卡片操作已完成")


def _application_confirmation_message(card_kind: str | None) -> str:
    return f"已提交模拟{_kind_name(card_kind)}申请，当前状态为待审核；不会自动发卡，也不表示已经获批。"


def _kind_name(card_kind: str | None) -> str:
    return {"debit": "借记卡", "credit": "信用卡"}.get(card_kind or "", "卡片")


def _card_dict(card: Card) -> dict[str, Any]:
    return {
        "id": card.id,
        "name": card.name,
        "kind": card.kind,
        "last4": card.last4,
        "account_id": card.account_id,
        "status": card.status,
        "daily_limit_minor": card.daily_limit_minor,
        "credit_limit_minor": card.credit_limit_minor,
        "outstanding_minor": card.outstanding_minor,
        "available_credit_minor": _available_credit(card) if card.kind == "credit" else None,
        "currency": card.currency,
        "version": card.version,
    }


def _available_credit(card: Card) -> int:
    return max(0, (card.credit_limit_minor or 0) - card.outstanding_minor)


def _validate_positive_amount(amount_minor: int | None) -> None:
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise BankError("INVALID_AMOUNT", "金额必须是整数分。")
    if amount_minor <= 0 or amount_minor > MAX_SAFE_JSON_INTEGER:
        raise BankError("INVALID_AMOUNT", "金额须为大于零的整数分。")


def _validate_conversation_id(conversation_id: str) -> None:
    if not isinstance(conversation_id, str) or not conversation_id.strip() or len(conversation_id) > 128:
        raise BankError("INVALID_CONVERSATION", "会话标识长度无效。")


def _format_cny(amount_minor: int) -> str:
    return f"{Decimal(amount_minor) / 100:,.2f}元"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")
