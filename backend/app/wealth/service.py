"""Deterministic product, suitability, order, and settlement service."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.bank.models import Account, LedgerTransaction, User
from app.bank.reservations import available_balance
from app.bank.service import MAX_SAFE_JSON_INTEGER, BankError

from .models import (
    Base,
    WealthActionPlan,
    WealthOrder,
    WealthPosition,
    WealthProduct,
    WealthRiskProfile,
    WealthSimulationClock,
)

DEMO_USER_ID = "demo-user-001"
DEMO_USER_NAME = "林小满"
CLEARING_OWNER_ID = "wealth-clearing-owner"
CLEARING_ACCOUNT_ID = "wealth-clearing-account"
DEFAULT_ACCOUNT_ID = "demo-checking-001"
WEALTH_RULE_VERSION = "wealth-rules-v1"
RISK_RULE_VERSION = "wealth-risk-v1"
PLAN_TTL = timedelta(minutes=10)
SHANGHAI = ZoneInfo("Asia/Shanghai")

_PROFILE_POINTS: dict[str, dict[str, int]] = {
    "horizon": {"short": 0, "medium": 1, "long": 2},
    "loss_tolerance": {"none": 0, "low": 1, "moderate": 3, "high": 5},
    "experience": {"none": 0, "basic": 1, "experienced": 2},
    "liquidity_need": {"high": 0, "medium": 1, "low": 2},
    "objective": {"preserve": 0, "balanced": 1, "growth": 2},
}
_PROFILE_KEYS = frozenset(_PROFILE_POINTS)

DEMO_PRODUCTS: tuple[dict[str, Any], ...] = (
    {
        "id": "demo-wealth-flex",
        "name": "灵活资金计划 R1",
        "risk_level": 1,
        "min_purchase_minor": 100_000,
        "purchase_fee_bps": 0,
        "redemption_fee_bps": 0,
        "lock_days": 0,
        "description": "模拟产品，固定模拟净值100分，仅用于流程演示，不代表收益或本金保障。",
    },
    {
        "id": "demo-wealth-balance",
        "name": "平衡180计划 R3",
        "risk_level": 3,
        "min_purchase_minor": 500_000,
        "purchase_fee_bps": 30,
        "redemption_fee_bps": 10,
        "lock_days": 180,
        "description": "模拟产品，固定模拟净值100分，锁定180个自然日；不代表收益或本金保障。",
    },
    {
        "id": "demo-wealth-growth",
        "name": "成长365计划 R5",
        "risk_level": 5,
        "min_purchase_minor": 1_000_000,
        "purchase_fee_bps": 50,
        "redemption_fee_bps": 20,
        "lock_days": 365,
        "description": "模拟产品，固定模拟净值100分，锁定365个自然日；不代表收益或本金保障。",
    },
)
_PRODUCT_BY_ID = {row["id"]: row for row in DEMO_PRODUCTS}


class WealthService:
    """Manage fictional wealth actions against the demo user's bank ledger.

    Pending orders never appear as positions. Purchase cash is debited when the
    user confirms; redemption shares are reserved at confirmation and cash is
    credited only by settlement. Every ledger and wealth write uses one session.
    """

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
        self.engine.dispose()

    def _begin_write(self, session: Session) -> None:
        # SQLite has no row-level FOR UPDATE. BEGIN IMMEDIATE serializes writers
        # before any balance or available-share value is read for a decision.
        if self.dialect_name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        with self._session_factory() as session, session.begin():
            self._begin_write(session)
            yield session

    def seed_demo(self) -> None:
        """Create fixed product rows, a private clearing account, and the clock.

        Existing user balances, risk answers, orders, positions, and simulation
        date are preserved, so startup and process restart do not reset state.
        """

        with self._transaction() as session:
            self._ensure_user(session, DEMO_USER_ID, DEMO_USER_NAME)
            self._ensure_user(session, CLEARING_OWNER_ID, "理财清算专用模拟所有者")
            session.flush()
            if session.get(Account, CLEARING_ACCOUNT_ID) is None:
                session.add(
                    Account(
                        id=CLEARING_ACCOUNT_ID,
                        owner_id=CLEARING_OWNER_ID,
                        name="理财清算专用模拟账户",
                        kind="wealth_clearing",
                        balance_minor=0,
                        currency="CNY",
                    )
                )

            for definition in DEMO_PRODUCTS:
                product = session.get(WealthProduct, definition["id"])
                values = {
                    **definition,
                    "settlement_business_days": 1,
                    "nav_minor": 100,
                    "currency": "CNY",
                    "rule_version": WEALTH_RULE_VERSION,
                }
                if product is None:
                    session.add(WealthProduct(**values))
                else:
                    for key, value in values.items():
                        setattr(product, key, value)

            clock = session.get(WealthSimulationClock, "demo")
            if clock is None:
                session.add(WealthSimulationClock(id="demo", current_date=datetime.now(SHANGHAI).date()))

    def list_products(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            profile = session.get(WealthRiskProfile, DEMO_USER_ID)
            products = session.scalars(select(WealthProduct).order_by(WealthProduct.id)).all()
            return [
                _product_dict(
                    row,
                    eligible=(
                        profile is not None
                        and profile.rule_version == RISK_RULE_VERSION
                        and row.risk_level <= profile.risk_level
                    ),
                )
                for row in products
            ]

    def get_risk_profile(self) -> dict[str, Any] | None:
        with self._session_factory() as session:
            profile = session.get(WealthRiskProfile, DEMO_USER_ID)
            return _profile_dict(profile) if profile is not None else None

    def set_risk_profile(self, answers: dict[str, Any]) -> dict[str, Any]:
        normalized, risk_level, explanation = _assess_answers(answers)
        now = _utcnow()
        with self._transaction() as session:
            self._lock_demo_user(session)
            profile = self._risk_profile_for_update(session)
            version = profile.version + 1 if profile is not None else 1
            if profile is None:
                profile = WealthRiskProfile(
                    owner_id=DEMO_USER_ID,
                    answers_json=json.dumps(normalized, ensure_ascii=False, sort_keys=True),
                    risk_level=risk_level,
                    risk_label=f"R{risk_level}",
                    rule_version=RISK_RULE_VERSION,
                    version=version,
                    assessed_at=now,
                    explanation=explanation,
                )
                session.add(profile)
            else:
                profile.answers_json = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
                profile.risk_level = risk_level
                profile.risk_label = f"R{risk_level}"
                profile.rule_version = RISK_RULE_VERSION
                profile.version = version
                profile.assessed_at = now
                profile.explanation = explanation
            session.flush()
            return _profile_dict(profile)

    def list_positions(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(WealthPosition)
                .where(
                    WealthPosition.owner_id == DEMO_USER_ID,
                    WealthPosition.principal_minor > 0,
                )
                .order_by(WealthPosition.settlement_date, WealthPosition.id)
            ).all()
            products = {
                row.id: row
                for row in session.scalars(
                    select(WealthProduct).where(
                        WealthProduct.id.in_({position.product_id for position in rows} or {""})
                    )
                ).all()
            }
            return [_position_dict(row, products[row.product_id]) for row in rows]

    def list_orders(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(WealthOrder)
                .where(WealthOrder.owner_id == DEMO_USER_ID)
                .order_by(WealthOrder.requested_on, WealthOrder.id)
            ).all()
            return [_order_dict(row) for row in rows]

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        if not order_id:
            return None
        with self._session_factory() as session:
            row = session.scalar(
                select(WealthOrder).where(
                    WealthOrder.id == order_id,
                    WealthOrder.owner_id == DEMO_USER_ID,
                )
            )
            return _order_dict(row) if row is not None else None

    def create_action_plan(
        self,
        action: str,
        conversation_id: str | None,
        product_id: str | None = None,
        account_id: str | None = None,
        position_id: str | None = None,
        amount_minor: int | None = None,
    ) -> dict[str, Any]:
        if action not in {"purchase", "redeem"}:
            raise BankError("INVALID_ACTION", "理财操作只能是 purchase 或 redeem。")
        if conversation_id is not None and len(conversation_id) > 128:
            raise BankError("INVALID_CONVERSATION", "会话标识长度无效。")
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
            raise BankError("INVALID_AMOUNT", "理财金额必须是整数分。")
        if amount_minor <= 0 or amount_minor > MAX_SAFE_JSON_INTEGER:
            raise BankError("INVALID_AMOUNT", "理财金额须为大于零的安全整数分。")
        if account_id is not None and (not isinstance(account_id, str) or not account_id):
            raise BankError("INVALID_ACCOUNT", "请提供有效的本人结算账户。")
        if action == "purchase" and not product_id:
            raise BankError("PRODUCT_NOT_FOUND", "申购前请先选择一个模拟产品。")
        if action == "redeem" and not position_id:
            raise BankError("POSITION_NOT_FOUND", "赎回前请先选择一笔实际持仓。")
        if action == "purchase" and position_id is not None:
            raise BankError("INVALID_ACTION", "申购计划不能指定赎回持仓。")

        now = _utcnow()
        plan_id = f"wealth-plan-{uuid4()}"
        resolved_account_id = account_id or DEFAULT_ACCOUNT_ID
        with self._transaction() as session:
            self._lock_demo_user(session)
            product = self._get_product_for_update(session, product_id) if product_id else None
            position: WealthPosition | None = None
            if action == "redeem":
                position = self._get_position_for_update(session, position_id or "")
                if position is None:
                    raise BankError("POSITION_NOT_FOUND", "未找到当前演示用户的该笔持仓。")
                if product is not None and product.id != position.product_id:
                    raise BankError("PRODUCT_MISMATCH", "所选产品与赎回持仓不一致。")
                product = session.get(WealthProduct, position.product_id)
            if product is None:
                raise BankError("PRODUCT_NOT_FOUND", "未找到该模拟理财产品。")
            if not _product_matches_contract(product):
                raise BankError("PRODUCT_RULE_CHANGED", "产品规则已变化，请重新查询产品并创建预览。")

            account = self._get_user_account_for_update(session, resolved_account_id)
            if account.currency != "CNY":
                raise BankError("CURRENCY_UNSUPPORTED", "理财模拟仅支持人民币账户。")
            clock = self._clock_for_update(session)
            if clock is None:
                raise BankError("WEALTH_NOT_SEEDED", "模拟理财尚未初始化。")
            requested_on = clock.current_date
            settles_on = _add_business_days(requested_on, 1)
            profile: WealthRiskProfile | None = None

            if action == "purchase":
                if amount_minor < product.min_purchase_minor:
                    raise BankError(
                        "BELOW_MINIMUM",
                        f"{product.name}起购金额为{_format_cny(product.min_purchase_minor)}。",
                    )
                profile = self._risk_profile_for_update(session)
                if profile is None:
                    raise BankError("RISK_PROFILE_REQUIRED", "请先完成全部五项风险评估，再预览申购。")
                if profile.rule_version != RISK_RULE_VERSION:
                    raise BankError("RISK_RULE_CHANGED", "风险评估规则已更新，请重新完成风险评估。")
                if profile.risk_level < product.risk_level:
                    raise BankError(
                        "RISK_PROFILE_INSUFFICIENT",
                        f"当前风险档案最高为{profile.risk_label}，不能申购{product.name}。",
                    )
                fee_minor = _fee_minor(amount_minor, product.purchase_fee_bps)
                total_minor = amount_minor + fee_minor
                if total_minor > MAX_SAFE_JSON_INTEGER:
                    raise BankError("INVALID_AMOUNT", "申购金额与费用超过安全整数范围。")
                if available_balance(session, account) < total_minor:
                    raise BankError("INSUFFICIENT_BALANCE", "账户余额不足，未创建申购预览。")
                payout_minor = 0
                risk_level = product.risk_level
                stored_position_id = None
                risk_version = profile.version
                risk_rule_version = profile.rule_version
            else:
                assert position is not None
                if position.product_id != product.id:
                    raise BankError("PRODUCT_MISMATCH", "赎回持仓所属产品不可用。")
                if requested_on < position.unlock_on:
                    raise BankError(
                        "LOCK_PERIOD_ACTIVE",
                        f"该笔持仓锁定至{position.unlock_on.isoformat()}，届时才能预览赎回。",
                    )
                if amount_minor > position.available_minor:
                    raise BankError("INSUFFICIENT_SHARES", "可赎回份额不足；待结算份额不能重复赎回。")
                fee_minor = _fee_minor(amount_minor, product.redemption_fee_bps)
                total_minor = amount_minor
                payout_minor = amount_minor - fee_minor
                risk_level = position.risk_level
                stored_position_id = position.id
                risk_version = None
                risk_rule_version = None

            if conversation_id:
                self._invalidate_pending_in_session(session, conversation_id, now)
            plan = WealthActionPlan(
                id=plan_id,
                owner_id=DEMO_USER_ID,
                action=action,
                product_id=product.id,
                product_rule_version=product.rule_version,
                risk_profile_rule_version=risk_rule_version,
                risk_profile_version=risk_version,
                risk_level=risk_level,
                account_id=account.id,
                position_id=stored_position_id,
                amount_minor=amount_minor,
                fee_minor=fee_minor,
                total_minor=total_minor,
                payout_minor=payout_minor,
                currency="CNY",
                requested_on=requested_on,
                settles_on=settles_on,
                status="pending",
                conversation_id=conversation_id,
                created_at=now,
                expires_at=now + PLAN_TTL,
            )
            session.add(plan)
            session.flush()
            return _plan_dict(plan, product)

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
            product = session.get(WealthProduct, plan.product_id)
            return _plan_dict(plan, product) if product is not None else None

    def pending_for_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        if not conversation_id:
            return None
        now = _utcnow()
        with self._transaction() as session:
            plans = self._plans_for_conversation(session, conversation_id)
            for plan in plans:
                if plan.status == "pending" and _as_utc(plan.expires_at) <= now:
                    self._mark_expired(plan, now)
            active = [plan for plan in plans if plan.status == "pending"]
            if not active:
                return None
            plan = max(active, key=lambda row: (row.created_at, row.id))
            product = session.get(WealthProduct, plan.product_id)
            return _plan_dict(plan, product) if product is not None else None

    def invalidate_pending_plans(self, conversation_id: str) -> int:
        if not conversation_id:
            return 0
        now = _utcnow()
        with self._transaction() as session:
            self._lock_demo_user(session)
            return self._invalidate_pending_in_session(session, conversation_id, now)

    def decide_plan(
        self,
        plan_id: str,
        decision: str,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        if decision not in {"approve", "decline"}:
            raise BankError("INVALID_DECISION", "理财操作只能确认 approve 或拒绝 decline。")
        now = _utcnow()
        with self._transaction() as session:
            self._lock_demo_user(session)
            plan = self._get_plan_for_update(session, plan_id)
            if plan is None:
                raise BankError("PLAN_NOT_FOUND", "未找到当前演示用户的理财预览。")
            if conversation_id is not None and plan.conversation_id != conversation_id:
                raise BankError("CONVERSATION_MISMATCH", "该理财预览属于另一条会话。")

            if plan.status in {"completed", "declined", "expired"} and plan.outcome_status is not None:
                if plan.decision is not None and plan.decision != decision:
                    raise BankError("PLAN_ALREADY_DECIDED", "该理财预览已完成决定，不能更改。")
                if plan.status == "expired" and plan.decision is None:
                    plan.decision = decision
                    plan.decided_at = now
                return self._decision_result(session, plan)
            if plan.status == "invalidated":
                raise BankError("PLAN_INVALIDATED", "理财信息已更新，旧预览已失效。")
            if plan.status != "pending":
                raise BankError("PLAN_STATE_INVALID", "理财预览状态已变化，请重新发起。")
            if _as_utc(plan.expires_at) <= now:
                self._mark_expired(plan, now, decision=decision)
                return self._decision_result(session, plan)
            if decision == "decline":
                plan.status = "declined"
                plan.decision = "decline"
                plan.outcome_status = "declined"
                plan.outcome_code = None
                plan.outcome_message = "已取消本次理财操作；账户资金、持仓和份额均未变化。"
                plan.decided_at = now
                return self._decision_result(session, plan)

            clock = self._clock_for_update(session)
            if clock is None or clock.current_date != plan.requested_on:
                self._decline_plan(
                    plan,
                    now,
                    "PLAN_DATE_CHANGED",
                    "模拟业务日已变化，请重新查询并创建理财预览。",
                )
                return self._decision_result(session, plan)

            product = self._get_product_for_update(session, plan.product_id)
            if (
                product is None
                or not _product_matches_contract(product)
                or product.rule_version != plan.product_rule_version
                or product.risk_level != plan.risk_level
            ):
                self._decline_plan(
                    plan,
                    now,
                    "PRODUCT_RULE_CHANGED",
                    "产品规则或版本已变化，未执行；请重新查询产品并创建预览。",
                )
                return self._decision_result(session, plan)

            if plan.action == "purchase":
                profile = self._risk_profile_for_update(session)
                if (
                    profile is None
                    or profile.version != plan.risk_profile_version
                    or profile.rule_version != plan.risk_profile_rule_version
                    or profile.rule_version != RISK_RULE_VERSION
                ):
                    self._decline_plan(
                        plan,
                        now,
                        "RISK_PROFILE_CHANGED",
                        "风险档案在预览后发生变化或已失效，未执行；请重新评估并创建预览。",
                    )
                    return self._decision_result(session, plan)
                if profile.risk_level < product.risk_level:
                    self._decline_plan(
                        plan,
                        now,
                        "RISK_PROFILE_INSUFFICIENT",
                        f"当前风险档案为{profile.risk_label}，不满足{product.name}的适当性要求。",
                    )
                    return self._decision_result(session, plan)
                expected_fee = _fee_minor(plan.amount_minor, product.purchase_fee_bps)
                if (
                    plan.amount_minor < product.min_purchase_minor
                    or plan.fee_minor != expected_fee
                    or plan.total_minor != plan.amount_minor + expected_fee
                    or plan.payout_minor != 0
                ):
                    self._decline_plan(
                        plan, now, "PLAN_RULE_MISMATCH", "申购金额或费用与当前产品规则不一致，未执行。"
                    )
                    return self._decision_result(session, plan)
            elif plan.action == "redeem":
                if (
                    plan.fee_minor != _fee_minor(plan.amount_minor, product.redemption_fee_bps)
                    or plan.total_minor != plan.amount_minor
                    or plan.payout_minor != plan.amount_minor - plan.fee_minor
                ):
                    self._decline_plan(
                        plan, now, "PLAN_RULE_MISMATCH", "赎回金额或费用与当前产品规则不一致，未执行。"
                    )
                    return self._decision_result(session, plan)
            else:
                raise BankError("PLAN_STATE_INVALID", "理财预览中的操作类型无效。")

            accounts = self._get_accounts_for_update(session, {plan.account_id, CLEARING_ACCOUNT_ID})
            account = accounts.get(plan.account_id)
            clearing = accounts.get(CLEARING_ACCOUNT_ID)
            if account is None or account.owner_id != DEMO_USER_ID or account.currency != plan.currency:
                self._decline_plan(plan, now, "ACCOUNT_NOT_FOUND", "未找到有效的本人人民币结算账户。")
                return self._decision_result(session, plan)
            if (
                clearing is None
                or clearing.owner_id != CLEARING_OWNER_ID
                or clearing.currency != plan.currency
            ):
                raise BankError("CLEARING_ACCOUNT_UNAVAILABLE", "模拟理财清算账户不可用，操作已回滚。")

            if plan.action == "purchase":
                if available_balance(session, account) < plan.total_minor:
                    self._decline_plan(
                        plan, now, "INSUFFICIENT_BALANCE", "账户余额已变化且不足，申购未执行。"
                    )
                    return self._decision_result(session, plan)
                account.balance_minor -= plan.total_minor
                clearing.balance_minor += plan.total_minor
            else:
                position = self._get_position_for_update(session, plan.position_id or "")
                if position is None:
                    self._decline_plan(plan, now, "POSITION_NOT_FOUND", "该笔持仓已不可用，赎回未执行。")
                    return self._decision_result(session, plan)
                if position.product_id != product.id or position.unlock_on > clock.current_date:
                    self._decline_plan(plan, now, "LOCK_PERIOD_ACTIVE", "该笔持仓仍在锁定期，赎回未执行。")
                    return self._decision_result(session, plan)
                if position.available_minor < plan.amount_minor:
                    self._decline_plan(plan, now, "INSUFFICIENT_SHARES", "可用份额已变化且不足，赎回未执行。")
                    return self._decision_result(session, plan)
                if account.balance_minor < 0 or plan.payout_minor > MAX_SAFE_JSON_INTEGER:
                    self._decline_plan(plan, now, "PLAN_RULE_MISMATCH", "赎回金额不符合模拟规则。")
                    return self._decision_result(session, plan)
                position.available_minor -= plan.amount_minor
                position.redemption_pending_minor += plan.amount_minor

            operation_id = f"wealth-op-{uuid4()}"
            order_id = f"wealth-order-{uuid4()}"
            order = WealthOrder(
                id=order_id,
                owner_id=DEMO_USER_ID,
                plan_id=plan.id,
                action=plan.action,
                product_id=product.id,
                position_id=plan.position_id,
                account_id=account.id,
                amount_minor=plan.amount_minor,
                fee_minor=plan.fee_minor,
                total_minor=plan.total_minor,
                payout_minor=plan.payout_minor,
                status="pending_settlement",
                lock_days=product.lock_days,
                risk_level=plan.risk_level,
                requested_on=plan.requested_on,
                settles_on=plan.settles_on,
                operation_id=operation_id,
                conversation_id=plan.conversation_id,
            )
            session.add(order)
            if plan.action == "purchase":
                self._write_ledger_pair(
                    session,
                    debit_account=account,
                    credit_account=clearing,
                    debit_minor=plan.amount_minor,
                    credit_minor=plan.amount_minor,
                    operation_id=operation_id,
                    leg="principal",
                    debit_description=f"理财申购本金·{product.name}",
                    credit_description=f"理财申购本金·{product.name}",
                    created_at=now,
                )
                if plan.fee_minor:
                    self._write_ledger_pair(
                        session,
                        debit_account=account,
                        credit_account=clearing,
                        debit_minor=plan.fee_minor,
                        credit_minor=plan.fee_minor,
                        operation_id=operation_id,
                        leg="fee",
                        debit_description=f"理财申购手续费·{product.name}",
                        credit_description=f"理财申购手续费·{product.name}",
                        created_at=now,
                    )
                message = _accepted_message(plan.action, product.name, plan.settles_on)
            else:
                message = _accepted_message(plan.action, product.name, plan.settles_on)
            plan.status = "completed"
            plan.decision = "approve"
            plan.outcome_status = "completed"
            plan.outcome_code = None
            plan.outcome_message = message
            plan.outcome_order_id = order.id
            plan.operation_id = operation_id
            plan.decided_at = now
            session.flush()
            return self._decision_result(session, plan)

    def current_date(self) -> str:
        with self._session_factory() as session:
            clock = session.get(WealthSimulationClock, "demo")
            if clock is None:
                raise BankError("WEALTH_NOT_SEEDED", "模拟理财尚未初始化。")
            return clock.current_date.isoformat()

    def process_due(self, as_of: date | datetime | str | None = None) -> list[dict[str, Any]]:
        effective_date = _as_business_date(as_of) if as_of is not None else None
        with self._transaction() as session:
            self._lock_demo_user(session)
            if effective_date is None:
                clock = self._clock_for_update(session)
                if clock is None:
                    raise BankError("WEALTH_NOT_SEEDED", "模拟理财尚未初始化。")
                effective_date = clock.current_date
            return self._process_due_in_session(session, effective_date)

    def advance_demo_clock(self, business_days: int) -> dict[str, Any]:
        if isinstance(business_days, bool) or not isinstance(business_days, int):
            raise BankError("INVALID_BUSINESS_DAYS", "业务日推进天数必须是整数。")
        if not 1 <= business_days <= 365:
            raise BankError("INVALID_BUSINESS_DAYS", "业务日推进范围为1至365天。")
        with self._transaction() as session:
            self._lock_demo_user(session)
            clock = self._clock_for_update(session)
            if clock is None:
                raise BankError("WEALTH_NOT_SEEDED", "模拟理财尚未初始化。")
            current = clock.current_date
            for _ in range(business_days):
                current = _add_business_days(current, 1)
            clock.current_date = current
            settled_orders = self._process_due_in_session(session, current)
            return {"as_of": current.isoformat(), "settled_orders": settled_orders}

    def completed_events(self) -> list[dict[str, Any]]:
        """Rebuild stable acceptance and settlement events from durable rows."""

        with self._session_factory() as session:
            accepted_plans = session.scalars(
                select(WealthActionPlan)
                .where(
                    WealthActionPlan.owner_id == DEMO_USER_ID,
                    WealthActionPlan.status == "completed",
                    WealthActionPlan.operation_id.is_not(None),
                    WealthActionPlan.outcome_order_id.is_not(None),
                )
                .order_by(WealthActionPlan.decided_at, WealthActionPlan.id)
            ).all()
            events: list[dict[str, Any]] = []
            for plan in accepted_plans:
                product = session.get(WealthProduct, plan.product_id)
                order = session.get(WealthOrder, plan.outcome_order_id)
                if product is None or order is None or plan.operation_id is None:
                    continue
                acceptance_message = plan.outcome_message or "理财订单已受理。"
                accepted_receipt = {
                    "status": "completed",
                    "kind": "wealth",
                    "operation_id": plan.operation_id,
                    "order_id": order.id,
                    "order_status": "pending_settlement",
                    "message": acceptance_message,
                    "conversation_id": plan.conversation_id,
                }
                events.append(
                    {
                        "id": f"wealth:{plan.operation_id}",
                        "kind": "wealth",
                        "source": "wealth",
                        "title": "理财操作已受理",
                        "text": acceptance_message,
                        "created_at": _iso_utc(plan.decided_at or plan.created_at),
                        "plan_id": plan.id,
                        "conversation_id": plan.conversation_id,
                        "operation_id": plan.operation_id,
                        "receipt": accepted_receipt,
                    }
                )
                if order.status == "settled" and order.settled_at is not None:
                    settled_message = _settled_message(order, product)
                    settled_receipt = {
                        "status": "completed",
                        "kind": "wealth",
                        "operation_id": order.operation_id,
                        "order_id": order.id,
                        "order_status": "settled",
                        "message": settled_message,
                        "conversation_id": order.conversation_id,
                    }
                    events.append(
                        {
                            "id": f"wealth-settlement:{order.id}",
                            "kind": "wealth",
                            "source": "wealth",
                            "title": "理财订单已结算",
                            "text": settled_message,
                            "created_at": _iso_utc(order.settled_at),
                            "plan_id": plan.id,
                            "conversation_id": order.conversation_id,
                            "operation_id": order.operation_id,
                            "receipt": settled_receipt,
                        }
                    )
            events.sort(key=lambda row: (row["created_at"], row["id"]))
            return events

    def _process_due_in_session(self, session: Session, as_of: date) -> list[dict[str, Any]]:
        query = select(WealthOrder).where(
            WealthOrder.owner_id == DEMO_USER_ID,
            WealthOrder.status == "pending_settlement",
            WealthOrder.settles_on <= as_of,
        )
        if self.dialect_name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        due = session.scalars(query.order_by(WealthOrder.settles_on, WealthOrder.id)).all()
        settled: list[dict[str, Any]] = []
        for order in due:
            if order.status != "pending_settlement":
                continue
            product = session.get(WealthProduct, order.product_id)
            if product is None:
                raise BankError("PRODUCT_NOT_FOUND", "待结算订单对应产品不存在；结算已回滚。")
            if order.action == "purchase":
                position_id = f"wealth-position-{order.id}"
                existing = session.get(WealthPosition, position_id)
                if existing is None:
                    position = WealthPosition(
                        id=position_id,
                        owner_id=DEMO_USER_ID,
                        product_id=order.product_id,
                        principal_minor=order.amount_minor,
                        available_minor=order.amount_minor,
                        redemption_pending_minor=0,
                        currency="CNY",
                        settlement_date=as_of,
                        unlock_on=as_of + timedelta(days=order.lock_days),
                        risk_level=order.risk_level,
                        source_order_id=order.id,
                    )
                    session.add(position)
                    session.flush()
                order.position_id = position_id
            elif order.action == "redeem":
                position = self._get_position_for_update(session, order.position_id or "")
                if position is None or position.redemption_pending_minor < order.amount_minor:
                    raise BankError("SETTLEMENT_SHARES_UNAVAILABLE", "赎回预占份额不完整；结算已回滚。")
                ids = {order.account_id, CLEARING_ACCOUNT_ID}
                accounts = self._get_accounts_for_update(session, ids)
                account = accounts.get(order.account_id)
                clearing = accounts.get(CLEARING_ACCOUNT_ID)
                if account is None or account.owner_id != DEMO_USER_ID or account.currency != "CNY":
                    raise BankError("ACCOUNT_NOT_FOUND", "赎回到账账户不可用；结算已回滚。")
                if clearing is None or clearing.owner_id != CLEARING_OWNER_ID or clearing.currency != "CNY":
                    raise BankError("CLEARING_ACCOUNT_UNAVAILABLE", "模拟清算账户不可用；结算已回滚。")
                if clearing.balance_minor < order.payout_minor:
                    raise BankError("SETTLEMENT_FUNDS_UNAVAILABLE", "模拟清算余额不足；赎回结算已回滚。")
                clearing.balance_minor -= order.payout_minor
                account.balance_minor += order.payout_minor
                position.redemption_pending_minor -= order.amount_minor
                position.principal_minor -= order.amount_minor
                self._write_ledger_pair(
                    session,
                    debit_account=clearing,
                    credit_account=account,
                    debit_minor=order.amount_minor,
                    credit_minor=order.amount_minor,
                    operation_id=order.operation_id,
                    leg="principal",
                    debit_description=f"理财赎回本金·{product.name}",
                    credit_description=f"理财赎回本金·{product.name}",
                    created_at=_utcnow(),
                )
                if order.fee_minor:
                    self._write_ledger_pair(
                        session,
                        debit_account=account,
                        credit_account=clearing,
                        debit_minor=order.fee_minor,
                        credit_minor=order.fee_minor,
                        operation_id=order.operation_id,
                        leg="fee",
                        debit_description=f"理财赎回手续费·{product.name}",
                        credit_description=f"理财赎回手续费·{product.name}",
                        created_at=_utcnow(),
                    )
            else:
                raise BankError("ORDER_STATE_INVALID", "待结算订单类型无效；结算已回滚。")

            order.status = "settled"
            order.settled_at = _utcnow()
            session.flush()
            settled.append(_order_dict(order))
        return settled

    def _lock_demo_user(self, session: Session) -> User:
        query = select(User).where(User.id == DEMO_USER_ID)
        if self.dialect_name == "postgresql":
            query = query.with_for_update()
        user = session.scalar(query)
        if user is None:
            raise BankError("BANK_NOT_SEEDED", "演示用户尚未初始化，请先运行理财演示数据初始化。")
        return user

    def _risk_profile_for_update(self, session: Session) -> WealthRiskProfile | None:
        query = select(WealthRiskProfile).where(WealthRiskProfile.owner_id == DEMO_USER_ID)
        if self.dialect_name == "postgresql":
            query = query.with_for_update()
        return session.scalar(query)

    def _clock_for_update(self, session: Session) -> WealthSimulationClock | None:
        query = select(WealthSimulationClock).where(WealthSimulationClock.id == "demo")
        if self.dialect_name == "postgresql":
            query = query.with_for_update()
        return session.scalar(query)

    def _get_product_for_update(self, session: Session, product_id: str | None) -> WealthProduct | None:
        if not product_id:
            return None
        query = select(WealthProduct).where(WealthProduct.id == product_id)
        if self.dialect_name == "postgresql":
            query = query.with_for_update()
        return session.scalar(query)

    def _get_plan_for_update(self, session: Session, plan_id: str) -> WealthActionPlan | None:
        query = select(WealthActionPlan).where(
            WealthActionPlan.id == plan_id,
            WealthActionPlan.owner_id == DEMO_USER_ID,
        )
        if self.dialect_name == "postgresql":
            query = query.with_for_update()
        return session.scalar(query)

    def _get_position_for_update(self, session: Session, position_id: str) -> WealthPosition | None:
        query = select(WealthPosition).where(
            WealthPosition.id == position_id,
            WealthPosition.owner_id == DEMO_USER_ID,
        )
        if self.dialect_name == "postgresql":
            query = query.with_for_update()
        return session.scalar(query)

    def _get_user_account_for_update(self, session: Session, account_id: str) -> Account:
        query = select(Account).where(Account.id == account_id, Account.owner_id == DEMO_USER_ID)
        if self.dialect_name == "postgresql":
            query = query.with_for_update()
        account = session.scalar(query)
        if account is None:
            raise BankError("ACCOUNT_NOT_FOUND", "未找到当前演示用户的结算账户。")
        return account

    def _get_accounts_for_update(self, session: Session, account_ids: set[str]) -> dict[str, Account]:
        query = select(Account).where(Account.id.in_(account_ids)).order_by(Account.id)
        if self.dialect_name == "postgresql":
            query = query.with_for_update()
        return {row.id: row for row in session.scalars(query).all()}

    def _plans_for_conversation(self, session: Session, conversation_id: str) -> list[WealthActionPlan]:
        query = select(WealthActionPlan).where(
            WealthActionPlan.owner_id == DEMO_USER_ID,
            WealthActionPlan.conversation_id == conversation_id,
            WealthActionPlan.status == "pending",
        )
        if self.dialect_name == "postgresql":
            query = query.with_for_update()
        return session.scalars(query.order_by(WealthActionPlan.created_at, WealthActionPlan.id)).all()

    @staticmethod
    def _invalidate_pending_in_session(session: Session, conversation_id: str, now: datetime) -> int:
        query = select(WealthActionPlan).where(
            WealthActionPlan.owner_id == DEMO_USER_ID,
            WealthActionPlan.conversation_id == conversation_id,
            WealthActionPlan.status == "pending",
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            query = query.with_for_update()
        plans = session.scalars(query).all()
        for plan in plans:
            plan.status = "invalidated"
            plan.outcome_status = "declined"
            plan.outcome_message = "理财信息已更新，旧预览已失效；账户资金和持仓未变化。"
            plan.decided_at = now
        return len(plans)

    def _decision_result(self, session: Session, plan: WealthActionPlan) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": plan.outcome_status or "declined",
            "message": plan.outcome_message or "理财预览已处理。",
        }
        if plan.operation_id is not None:
            result["operation_id"] = plan.operation_id
        if plan.outcome_code is not None:
            result["code"] = plan.outcome_code
        if plan.conversation_id is not None:
            result["conversation_id"] = plan.conversation_id
        if plan.outcome_order_id is not None:
            order = session.get(WealthOrder, plan.outcome_order_id)
            if order is not None:
                result["order_id"] = order.id
                result["order_status"] = order.status
                product = session.get(WealthProduct, order.product_id)
                if order.status == "settled" and product is not None:
                    result["message"] = _settled_message(order, product)
                elif order.status == "pending_settlement":
                    result["message"] = _accepted_message(
                        order.action, product.name if product else "理财产品", order.settles_on
                    )
        return result

    @staticmethod
    def _decline_plan(plan: WealthActionPlan, now: datetime, code: str, message: str) -> None:
        plan.status = "declined"
        plan.decision = "approve"
        plan.outcome_status = "declined"
        plan.outcome_code = code
        plan.outcome_message = message
        plan.decided_at = now

    @staticmethod
    def _mark_expired(plan: WealthActionPlan, now: datetime, decision: str | None = None) -> None:
        plan.status = "expired"
        plan.outcome_status = "declined"
        plan.outcome_code = "PLAN_EXPIRED"
        plan.outcome_message = "理财预览已过期，未发生资金或持仓变化。"
        plan.decision = decision
        plan.decided_at = now

    @staticmethod
    def _ensure_user(session: Session, user_id: str, name: str) -> None:
        if session.get(User, user_id) is None:
            session.add(User(id=user_id, name=name))

    @staticmethod
    def _write_ledger_pair(
        session: Session,
        *,
        debit_account: Account,
        credit_account: Account,
        debit_minor: int,
        credit_minor: int,
        operation_id: str,
        leg: str,
        debit_description: str,
        credit_description: str,
        created_at: datetime,
    ) -> None:
        if debit_minor <= 0 or debit_minor != credit_minor:
            raise BankError("LEDGER_AMOUNT_MISMATCH", "模拟理财账本借贷金额不平，操作已回滚。")
        session.add_all(
            [
                LedgerTransaction(
                    id=f"{operation_id}-{leg}-debit",
                    account_id=debit_account.id,
                    description=debit_description,
                    amount_minor=-debit_minor,
                    currency="CNY",
                    created_at=created_at,
                    operation_id=operation_id,
                ),
                LedgerTransaction(
                    id=f"{operation_id}-{leg}-credit",
                    account_id=credit_account.id,
                    description=credit_description,
                    amount_minor=credit_minor,
                    currency="CNY",
                    created_at=created_at,
                    operation_id=operation_id,
                ),
            ]
        )


def _product_dict(product: WealthProduct, *, eligible: bool) -> dict[str, Any]:
    return {
        "id": product.id,
        "name": product.name,
        "risk_level": product.risk_level,
        "min_purchase_minor": product.min_purchase_minor,
        "purchase_fee_bps": product.purchase_fee_bps,
        "redemption_fee_bps": product.redemption_fee_bps,
        "lock_days": product.lock_days,
        "settlement_business_days": product.settlement_business_days,
        "nav_minor": product.nav_minor,
        "currency": product.currency,
        "rule_version": product.rule_version,
        "description": product.description,
        "eligible": eligible,
    }


def _profile_dict(profile: WealthRiskProfile) -> dict[str, Any]:
    return {
        "answers": json.loads(profile.answers_json),
        "risk_level": profile.risk_level,
        "risk_label": profile.risk_label,
        "rule_version": profile.rule_version,
        "assessed_at": _iso_utc(profile.assessed_at),
        "explanation": profile.explanation,
    }


def _position_dict(position: WealthPosition, product: WealthProduct) -> dict[str, Any]:
    return {
        "id": position.id,
        "product_id": product.id,
        "product_name": product.name,
        "principal_minor": position.principal_minor,
        "available_minor": position.available_minor,
        "redemption_pending_minor": position.redemption_pending_minor,
        "currency": position.currency,
        "unlock_on": position.unlock_on.isoformat(),
        "risk_level": position.risk_level,
    }


def _order_dict(order: WealthOrder) -> dict[str, Any]:
    return {
        "id": order.id,
        "action": order.action,
        "product_id": order.product_id,
        "position_id": order.position_id,
        "amount_minor": order.amount_minor,
        "fee_minor": order.fee_minor,
        "total_minor": order.total_minor,
        "payout_minor": order.payout_minor,
        "status": order.status,
        "requested_on": order.requested_on.isoformat(),
        "settles_on": order.settles_on.isoformat(),
        "settled_at": _iso_utc(order.settled_at) if order.settled_at is not None else None,
        "operation_id": order.operation_id,
        "conversation_id": order.conversation_id,
    }


def _plan_dict(plan: WealthActionPlan, product: WealthProduct) -> dict[str, Any]:
    if plan.action == "purchase":
        title = f"申购{product.name}"
        description = (
            f"申购本金{_format_cny(plan.amount_minor)}，费用{_format_cny(plan.fee_minor)}，"
            f"确认后扣款{_format_cny(plan.total_minor)}；预计{plan.settles_on.isoformat()}结算，"
            "结算前不计入持仓。"
        )
    else:
        title = f"赎回{product.name}"
        description = (
            f"赎回份额本金{_format_cny(plan.amount_minor)}，费用{_format_cny(plan.fee_minor)}，"
            f"预计到账{_format_cny(plan.payout_minor)}；确认后预占份额，"
            f"预计{plan.settles_on.isoformat()}结算到账。"
        )
    return {
        "id": plan.id,
        "kind": "wealth",
        "action": plan.action,
        "title": title,
        "description": description,
        "status": plan.status,
        "expires_at": _iso_utc(plan.expires_at),
        "conversation_id": plan.conversation_id,
        "product_id": product.id,
        "product_name": product.name,
        "account_id": plan.account_id,
        "position_id": plan.position_id,
        "amount_minor": plan.amount_minor,
        "fee_minor": plan.fee_minor,
        "total_minor": plan.total_minor,
        "payout_minor": plan.payout_minor,
        "settles_on": plan.settles_on.isoformat(),
        "risk_level": plan.risk_level,
        "rule_version": plan.product_rule_version,
    }


def _assess_answers(answers: dict[str, Any]) -> tuple[dict[str, str], int, str]:
    if not isinstance(answers, dict):
        raise BankError("INVALID_RISK_PROFILE", "风险评估必须包含全部五项答案。")
    missing = _PROFILE_KEYS - set(answers)
    unknown = set(answers) - _PROFILE_KEYS
    if missing or unknown:
        raise BankError(
            "INVALID_RISK_PROFILE",
            "风险评估必须且只能填写horizon、loss_tolerance、experience、liquidity_need、objective五项。",
        )
    normalized: dict[str, str] = {}
    for key, points in _PROFILE_POINTS.items():
        value = answers[key]
        if not isinstance(value, str) or value not in points:
            raise BankError("INVALID_RISK_PROFILE", f"风险评估字段{key}的答案无效。")
        normalized[key] = value
    score = sum(_PROFILE_POINTS[key][value] for key, value in normalized.items())
    if score <= 2:
        risk_level = 1
    elif score <= 5:
        risk_level = 2
    elif score <= 8:
        risk_level = 3
    elif score <= 11:
        risk_level = 4
    else:
        risk_level = 5
    if normalized["loss_tolerance"] == "none":
        risk_level = 1
    if normalized["experience"] == "none":
        risk_level = min(risk_level, 2)
    if normalized["horizon"] == "short" and normalized["liquidity_need"] == "high":
        risk_level = 1
    explanation = (
        f"根据五项已填写答案按{RISK_RULE_VERSION}计算，原始分数为{score}，"
        f"并应用亏损承受、经验和短期高流动性限制，模拟风险档案为R{risk_level}。"
        "该档案只用于模拟产品适当性限制，不构成投资建议或收益预测。"
    )
    return normalized, risk_level, explanation


def _product_matches_contract(product: WealthProduct) -> bool:
    fixed = _PRODUCT_BY_ID.get(product.id)
    return bool(
        fixed
        and product.name == fixed["name"]
        and product.risk_level == fixed["risk_level"]
        and product.min_purchase_minor == fixed["min_purchase_minor"]
        and product.purchase_fee_bps == fixed["purchase_fee_bps"]
        and product.redemption_fee_bps == fixed["redemption_fee_bps"]
        and product.lock_days == fixed["lock_days"]
        and product.settlement_business_days == 1
        and product.nav_minor == 100
        and product.currency == "CNY"
        and product.rule_version == WEALTH_RULE_VERSION
        and product.description == fixed["description"]
    )


def _fee_minor(amount_minor: int, basis_points: int) -> int:
    # Positive half-up rounding without floating-point arithmetic.
    return (amount_minor * basis_points + 5_000) // 10_000


def _add_business_days(start: date, days: int) -> date:
    result = start
    added = 0
    while added < days:
        result += timedelta(days=1)
        if result.weekday() < 5:
            added += 1
    return result


def _as_business_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.astimezone(SHANGHAI).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise BankError("INVALID_DATE", "业务日期必须是有效的YYYY-MM-DD日期。") from error


def _format_cny(amount_minor: int) -> str:
    return f"¥{amount_minor // 100:,}.{amount_minor % 100:02d}"


def _accepted_message(action: str, product_name: str, settles_on: date) -> str:
    if action == "purchase":
        return (
            f"{product_name}申购已受理，预计于{settles_on.isoformat()}业务日结算；"
            "结算前不计入持仓，模拟产品固定净值不代表收益。"
        )
    return (
        f"{product_name}赎回已受理，份额已预占，预计于{settles_on.isoformat()}业务日结算；"
        "结算前赎回款尚未到账。"
    )


def _settled_message(order: WealthOrder, product: WealthProduct) -> str:
    if order.action == "purchase":
        return (
            f"{product.name}申购订单已结算，{_format_cny(order.amount_minor)}模拟持仓已入账；"
            "按固定模拟净值记份额，不代表收益或本金保障。"
        )
    return f"{product.name}赎回订单已结算，{_format_cny(order.payout_minor)}赎回款已到账；对应份额已扣减。"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return _as_utc(value).isoformat()
