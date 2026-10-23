"""Service layer for the fixed-profile synthetic banking ledger."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .errors import BankError
from .models import Account, Base, ContactCreateRequest, LedgerTransaction, Recipient, TransferPlan, User
from .reservations import available_balance, reserved_balance

DEMO_USER_ID = "demo-user-001"
DEMO_USER_NAME = "林小满"
MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991
TRANSFER_PLAN_TTL = timedelta(minutes=10)
DEMO_COUNTERPARTY_INITIAL_BALANCE_MINOR = 5_000_000
_CONTACT_UNSET = object()


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
        self._migrate_contact_columns()

    @staticmethod
    def _configure_sqlite(engine: Engine) -> None:
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.close()

    def _migrate_contact_columns(self) -> None:
        """Add contact metadata while preserving all existing recipient IDs."""
        columns = {column["name"] for column in sa_inspect(self.engine).get_columns("bank_recipients")}
        plan_columns = {column["name"] for column in sa_inspect(self.engine).get_columns("bank_transfer_plans")}
        additions = {
            "note": "TEXT NOT NULL DEFAULT ''",
            "active": "BOOLEAN NOT NULL DEFAULT TRUE",
            "version": "INTEGER NOT NULL DEFAULT 1",
        }
        with self.engine.begin() as connection:
            for name, definition in additions.items():
                if name not in columns:
                    connection.exec_driver_sql(f"ALTER TABLE bank_recipients ADD COLUMN {name} {definition}")
            if "recipient_name" not in plan_columns:
                connection.exec_driver_sql("ALTER TABLE bank_transfer_plans ADD COLUMN recipient_name VARCHAR(120)")

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
                .where(Recipient.owner_id == DEMO_USER_ID, Recipient.active.is_(True))
                .order_by(Recipient.name, Recipient.bank_name, Recipient.account_last4, Recipient.id)
            ).all()
            matches = [
                row
                for row in recipients
                if not needle or needle in row.name.casefold()
                or needle in row.note.casefold()
                or needle in row.bank_name.casefold()
                or needle in row.account_last4.casefold()
                or needle in row.id.casefold()
            ]
            return [
                {
                    "id": row.id,
                    "name": row.name,
                    "note": row.note,
                    "bank_name": row.bank_name or None,
                    "account_last4": row.account_last4 or None,
                    "account_hint": f"尾号 {row.account_last4}" if row.bank_name and row.account_last4 else "仅AA参与",
                    "can_transfer": bool(row.bank_name and row.account_last4),
                    "active": row.active,
                    "version": row.version,
                    "destination_account_id": row.destination_account_id,
                }
                for row in matches
            ]

    def list_contacts(self, query: str = "") -> list[dict[str, Any]]:
        """Return active contacts for the shared management and selection UI."""
        needle = query.strip().casefold()
        with self._session_factory() as session:
            rows = session.scalars(
                select(Recipient)
                .where(Recipient.owner_id == DEMO_USER_ID, Recipient.active.is_(True))
                .order_by(Recipient.name, Recipient.bank_name, Recipient.account_last4, Recipient.id)
            ).all()
            return [self._contact_dict(row) for row in rows if _contact_matches(row, needle)]

    def create_contact(
        self,
        name: str,
        *,
        note: str = "",
        bank_name: str | None = None,
        account_last4: str | None = None,
        client_request_id: str,
    ) -> dict[str, Any]:
        values = _validate_contact_fields(name, note, bank_name, account_last4)
        request_id = client_request_id.strip() if isinstance(client_request_id, str) else ""
        if not request_id or len(request_id) > 160:
            raise BankError("INVALID_CLIENT_REQUEST_ID", "请刷新后重试，联系人请求标识无效。")
        payload = {**values}
        with self._transaction() as session:
            owner = session.scalar(
                select(User).where(User.id == DEMO_USER_ID).with_for_update()
            )
            if owner is None:
                raise BankError("BANK_NOT_SEEDED", "演示账户尚未初始化。")
            prior = session.scalar(
                select(ContactCreateRequest).where(
                    ContactCreateRequest.owner_id == DEMO_USER_ID,
                    ContactCreateRequest.client_request_id == request_id,
                )
            )
            if prior is not None:
                if prior.request_payload != payload:
                    raise BankError("CONTACT_REQUEST_CONFLICT", "该保存请求已用于其他联系人，请重新提交。")
                recipient = session.get(Recipient, prior.recipient_id)
                if recipient is None:
                    raise BankError("CONTACT_NOT_FOUND", "联系人已不存在，请重新保存。")
                return {**self._contact_dict(recipient), "already_exists": True}

            duplicate = session.scalar(
                select(Recipient).where(
                    Recipient.owner_id == DEMO_USER_ID,
                    Recipient.active.is_(True),
                    Recipient.name == values["name"],
                    Recipient.note == values["note"],
                    Recipient.bank_name == values["bank_name"],
                    Recipient.account_last4 == values["account_last4"],
                )
            )
            if duplicate is not None:
                recipient = duplicate
                already_exists = True
            else:
                recipient_id = f"contact-{uuid4().hex}"
                destination_id = self._create_counterparty_account(session, values["name"])
                recipient = Recipient(
                    id=recipient_id,
                    owner_id=DEMO_USER_ID,
                    name=values["name"],
                    note=values["note"],
                    bank_name=values["bank_name"],
                    account_last4=values["account_last4"],
                    destination_account_id=destination_id,
                    active=True,
                    version=1,
                )
                session.add(recipient)
                session.flush()
                already_exists = False
            session.add(
                ContactCreateRequest(
                    id=f"contact-request-{uuid4().hex}",
                    owner_id=DEMO_USER_ID,
                    client_request_id=request_id,
                    request_payload=payload,
                    recipient_id=recipient.id,
                    created_at=_utcnow(),
                )
            )
            session.flush()
            return {**self._contact_dict(recipient), "already_exists": already_exists}

    def update_contact(
        self,
        contact_id: str,
        version: int,
        *,
        name: str | None = None,
        note: str | None = None,
        bank_name: str | None | object = _CONTACT_UNSET,
        account_last4: str | None | object = _CONTACT_UNSET,
    ) -> dict[str, Any]:
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise BankError("INVALID_CONTACT_VERSION", "联系人版本无效，请刷新后重试。")
        with self._transaction() as session:
            recipient = session.scalar(
                select(Recipient)
                .where(Recipient.id == contact_id, Recipient.owner_id == DEMO_USER_ID)
                .with_for_update()
            )
            if recipient is None or not recipient.active:
                raise BankError("CONTACT_NOT_FOUND", "联系人已移出可选列表，请刷新。")
            if recipient.version != version:
                raise BankError("CONTACT_VERSION_CONFLICT", "联系人已在其他位置更新，请刷新后再编辑。")
            new_bank = recipient.bank_name
            new_last4 = recipient.account_last4
            if bank_name is not _CONTACT_UNSET or account_last4 is not _CONTACT_UNSET:
                # A null binding field clears the optional pair. Otherwise a
                # partial patch preserves the other currently saved value.
                if bank_name is None or account_last4 is None:
                    new_bank = ""
                    new_last4 = ""
                else:
                    if bank_name is not _CONTACT_UNSET:
                        new_bank = str(bank_name)
                    if account_last4 is not _CONTACT_UNSET:
                        new_last4 = str(account_last4)
            duplicate = session.scalar(
                select(Recipient).where(
                    Recipient.owner_id == DEMO_USER_ID,
                    Recipient.id != recipient.id,
                    Recipient.active.is_(True),
                    Recipient.name == (name.strip() if isinstance(name, str) else recipient.name),
                    Recipient.note == (note.strip() if isinstance(note, str) else recipient.note),
                    Recipient.bank_name == new_bank,
                    Recipient.account_last4 == new_last4,
                )
            )
            if duplicate is not None:
                raise BankError("CONTACT_DUPLICATE", "相同信息的联系人已经存在，请从列表中选择。")
            values = _validate_contact_fields(
                name if name is not None else recipient.name,
                note if note is not None else recipient.note,
                new_bank,
                new_last4,
            )
            changed = any(
                getattr(recipient, key) != value
                for key, value in values.items()
            )
            if not changed:
                return self._contact_dict(recipient)
            binding_changed = (
                recipient.bank_name != values["bank_name"]
                or recipient.account_last4 != values["account_last4"]
            )
            if binding_changed:
                recipient.destination_account_id = self._create_counterparty_account(
                    session, values["name"]
                )
            else:
                destination = session.get(Account, recipient.destination_account_id)
                if destination is not None:
                    destination.name = f"{values['name']}模拟收款账户"
            recipient.name = values["name"]
            recipient.note = values["note"]
            recipient.bank_name = values["bank_name"]
            recipient.account_last4 = values["account_last4"]
            recipient.version += 1
            _invalidate_pending_recipient_plans(session, self.engine, recipient.id, _utcnow())
            session.flush()
            return self._contact_dict(recipient)

    def remove_contact(self, contact_id: str, version: int) -> dict[str, Any]:
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise BankError("INVALID_CONTACT_VERSION", "联系人版本无效，请刷新后重试。")
        with self._transaction() as session:
            recipient = session.scalar(
                select(Recipient)
                .where(Recipient.id == contact_id, Recipient.owner_id == DEMO_USER_ID)
                .with_for_update()
            )
            if recipient is None:
                raise BankError("CONTACT_NOT_FOUND", "未找到该联系人。")
            if not recipient.active:
                return {"contact": self._contact_dict(recipient), "pending_schedule_count": 0}
            if recipient.version != version:
                raise BankError("CONTACT_VERSION_CONFLICT", "联系人已在其他位置更新，请刷新后再移除。")
            pending_schedule_count = _pending_schedule_count(session, self.engine, recipient.id)
            recipient.active = False
            recipient.version += 1
            _invalidate_pending_recipient_plans(session, self.engine, recipient.id, _utcnow())
            session.flush()
            return {
                "contact": self._contact_dict(recipient),
                "pending_schedule_count": pending_schedule_count,
            }

    @staticmethod
    def _contact_dict(recipient: Recipient) -> dict[str, Any]:
        can_transfer = bool(recipient.bank_name and recipient.account_last4)
        return {
            "id": recipient.id,
            "name": recipient.name,
            "note": recipient.note,
            "bank_name": recipient.bank_name or None,
            "account_hint": f"尾号 {recipient.account_last4}" if can_transfer else "仅AA参与",
            "account_last4": recipient.account_last4 or None,
            "can_transfer": can_transfer,
            "version": recipient.version,
            "active": recipient.active,
        }

    def _create_counterparty_account(self, session: Session, name: str) -> str:
        """Create finite demo funds outside the current user's asset accounts."""
        identity = uuid4().hex
        owner_id = f"contact-owner-{identity}"
        account_id = f"contact-account-{identity}"
        session.add(User(id=owner_id, name=f"{name}（模拟联系人）"))
        session.flush()
        session.add(
            Account(
                id=account_id,
                owner_id=owner_id,
                name=f"{name}模拟收款账户",
                kind="external",
                balance_minor=DEMO_COUNTERPARTY_INITIAL_BALANCE_MINOR,
                currency="CNY",
            )
        )
        session.add(
            LedgerTransaction(
                id=f"contact-opening-{identity}",
                account_id=account_id,
                description="联系人模拟账户初始余额（不计入当前用户资产）",
                amount_minor=DEMO_COUNTERPARTY_INITIAL_BALANCE_MINOR,
                currency="CNY",
                created_at=_utcnow(),
            )
        )
        return account_id

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
            if recipient is None or not recipient.active:
                raise BankError("RECIPIENT_NOT_FOUND", "未找到明确的收款人，请先选择联系人。")
            if not recipient.bank_name or not recipient.account_last4:
                raise BankError("RECIPIENT_TRANSFER_DETAILS_REQUIRED", "该联系人目前仅可参与AA，请补充模拟银行和账户尾号后再转账。")
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
                recipient_name=recipient.name,
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
                    "text": f"已向{p.recipient_name or self._recipient_name(session, p.recipient_id)}转账"
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
            "recipient_name": plan.recipient_name or recipient_name,
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


