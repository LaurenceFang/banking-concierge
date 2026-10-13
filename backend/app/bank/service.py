"""Service layer for the fixed-profile synthetic banking ledger."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .errors import BankError
from .models import Account, Base, LedgerTransaction, Recipient, TransferPlan, User
from .reservations import available_balance, reserved_balance

DEMO_USER_ID = "demo-user-001"
DEMO_USER_NAME = "林小满"
MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991
TRANSFER_PLAN_TTL = timedelta(minutes=10)


class BankService:
    """Read and update the demo user's accounts through a SQLAlchemy ledger.

    The user identity is deliberately fixed by the server. Methods accept account,
    recipient, and plan identifiers, but never a caller-provided owner identifier.
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

    def dispose(self) -> None:
        """Close pooled connections; useful when a temporary database is removed."""

        self.engine.dispose()

    def _begin_write(self, session: Session) -> None:
        # PostgreSQL relies on SELECT ... FOR UPDATE below. SQLite does not support
        # row locks, so acquire its reserved write lock before reading state that
        # will govern a transfer. This serializes competing writers and avoids an
        # unsafe read-then-write balance check.
        if self.dialect_name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        with self._session_factory() as session, session.begin():
            self._begin_write(session)
            yield session

    def seed(self) -> None:
        """Insert deterministic fictional users, accounts, contacts, and activity.

        Existing rows are left untouched, making this safe to call at every app
        startup. Initial transaction amounts add up to each seeded account balance.
        """

        now = _utcnow()
        with self._transaction() as session:
            users = (
                (DEMO_USER_ID, DEMO_USER_NAME),
                ("demo-owner-chen", "陈晨"),
                ("demo-owner-zhang-1", "张伟"),
                ("demo-owner-zhang-2", "张伟"),
                ("demo-owner-li", "李娜"),
            )
            for user_id, name in users:
                if session.get(User, user_id) is None:
                    session.add(User(id=user_id, name=name))
            session.flush()

            account_rows = (
                ("demo-checking-001", DEMO_USER_ID, "日常借记卡", "checking", 9_100_567),
                ("demo-savings-001", DEMO_USER_ID, "安心储蓄账户", "savings", 16_008_032),
                ("demo-destination-chen", "demo-owner-chen", "陈晨收款账户", "external", 5_000_000),
                ("demo-destination-zhang-1", "demo-owner-zhang-1", "张伟收款账户", "external", 5_000_000),
                ("demo-destination-zhang-2", "demo-owner-zhang-2", "张伟收款账户", "external", 5_000_000),
                ("demo-destination-li", "demo-owner-li", "李娜收款账户", "external", 5_000_000),
            )
            for account_id, owner_id, name, kind, balance in account_rows:
                if session.get(Account, account_id) is None:
                    session.add(
                        Account(
                            id=account_id,
                            owner_id=owner_id,
                            name=name,
                            kind=kind,
                            balance_minor=balance,
                            currency="CNY",
                        )
                    )
            session.flush()

            recipient_rows = (
                ("demo-recipient-chen", "陈晨", "海岚银行", "2048", "demo-destination-chen"),
                ("demo-recipient-zhang-1", "张伟", "海岚银行", "7712", "demo-destination-zhang-1"),
                ("demo-recipient-zhang-2", "张伟", "东洲银行", "3306", "demo-destination-zhang-2"),
                ("demo-recipient-li", "李娜", "海岚银行", "9185", "demo-destination-li"),
            )
            for recipient_id, name, bank_name, account_last4, destination_id in recipient_rows:
                if session.get(Recipient, recipient_id) is None:
                    session.add(
                        Recipient(
                            id=recipient_id,
                            owner_id=DEMO_USER_ID,
                            name=name,
                            bank_name=bank_name,
                            account_last4=account_last4,
                            destination_account_id=destination_id,
                        )
                    )
            session.flush()

            activity: list[tuple[str, str, int, int]] = [
                # Checking account: the entries sum to 9,100,567 fen.
                ("demo-tx-check-open", "demo-checking-001", "账户期初余额", 10_000_000, 30),
                ("demo-tx-check-salary", "demo-checking-001", "星河设计工作室·工资入账", 2_400_000, 4),
                ("demo-tx-check-rent", "demo-checking-001", "云栖公寓·月租", -2_200_000, 3),
                ("demo-tx-check-grocery", "demo-checking-001", "鲜选超市", -35_690, 2),
                ("demo-tx-check-electricity", "demo-checking-001", "城市供电·电费", -22_800, 1),
                ("demo-tx-check-internet", "demo-checking-001", "云网通信·宽带", -12_900, 1),
                ("demo-tx-check-cafe", "demo-checking-001", "木棉咖啡", -18_700, 0),
                ("demo-tx-check-transit", "demo-checking-001", "城市交通卡", -9_343, 0),
                ("demo-tx-check-save", "demo-checking-001", "转入安心储蓄账户", -1_000_000, 6),
                # The paired savings entry preserves the internal-transfer total.
                ("demo-tx-save-open", "demo-savings-001", "账户期初余额", 15_000_000, 30),
                ("demo-tx-save-checking", "demo-savings-001", "来自日常借记卡", 1_000_000, 6),
                ("demo-tx-save-interest", "demo-savings-001", "季度利息", 8_032, 1),
            ]
            for account_id, _owner_id, _name, _kind, balance in account_rows[2:]:
                activity.append(
                    (
                        f"demo-tx-{account_id}-open",
                        account_id,
                        "收款账户期初余额",
                        balance,
                        30,
                    )
                )
            for tx_id, account_id, description, amount_minor, days_ago in activity:
                if session.get(LedgerTransaction, tx_id) is None:
                    session.add(
                        LedgerTransaction(
                            id=tx_id,
                            account_id=account_id,
                            description=description,
                            amount_minor=amount_minor,
                            currency="CNY",
                            created_at=now - timedelta(days=days_ago, hours=2),
                        )
                    )

    def overview(self) -> dict[str, Any]:
        with self._session_factory() as session:
            user = session.get(User, DEMO_USER_ID)
            if user is None:
                raise BankError("BANK_NOT_SEEDED", "演示账户尚未初始化。")
            return {
                "user": {"id": user.id, "name": user.name},
                "accounts": self._list_accounts(session),
                "transactions": self._list_transactions(session),
            }

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            return self._list_accounts(session)

    def _list_accounts(self, session: Session) -> list[dict[str, Any]]:
        rows = session.scalars(
            select(Account).where(Account.owner_id == DEMO_USER_ID).order_by(Account.kind, Account.id)
        ).all()
        return [
            {
                "id": row.id,
                "name": row.name,
                "kind": row.kind,
                "balance_minor": row.balance_minor,
                "reserved_minor": reserved_balance(session, row.id),
                "available_minor": available_balance(session, row),
                "currency": row.currency,
            }
            for row in rows
        ]

    def list_transactions(self, account_id: str | None = None) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            return self._list_transactions(session, account_id)

    def _list_transactions(self, session: Session, account_id: str | None = None) -> list[dict[str, Any]]:
        query = (
            select(LedgerTransaction)
            .join(Account, LedgerTransaction.account_id == Account.id)
            .where(Account.owner_id == DEMO_USER_ID)
        )
        if account_id is not None:
            query = query.where(Account.id == account_id)
            if (
                session.scalar(
                    select(Account.id).where(Account.id == account_id, Account.owner_id == DEMO_USER_ID)
                )
                is None
            ):
                raise BankError("ACCOUNT_NOT_FOUND", "未找到当前演示用户的该账户。")
        rows = session.scalars(
            query.order_by(LedgerTransaction.created_at.desc(), LedgerTransaction.id.desc())
        ).all()
        return [
            {
                "id": row.id,
                "account_id": row.account_id,
                "description": row.description,
                "amount_minor": row.amount_minor,
                "currency": row.currency,
                "created_at": _iso_utc(row.created_at),
            }
            for row in rows
        ]

    def find_recipients(self, query: str) -> list[dict[str, Any]]:
        needle = query.strip().casefold()
        if not needle:
            return []
        return self.list_recipients(query)

    def list_recipients(self, query: str = "") -> list[dict[str, Any]]:
        """List only this user's saved recipients for explicit form selection."""
        needle = query.strip().casefold()
        with self._session_factory() as session:
            recipients = session.scalars(
                select(Recipient)
                .where(Recipient.owner_id == DEMO_USER_ID)
                .order_by(Recipient.name, Recipient.bank_name, Recipient.account_last4, Recipient.id)
            ).all()
            matches = [
                row
                for row in recipients
                if not needle or needle in row.name.casefold()
                or needle in row.bank_name.casefold()
                or needle in row.account_last4.casefold()
                or needle in row.id.casefold()
            ]
            return [
                {
                    "id": row.id,
                    "name": row.name,
                    "bank_name": row.bank_name,
                    "account_hint": f"尾号 {row.account_last4}",
                    "destination_account_id": row.destination_account_id,
                }
                for row in matches
            ]

    def create_transfer_plan(
        self,
        source_account_id: str,
        recipient_id: str,
        amount_minor: int,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
            raise BankError("INVALID_AMOUNT", "转账金额必须是整数分。")
        if amount_minor <= 0 or amount_minor > MAX_SAFE_JSON_INTEGER:
            raise BankError("INVALID_AMOUNT", "转账金额须为大于零的整数分。")
        if not source_account_id or not recipient_id:
            raise BankError("INVALID_TRANSFER", "请提供出账账户和明确的收款人。")
        if conversation_id is not None and len(conversation_id) > 128:
            raise BankError("INVALID_CONVERSATION", "会话标识长度无效。")

        now = _utcnow()
        plan_id = f"plan-{uuid4()}"
        with self._transaction() as session:
            source = session.scalar(
                select(Account)
                .where(Account.id == source_account_id, Account.owner_id == DEMO_USER_ID)
                .with_for_update()
            )
            if source is None:
                raise BankError("ACCOUNT_NOT_FOUND", "未找到当前演示用户的出账账户。")
            recipient = session.scalar(
                select(Recipient).where(Recipient.id == recipient_id, Recipient.owner_id == DEMO_USER_ID)
            )
            if recipient is None:
                raise BankError("RECIPIENT_NOT_FOUND", "未找到明确的收款人，请先选择联系人。")
            if source.currency != "CNY":
                raise BankError("CURRENCY_UNSUPPORTED", "当前演示转账仅支持人民币账户。")
            if source.id == recipient.destination_account_id:
                raise BankError("INVALID_TRANSFER", "不能向同一账户转账。")
            destination = session.get(Account, recipient.destination_account_id)
            if destination is None or destination.currency != source.currency:
                raise BankError("RECIPIENT_ACCOUNT_UNAVAILABLE", "收款账户暂不可用。")
            if available_balance(session, source) < amount_minor:
                raise BankError("INSUFFICIENT_BALANCE", "账户余额不足，请调整金额或选择其他账户。")

            if conversation_id:
                self._invalidate_pending_in_session(session, conversation_id, now)

            # The demo's internal simulation has no transfer fee. A zero fee keeps
            # the source debit and recipient credit exactly balanced.
            fee_minor = 0
            plan = TransferPlan(
                id=plan_id,
                owner_id=DEMO_USER_ID,
                source_account_id=source.id,
                recipient_id=recipient.id,
                destination_account_id=destination.id,
                amount_minor=amount_minor,
                fee_minor=fee_minor,
                total_minor=amount_minor + fee_minor,
                currency=source.currency,
                status="pending",
                conversation_id=conversation_id,
                created_at=now,
                expires_at=now + TRANSFER_PLAN_TTL,
            )
            session.add(plan)
            result = self._plan_dict(plan, recipient.name)
        return result

    def decide_plan(self, plan_id: str, decision: str) -> dict[str, Any]:
        if decision not in {"approve", "decline"}:
            raise BankError("INVALID_DECISION", "确认操作只能是 approve 或 decline。")

        now = _utcnow()
        with self._transaction() as session:
            plan = session.scalar(
                select(TransferPlan)
                .where(TransferPlan.id == plan_id, TransferPlan.owner_id == DEMO_USER_ID)
                .with_for_update()
            )
            if plan is None:
                raise BankError("PLAN_NOT_FOUND", "未找到当前演示用户的转账计划。")

            if (
                plan.decision in {"approve", "decline"}
                and plan.status in {"completed", "declined"}
                and plan.outcome_status is not None
            ):
                if plan.decision != decision:
                    raise BankError("PLAN_ALREADY_DECIDED", "该转账计划已完成决定，不能更改。")
                return _decision_result(
                    plan.outcome_status,
                    plan.outcome_message or "转账计划已处理。",
                    plan.operation_id,
                    plan.outcome_code,
                )
            if plan.status == "invalidated":
                raise BankError("PLAN_INVALIDATED", "转账信息已更新，请使用最新的转账计划。")
            if plan.status == "expired" or _as_utc(plan.expires_at) <= now:
                plan.status = "expired"
                plan.decision = decision
                plan.outcome_status = "declined"
                plan.outcome_code = "PLAN_EXPIRED"
                plan.outcome_message = "转账计划已过期，未发生资金变动。"
                plan.decided_at = now
                # Persist the expiry outcome. The public decision contract only
                # exposes completed/declined; callers still receive a safe decline.
                return _decision_result("declined", plan.outcome_message, code="PLAN_EXPIRED")
            if plan.status != "pending":
                raise BankError("PLAN_STATE_INVALID", "转账计划状态已变化，请重新发起。")

            if decision == "decline":
                plan.status = "declined"
                plan.decision = "decline"
                plan.outcome_status = "declined"
                plan.outcome_code = None
                plan.outcome_message = "已取消这笔转账，账户资金未变化。"
                plan.decided_at = now
                return _decision_result("declined", plan.outcome_message)

            account_ids = sorted({plan.source_account_id, plan.destination_account_id})
            account_query = select(Account).where(Account.id.in_(account_ids)).order_by(Account.id)
            if self.dialect_name == "postgresql":
                account_query = account_query.with_for_update()
            locked_accounts = {row.id: row for row in session.scalars(account_query).all()}
            source = locked_accounts.get(plan.source_account_id)
            destination = locked_accounts.get(plan.destination_account_id)
            if source is None or source.owner_id != DEMO_USER_ID:
                raise BankError("ACCOUNT_NOT_FOUND", "未找到当前演示用户的出账账户。")
            if destination is None or destination.currency != plan.currency:
                raise BankError("RECIPIENT_ACCOUNT_UNAVAILABLE", "收款账户暂不可用。")
            if source.currency != plan.currency:
                raise BankError("PLAN_STATE_INVALID", "转账计划中的币种与出账账户不一致。")

            if available_balance(session, source) < plan.total_minor:
                plan.status = "declined"
                plan.decision = "approve"
                plan.outcome_status = "declined"
                plan.outcome_code = "INSUFFICIENT_BALANCE"
                plan.outcome_message = "账户余额已不足，转账未执行，请重新查询余额并创建计划。"
                plan.decided_at = now
                return _decision_result("declined", plan.outcome_message, code="INSUFFICIENT_BALANCE")

            operation_id = f"op-{uuid4()}"
            source.balance_minor -= plan.total_minor
            destination.balance_minor += plan.amount_minor
            session.add_all(
                [
                    LedgerTransaction(
                        id=f"{operation_id}-debit",
                        account_id=source.id,
                        description=f"转账至{self._recipient_name(session, plan.recipient_id)}",
                        amount_minor=-plan.total_minor,
                        currency=plan.currency,
                        created_at=now,
                        operation_id=operation_id,
                    ),
                    LedgerTransaction(
                        id=f"{operation_id}-credit",
                        account_id=destination.id,
                        description="转账收款",
                        amount_minor=plan.amount_minor,
                        currency=plan.currency,
                        created_at=now,
                        operation_id=operation_id,
                    ),
                ]
            )
            plan.status = "completed"
            plan.decision = "approve"
            plan.outcome_status = "completed"
            plan.outcome_code = None
            plan.outcome_message = "转账已完成，账本已更新。"
            plan.operation_id = operation_id
            plan.decided_at = now
            return _decision_result("completed", plan.outcome_message, operation_id=operation_id)

    def invalidate_pending_plans(self, conversation_id: str) -> int:
        if not conversation_id:
            return 0
        now = _utcnow()
        with self._transaction() as session:
            return self._invalidate_pending_in_session(session, conversation_id, now)

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        """Read current plan state for a restored thread without approving it."""
        with self._session_factory() as session:
            plan = session.scalar(
                select(TransferPlan).where(TransferPlan.id == plan_id, TransferPlan.owner_id == DEMO_USER_ID)
            )
            if plan is None:
                return None
            result = self._plan_dict(plan, self._recipient_name(session, plan.recipient_id))
            if result["status"] == "pending" and _as_utc(plan.expires_at) <= _utcnow():
                result["status"] = "expired"
            result["conversation_id"] = plan.conversation_id
            return result

    def pending_for_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self._session_factory() as session:
            identity = session.scalar(
                select(TransferPlan.id)
                .where(
                    TransferPlan.owner_id == DEMO_USER_ID,
                    TransferPlan.conversation_id == conversation_id,
                    TransferPlan.status == "pending",
                    TransferPlan.expires_at > _utcnow(),
                )
                .order_by(TransferPlan.created_at.desc())
                .limit(1)
            )
        return self.get_plan(identity) if identity else None

    def completed_events(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            plans = session.scalars(
                select(TransferPlan)
                .where(TransferPlan.owner_id == DEMO_USER_ID, TransferPlan.status == "completed")
                .order_by(TransferPlan.decided_at.desc())
            ).all()
            return [
                {
                    "id": f"transfer:{p.operation_id}",
                    "kind": "transfer",
                    "source": "bank",
                    "title": "转账已完成",
                    "text": f"已向{self._recipient_name(session, p.recipient_id)}转账"
                    f" {Decimal(p.amount_minor) / 100:,.2f} 元，账本已更新。",
                    "created_at": _iso_utc(p.decided_at),
                    "conversation_id": p.conversation_id,
                    "operation_id": p.operation_id,
                    "plan_id": p.id,
                    "receipt": {
                        "status": "completed",
                        "message": p.outcome_message,
                        "operation_id": p.operation_id,
                        "conversation_id": p.conversation_id,
                    },
                }
                for p in plans
            ]

    @staticmethod
    def _invalidate_pending_in_session(session: Session, conversation_id: str, now: datetime) -> int:
        query = select(TransferPlan).where(
            TransferPlan.owner_id == DEMO_USER_ID,
            TransferPlan.conversation_id == conversation_id,
            TransferPlan.status == "pending",
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            query = query.with_for_update()
        plans = session.scalars(query).all()
        for plan in plans:
            plan.status = "invalidated"
            plan.outcome_status = "declined"
            plan.outcome_message = "转账信息已更新，旧计划已失效。"
            plan.decided_at = now
        return len(plans)

    @staticmethod
    def _recipient_name(session: Session, recipient_id: str) -> str:
        name = session.scalar(
            select(Recipient.name).where(Recipient.id == recipient_id, Recipient.owner_id == DEMO_USER_ID)
        )
        return name or "收款人"

    @staticmethod
    def _plan_dict(plan: TransferPlan, recipient_name: str) -> dict[str, Any]:
        return {
            "id": plan.id,
            "source_account_id": plan.source_account_id,
            "recipient_id": plan.recipient_id,
            "recipient_name": recipient_name,
            "destination_account_id": plan.destination_account_id,
            "amount_minor": plan.amount_minor,
            "fee_minor": plan.fee_minor,
            "total_minor": plan.total_minor,
            "currency": plan.currency,
            "status": plan.status,
            "expires_at": _iso_utc(plan.expires_at),
        }


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _decision_result(
    status: str,
    message: str,
    operation_id: str | None = None,
    code: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "message": message}
    if operation_id is not None:
        result["operation_id"] = operation_id
    if code is not None:
        result["code"] = code
    return result