def _validate_contact_fields(
    name: str,
    note: str | None,
    bank_name: str | None,
    account_last4: str | None,
) -> dict[str, str]:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
        raise BankError("INVALID_CONTACT_NAME", "联系人姓名为必填项，长度不能超过40个字符。")
    normalized_note = note.strip() if isinstance(note, str) else ""
    if len(normalized_note) > 120:
        raise BankError("INVALID_CONTACT_NOTE", "联系人备注不能超过120个字符。")
    normalized_bank = bank_name.strip() if isinstance(bank_name, str) else ""
    normalized_last4 = account_last4.strip() if isinstance(account_last4, str) else ""
    if len(normalized_bank) > 120:
        raise BankError("INVALID_CONTACT_BANK", "模拟银行名称不能超过120个字符。")
    if bool(normalized_bank) != bool(normalized_last4):
        raise BankError("INVALID_CONTACT_DETAILS", "模拟银行名称与账户尾号需要同时填写，或同时留空。")
    if normalized_last4 and (len(normalized_last4) != 4 or not normalized_last4.isdigit()):
        raise BankError("INVALID_CONTACT_DETAILS", "账户尾号请填写4位数字。")
    return {
        "name": name.strip(),
        "note": normalized_note,
        "bank_name": normalized_bank,
        "account_last4": normalized_last4,
    }


def _contact_matches(recipient: Recipient, needle: str) -> bool:
    if not needle:
        return True
    return any(
        needle in value.casefold()
        for value in (recipient.name, recipient.note, recipient.bank_name, recipient.account_last4, recipient.id)
    )


def _invalidate_pending_recipient_plans(
    session: Session,
    engine: Engine,
    recipient_id: str,
    now: datetime,
) -> None:
    transfer_plans = session.scalars(
        select(TransferPlan).where(
            TransferPlan.owner_id == DEMO_USER_ID,
            TransferPlan.recipient_id == recipient_id,
            TransferPlan.status == "pending",
        )
    ).all()
    for plan in transfer_plans:
        plan.status = "invalidated"
        plan.outcome_status = "declined"
        plan.outcome_message = "联系人信息已更新，旧转账预览已失效。"
        plan.decided_at = now

    # The task service owns this table's metadata. Contact management may also
    # be used when task actions are disabled, so only query it when initialized.
    if not sa_inspect(engine).has_table("task_action_plans"):
        return
    from app.tasks.models import TaskActionPlan

    task_plans = session.scalars(
        select(TaskActionPlan).where(
            TaskActionPlan.owner_id == DEMO_USER_ID,
            TaskActionPlan.status == "pending",
        )
    ).all()
    for plan in task_plans:
        participant_ids = {
            str(item.get("id")) for item in (plan.participants or []) if isinstance(item, dict)
        }
        if plan.recipient_id != recipient_id and recipient_id not in participant_ids:
            continue
        plan.status = "invalidated"
        plan.outcome_status = "declined"
        plan.outcome_message = "联系人信息已更新，旧任务预览已失效。"
        plan.decided_at = now


def _pending_schedule_count(session: Session, engine: Engine, recipient_id: str) -> int:
    if not sa_inspect(engine).has_table("task_scheduled_transfers"):
        return 0
    from app.tasks.models import ScheduledTransfer

    return len(
        session.scalars(
            select(ScheduledTransfer.id).where(
                ScheduledTransfer.owner_id == DEMO_USER_ID,
                ScheduledTransfer.recipient_id == recipient_id,
                ScheduledTransfer.status == "scheduled",
            )
        ).all()
    )
