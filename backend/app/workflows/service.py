"""Durable, fictional birthday orders across two simulated merchants."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.bank.errors import BankError
from app.bank.models import Account, LedgerTransaction, User
from app.bank.service import DEMO_USER_ID, MAX_SAFE_JSON_INTEGER

from .models import (
    WorkflowBase,
    WorkflowCatalogItem,
    WorkflowEvent,
    WorkflowMerchant,
    WorkflowMerchantOrder,
    WorkflowPlan,
    WorkflowStep,
    WorkflowTask,
)

DEFAULT_ACCOUNT_ID = "demo-checking-001"
PLAN_TTL = timedelta(minutes=10)
MAX_SCHEDULE = timedelta(days=365)
EXECUTION_WINDOW = timedelta(hours=24)

MERCHANTS: dict[str, dict[str, str]] = {
    "merchant-flowers": {
        "name": "花间花坊",
        "owner_id": "workflow-merchant-flowers-owner",
        "account_id": "workflow-merchant-flowers-account",
    },
    "merchant-cake": {
        "name": "云朵烘焙",
        "owner_id": "workflow-merchant-cake-owner",
        "account_id": "workflow-merchant-cake-account",
    },
}

CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "flowers-basic",
        "merchant_id": "merchant-flowers",
        "merchant_name": "花间花坊",
        "name": "生日花束",
        "price_minor": 19_900,
        "currency": "CNY",
        "version": 1,
        "cancel_supported": True,
    },
    {
        "id": "cake-basic",
        "merchant_id": "merchant-cake",
        "merchant_name": "云朵烘焙",
        "name": "生日蛋糕",
        "price_minor": 12_900,
        "currency": "CNY",
        "version": 1,
        "cancel_supported": True,
    },
)


class WorkflowService:
    """Manage simulated merchant orders without contacting any real provider.

    Reservation and ledger helpers are imported from the shared bank package at
    call time. Each merchant step and each compensating refund commits separately
    so a later failure cannot erase an earlier, already paid merchant order.
    """

    def __init__(self, database_url: str, *, fault_mode: str | None = None) -> None:
        if fault_mode not in {None, "cake_out_of_stock", "response_lost", "refund_failure"}:
            raise ValueError("unsupported workflow fault fixture")
        if database_url.startswith("postgres://"):
            database_url = "postgresql+psycopg://" + database_url.removeprefix("postgres://")
        elif database_url.startswith("postgresql://"):
            database_url = "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
        self.database_url = database_url
        self.fault_mode = fault_mode
        engine_options: dict[str, Any] = {"pool_pre_ping": True}
        if database_url.startswith("sqlite"):
            engine_options["connect_args"] = {"check_same_thread": False, "timeout": 10}
            if database_url.endswith((":memory:", ":memory:?cache=shared")):
                engine_options["poolclass"] = StaticPool
        self.engine: Engine = create_engine(database_url, **engine_options)
        if self.engine.dialect.name == "sqlite":
            self._configure_sqlite(self.engine)
        self._session_factory = sessionmaker(self.engine, expire_on_commit=False, class_=Session)
        # BankService owns the common bank metadata; creating it is idempotent and
        # ensures the workflow can be initialized against an otherwise empty demo DB.
        from app.bank.models import Base as BankBase

        BankBase.metadata.create_all(self.engine)
        WorkflowBase.metadata.create_all(self.engine)
        self._migrate_workflow_schema()
        # create_all intentionally leaves existing tables alone. Create the
        # nullable request-key index after extending an existing catalog table.
        from .models import WorkflowCatalogItem as CatalogModel

        for index in CatalogModel.__table__.indexes:
            index.create(self.engine, checkfirst=True)
        # Direct service construction is also supported by tests and local tools;
        # ensure the shared durable clock table exists without owning its logic.
        from app.task_clock import Base as ClockBase
        from app.task_clock import TaskClockState

        ClockBase.metadata.create_all(self.engine)
        with Session(self.engine) as session, session.begin():
            if session.get(TaskClockState, 1) is None:
                session.add(TaskClockState(id=1, offset_seconds=0))

    @staticmethod
    def _configure_sqlite(engine: Engine) -> None:
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.close()

    def _migrate_workflow_schema(self) -> None:
        """Add fields used by the editable catalog to existing demo databases."""

        with self.engine.begin() as connection:
            columns = {column["name"] for column in inspect(connection).get_columns("workflow_catalog_items")}
            dialect = connection.dialect.name
            boolean_default = "1" if dialect == "sqlite" else "TRUE"
            boolean_false = "0" if dialect == "sqlite" else "FALSE"
            additions = {
                "unit": "VARCHAR(40) NOT NULL DEFAULT '份'",
                "description": "TEXT NOT NULL DEFAULT ''",
                "active": f"BOOLEAN NOT NULL DEFAULT {boolean_default}",
                "client_request_id": "VARCHAR(120)",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE workflow_catalog_items ADD COLUMN {name} {declaration}"
                    )
            plan_columns = {column["name"] for column in inspect(connection).get_columns("workflow_plans")}
            if "title" not in plan_columns:
                connection.exec_driver_sql("ALTER TABLE workflow_plans ADD COLUMN title VARCHAR(160)")
            if "revision_json" not in plan_columns:
                connection.exec_driver_sql("ALTER TABLE workflow_plans ADD COLUMN revision_json TEXT NOT NULL DEFAULT '{}'")
            if "execute_immediately" not in plan_columns:
                # Legacy rows are not safely inferable from execute_at alone.
                connection.exec_driver_sql(
                    f"ALTER TABLE workflow_plans ADD COLUMN execute_immediately BOOLEAN NOT NULL DEFAULT {boolean_false}"
                )
            task_columns = {column["name"] for column in inspect(connection).get_columns("workflow_tasks")}
            if "title" not in task_columns:
                connection.exec_driver_sql("ALTER TABLE workflow_tasks ADD COLUMN title VARCHAR(160)")

    @property
    def dialect_name(self) -> str:
        return self.engine.dialect.name

    def close(self) -> None:
        self.engine.dispose()

    def _begin_write(self, session: Session) -> None:
        if self.dialect_name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        with self._session_factory() as session, session.begin():
            self._begin_write(session)
            yield session

    def seed_demo(self) -> None:
        """Insert catalog and isolated merchant accounts without resetting state."""

        with self._transaction() as session:
            for merchant_id, definition in MERCHANTS.items():
                self._ensure_merchant(
                    session,
                    definition["name"],
                    fixed={**definition, "id": merchant_id},
                )
            session.flush()
            for definition in CATALOG:
                if session.get(WorkflowCatalogItem, definition["id"]) is None:
                    session.add(WorkflowCatalogItem(**definition, unit="份", description="", active=True))

    def now(self) -> datetime:
        with self._session_factory() as session:
            return _task_now(session)

    def catalog(self) -> list[dict[str, Any]]:
        return self.list_services()

    def list_services(self, query: str | None = None) -> list[dict[str, Any]]:
        """Return active simulated services, optionally matching their labels."""

        needle = query.strip().casefold() if isinstance(query, str) else ""
        with self._session_factory() as session:
            products = session.scalars(
                select(WorkflowCatalogItem).where(WorkflowCatalogItem.active.is_(True))
            ).all()
            catalog_order = {item["id"]: index for index, item in enumerate(CATALOG)}
            products.sort(key=lambda product: (catalog_order.get(product.id, len(catalog_order)), product.id))
            result = [_product_dict(product) for product in products]
            if needle:
                result = [
                    row for row in result
                    if needle in " ".join(
                        str(row[key]) for key in ("name", "merchant_name", "unit", "description")
                    ).casefold()
                ]
            return result

    def get_service(self, service_id: str) -> dict[str, Any] | None:
        if not service_id:
            return None
        with self._session_factory() as session:
            item = session.get(WorkflowCatalogItem, service_id)
            return _product_dict(item) if item is not None else None

    def create_service(
        self,
        name: str,
        merchant_name: str,
        price_minor: int,
        *,
        unit: str = "份",
        description: str = "",
        cancel_supported: bool = True,
        client_request_id: str | None = None,
    ) -> dict[str, Any]:
        values = _validate_service_fields(
            name=name,
            merchant_name=merchant_name,
            price_minor=price_minor,
            unit=unit,
            description=description,
            cancel_supported=cancel_supported,
            client_request_id=client_request_id,
        )
        with self._transaction() as session:
            if values["client_request_id"]:
                prior = session.scalar(
                    select(WorkflowCatalogItem).where(
                        WorkflowCatalogItem.client_request_id == values["client_request_id"]
                    )
                )
                if prior is not None:
                    return _product_dict(prior)
            merchant = self._ensure_merchant(session, values["merchant_name"])
            item = WorkflowCatalogItem(
                id=f"service-{uuid4()}",
                merchant_id=merchant.id,
                merchant_name=merchant.name,
                name=values["name"],
                price_minor=values["price_minor"],
                currency="CNY",
                version=1,
                cancel_supported=values["cancel_supported"],
                unit=values["unit"],
                description=values["description"],
                active=True,
                client_request_id=values["client_request_id"],
            )
            session.add(item)
            session.flush()
            return _product_dict(item)

    def update_service(
        self,
        service_id: str,
        *,
        version: int,
        name: str | None = None,
        merchant_name: str | None = None,
        price_minor: int | None = None,
        unit: str | None = None,
        description: str | None = None,
        cancel_supported: bool | None = None,
    ) -> dict[str, Any]:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise BankError("INVALID_VERSION", "请提供有效的服务版本号。")
        with self._transaction() as session:
            item = session.get(WorkflowCatalogItem, service_id)
            if item is None:
                raise BankError("SERVICE_NOT_FOUND", "未找到该模拟服务。")
            if item.version != version:
                raise BankError("VERSION_CONFLICT", "服务已被其他操作更新，请刷新后重新编辑。")
            if not item.active:
                raise BankError("SERVICE_NOT_FOUND", "该模拟服务已从可选目录移除。")
            values = _validate_service_fields(
                name=item.name if name is None else name,
                merchant_name=item.merchant_name if merchant_name is None else merchant_name,
                price_minor=item.price_minor if price_minor is None else price_minor,
                unit=item.unit if unit is None else unit,
                description=item.description if description is None else description,
                cancel_supported=item.cancel_supported if cancel_supported is None else cancel_supported,
                client_request_id=item.client_request_id,
            )
            changed = any(
                getattr(item, key) != values[key]
                for key in ("name", "merchant_name", "price_minor", "unit", "description", "cancel_supported")
            )
            if changed:
                merchant = self._ensure_merchant(session, values["merchant_name"])
                item.merchant_id = merchant.id
                item.merchant_name = merchant.name
                item.name = values["name"]
                item.price_minor = values["price_minor"]
                item.unit = values["unit"]
                item.description = values["description"]
                item.cancel_supported = values["cancel_supported"]
                item.version += 1
                self._invalidate_service_plans(session, item.id, _utcnow())
                session.flush()
            return _product_dict(item)

    def remove_service(self, service_id: str, *, version: int) -> dict[str, Any]:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise BankError("INVALID_VERSION", "请提供有效的服务版本号。")
        with self._transaction() as session:
            item = session.get(WorkflowCatalogItem, service_id)
            if item is None:
                raise BankError("SERVICE_NOT_FOUND", "未找到该模拟服务。")
            if item.version != version:
                raise BankError("VERSION_CONFLICT", "服务已被其他操作更新，请刷新后重新操作。")
            if item.active:
                item.active = False
                item.version += 1
                self._invalidate_service_plans(session, item.id, _utcnow())
                session.flush()
            return _product_dict(item)

    def _ensure_merchant(
        self,
        session: Session,
        merchant_name: str,
        *,
        fixed: dict[str, str] | None = None,
    ) -> WorkflowMerchant:
        merchant_name = merchant_name.strip()
        merchant = session.scalar(select(WorkflowMerchant).where(WorkflowMerchant.name == merchant_name))
        if merchant is not None:
            # Older PG test fixture cleanup could leave the merchant row while
            # removing its synthetic settlement account. Keep seeding idempotent
            # and repair that dangling demo identity without resetting balances.
            owner = session.get(User, merchant.owner_id)
            expected_owner_name = f"{merchant_name}（模拟）"
            if owner is None:
                session.add(User(id=merchant.owner_id, name=expected_owner_name))
                session.flush()
            elif owner.name != expected_owner_name:
                raise BankError("MERCHANT_IDENTITY_MISMATCH", "模拟商户身份记录不匹配。")
            account = session.get(Account, merchant.account_id)
            if account is None:
                session.add(
                    Account(
                        id=merchant.account_id,
                        owner_id=merchant.owner_id,
                        name=f"{merchant_name}模拟结算账户",
                        kind="workflow_merchant",
                        balance_minor=0,
                        currency="CNY",
                    )
                )
                session.flush()
            elif account.owner_id != merchant.owner_id or account.currency != "CNY":
                raise BankError("MERCHANT_ACCOUNT_MISMATCH", "模拟商户结算账户身份不匹配。")
            return merchant
        identity = fixed or {}
        suffix = uuid4().hex if fixed is None else ""
        merchant_id = identity.get("id") or f"merchant-{suffix}"
        owner_id = identity.get("owner_id") or f"workflow-merchant-{suffix}-owner"
        account_id = identity.get("account_id") or f"workflow-merchant-{suffix}-account"
        owner = session.get(User, owner_id)
        expected_owner_name = f"{merchant_name}（模拟）"
        if owner is None:
            session.add(User(id=owner_id, name=expected_owner_name))
            session.flush()
        elif owner.name != expected_owner_name:
            raise BankError("MERCHANT_IDENTITY_MISMATCH", "模拟商户身份记录不匹配。")
        account = session.get(Account, account_id)
        if account is None:
            session.add(
                Account(
                    id=account_id,
                    owner_id=owner_id,
                    name=f"{merchant_name}模拟结算账户",
                    kind="workflow_merchant",
                    balance_minor=0,
                    currency="CNY",
                )
            )
        elif account.owner_id != owner_id or account.currency != "CNY":
            raise BankError("MERCHANT_ACCOUNT_MISMATCH", "模拟商户结算账户身份不匹配。")
        merchant = WorkflowMerchant(
            id=merchant_id,
            name=merchant_name,
            owner_id=owner_id,
            account_id=account_id,
        )
        session.add(merchant)
        session.flush()
        return merchant

    @staticmethod
    def _merchant_account(session: Session, merchant_id: str) -> str:
        merchant = session.get(WorkflowMerchant, merchant_id)
        if merchant is None:
            raise BankError("MERCHANT_NOT_FOUND", "模拟商品关联的商户不可用。")
        return merchant.account_id

    @staticmethod
    def _invalidate_service_plans(session: Session, service_id: str, now: datetime) -> None:
        plans = session.scalars(
            select(WorkflowPlan).where(WorkflowPlan.action == "arrange", WorkflowPlan.status == "pending")
        ).all()
        for plan in plans:
            if any(row.get("product_id") == service_id for row in json.loads(plan.items_json or "[]")):
                plan.status = "invalidated"
                plan.outcome_status = "declined"
                plan.outcome_code = "PRODUCT_RULE_CHANGED"
                plan.outcome_message = "服务目录已更新，旧确认单已失效；请重新核对最新报价。"
                plan.decided_at = now

    def create_action_plan(
        self,
        action: str,
        conversation_id: str | None,
        *,
        account_id: str | None = None,
        recipient_label: str | None = None,
        budget_minor: int | None = None,
        items: list[dict[str, Any]] | None = None,
        execute_at: datetime | str | None = None,
        task_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        if action not in {"arrange", "cancel"}:
            raise BankError("INVALID_ACTION", "联动操作只能是 arrange 或 cancel。")
        if conversation_id is not None and (
            not isinstance(conversation_id, str) or len(conversation_id) > 128
        ):
            raise BankError("INVALID_CONVERSATION", "会话标识长度无效。")
        now_real = _utcnow()
        plan_id = f"workflow-plan-{uuid4()}"

        if action == "cancel":
            if not isinstance(task_id, str) or not task_id or len(task_id) > 100:
                raise BankError("TASK_NOT_FOUND", "取消联动安排时必须提供有效的 task_id。")
            with self._transaction() as session:
                task = self._get_task_for_update(session, task_id)
                if task is None:
                    raise BankError("TASK_NOT_FOUND", "未找到该生日安排。")
                if task.status in {"cancelled", "expired"}:
                    raise BankError("TASK_NOT_CANCELLABLE", "该生日安排已经结束，不能再次取消。")
                if task.status == "failed" and task.refund_pending_minor == 0:
                    raise BankError("TASK_NOT_CANCELLABLE", "失败安排的已授权补偿已完成，请查看最终结果。")
                if conversation_id:
                    self._invalidate_pending_in_session(session, conversation_id, now_real)
                plan = WorkflowPlan(
                    id=plan_id,
                    action="cancel",
                    title=task.title,
                    task_id=task.id,
                    account_id=task.account_id,
                    recipient_label=task.recipient_label,
                    budget_minor=task.budget_minor,
                    total_minor=task.total_minor,
                    items_json=json.dumps(self._step_snapshots(session, task.id), ensure_ascii=False),
                    execute_at=task.execute_at,
                    deadline_at=task.deadline_at,
                    status="pending",
                    conversation_id=conversation_id,
                    created_at=now_real,
                    expires_at=now_real + PLAN_TTL,
                )
                session.add(plan)
                session.flush()
                return self._plan_dict(plan)

        if account_id is not None and (not isinstance(account_id, str) or not account_id):
            raise BankError("INVALID_ACCOUNT", "请选择当前用户的人民币结算账户。")
        resolved_account_id = account_id or DEFAULT_ACCOUNT_ID
        if not isinstance(resolved_account_id, str) or not resolved_account_id:
            raise BankError("INVALID_ACCOUNT", "请选择当前用户的人民币结算账户。")
        if (
            not isinstance(recipient_label, str)
            or not recipient_label.strip()
            or len(recipient_label.strip()) > 120
        ):
            raise BankError("INVALID_RECIPIENT_LABEL", "请填写不超过120字的模拟收件人称呼。")
        if isinstance(budget_minor, bool) or not isinstance(budget_minor, int):
            raise BankError("INVALID_BUDGET", "预算必须是整数分。")
        if budget_minor <= 0 or budget_minor > MAX_SAFE_JSON_INTEGER:
            raise BankError("INVALID_BUDGET", "预算必须为大于零的安全整数分。")
        snapshots = _normalize_items(items)
        if title is not None:
            title = title.strip()
            if not title or len(title) > 160:
                raise BankError("INVALID_TITLE", "安排名称须为1至160字。")
        execute_immediately = execute_at is None
        requested_at: datetime | None = _parse_aware_datetime(execute_at) if execute_at is not None else None

        with self._transaction() as session:
            return self._create_arrange_plan_in_session(
                session,
                plan_id=plan_id,
                task_id=task_id,
                conversation_id=conversation_id,
                account_id=resolved_account_id,
                recipient_label=recipient_label.strip(),
                budget_minor=budget_minor,
                title=title,
                snapshots=snapshots,
                execute_immediately=execute_immediately,
                requested_at=requested_at,
                now_real=now_real,
            )

    def revise_action_plan(
        self,
        plan_id: str,
        conversation_id: str,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        """Replace one pending arrange preview while retaining untouched fields.

        ``items`` is a complete replacement list. Omitted keys retain their
        source values; ``title=None`` clears the custom title. Omitting
        ``execute_at`` preserves the source timing mode; explicit null means
        immediate execution.
        """

        allowed = {
            "title",
            "recipient_label",
            "budget_minor",
            "account_id",
            "execute_at",
            "items",
        }
        if not isinstance(changes, dict) or not changes:
            raise BankError("INVALID_PLAN_CHANGES", "至少提供一项需要修改的安排内容。")
        unknown = set(changes) - allowed
        if unknown:
            raise BankError("INVALID_PLAN_CHANGES", "修改字段不受支持，请刷新安排后重试。")
        if not isinstance(plan_id, str) or not plan_id or len(plan_id) > 100:
            raise BankError("PLAN_NOT_FOUND", "未找到生日安排计划。")
        if not isinstance(conversation_id, str) or not conversation_id or len(conversation_id) > 128:
            raise BankError("INVALID_CONVERSATION", "会话标识长度无效。")

        now_real = _utcnow()
        expired = False
        with self._transaction() as session:
            source = self._get_plan_for_update(session, plan_id)
            if source is None:
                raise BankError("PLAN_NOT_FOUND", "未找到生日安排计划。")
            if source.conversation_id != conversation_id:
                raise BankError("CONVERSATION_MISMATCH", "这张安排预览属于另一条会话。")
            if source.action != "arrange":
                raise BankError("PLAN_NOT_REVISABLE", "只有生活安排预览可以修改。")
            if source.status == "pending" and _as_utc(source.expires_at) <= now_real:
                self._mark_expired(source, now_real)
                expired = True
            elif source.status != "pending":
                if source.status == "invalidated":
                    raise BankError("PLAN_INVALIDATED", "这张安排预览已被替代，请使用最新预览。")
                if source.status == "expired":
                    raise BankError("PLAN_EXPIRED", "这张安排预览已过期，请重新发起。")
                if source.decision is not None or source.status in {"completed", "declined"}:
                    raise BankError("PLAN_ALREADY_DECIDED", "已确认或拒绝的安排不能修改。")
                raise BankError("PLAN_STATE_INVALID", "安排预览状态已变化，不能继续修改。")
            else:
                raw_items = json.loads(source.items_json or "[]")
                normalized_source_items = [
                    {"product_id": row["product_id"], "quantity": row["quantity"]}
                    for row in raw_items
                ]

                account_id = source.account_id
                if "account_id" in changes:
                    account_id = changes["account_id"]
                    if not isinstance(account_id, str) or not account_id.strip():
                        raise BankError("INVALID_ACCOUNT", "请选择当前用户的人民币结算账户。")
                    account_id = account_id.strip()

                recipient_label = source.recipient_label
                if "recipient_label" in changes:
                    recipient_label = changes["recipient_label"]
                    if not isinstance(recipient_label, str):
                        raise BankError("INVALID_RECIPIENT_LABEL", "请填写有效的模拟收件人称呼。")
                    recipient_label = recipient_label.strip()

                budget_minor = source.budget_minor
                if "budget_minor" in changes:
                    budget_minor = changes["budget_minor"]

                title = source.title
                if "title" in changes:
                    title = changes["title"]

                requested_at: datetime | None
                if "execute_at" in changes:
                    value = changes["execute_at"]
                    requested_at = None if value is None else _parse_aware_datetime(value)
                    execute_immediately = value is None
                else:
                    execute_immediately = bool(source.execute_immediately)
                    requested_at = (
                        None
                        if execute_immediately
                        else _as_utc(source.execute_at)
                        if source.execute_at is not None
                        else None
                    )

                if "items" in changes and changes["items"] is None:
                    raise BankError("INVALID_ITEMS", "items须是完整服务列表；移除服务时请提交剩余服务。")
                raw_replacement_items = changes.get("items", normalized_source_items)
                snapshots = _normalize_items(raw_replacement_items)
                if title is not None:
                    if not isinstance(title, str):
                        raise BankError("INVALID_TITLE", "安排名称须为文字。")
                    title = title.strip()
                    if not title or len(title) > 160:
                        raise BankError("INVALID_TITLE", "安排名称须为1至160字。")

                result = self._create_arrange_plan_in_session(
                    session,
                    plan_id=f"workflow-plan-{uuid4()}",
                    task_id=None,
                    conversation_id=conversation_id,
                    account_id=account_id,
                    recipient_label=recipient_label,
                    budget_minor=budget_minor,
                    title=title,
                    snapshots=snapshots,
                    execute_immediately=execute_immediately,
                    requested_at=requested_at,
                    now_real=now_real,
                    revision_source=source,
                )

        if expired:
            raise BankError("PLAN_EXPIRED", "这张安排预览已过期，请重新发起。")
        return result

    def _create_arrange_plan_in_session(
        self,
        session: Session,
        *,
        plan_id: str,
        task_id: str | None,
        conversation_id: str | None,
        account_id: str,
        recipient_label: str,
        budget_minor: int | None,
        title: str | None,
        snapshots: list[dict[str, Any]],
        execute_immediately: bool,
        requested_at: datetime | None,
        now_real: datetime,
        revision_source: WorkflowPlan | None = None,
    ) -> dict[str, Any]:
        """Validate, quote, and persist an arrange plan in its caller's transaction."""

        clock_now = _task_now(session)
        if not isinstance(execute_immediately, bool):
            raise BankError("INVALID_EXECUTE_AT", "执行方式无效，请重新预览。")
        if execute_immediately:
            if requested_at is not None:
                raise BankError("INVALID_EXECUTE_AT", "立即执行预览不能同时指定未来时间。")
            scheduled_at = clock_now
        else:
            if requested_at is None:
                raise BankError("INVALID_EXECUTE_AT", "定时安排缺少明确的执行时间。")
            scheduled_at = requested_at
        if scheduled_at < clock_now:
            raise BankError("INVALID_EXECUTE_AT", "执行时间不能早于当前模拟时间。")
        if scheduled_at > clock_now + MAX_SCHEDULE:
            raise BankError("INVALID_EXECUTE_AT", "执行时间不能超过当前模拟时间365天。")
        account = session.get(Account, account_id)
        if account is None or account.owner_id != DEMO_USER_ID or account.currency != "CNY":
            raise BankError("ACCOUNT_NOT_FOUND", "未找到当前演示用户的人民币结算账户。")

        priced_items: list[dict[str, Any]] = []
        for raw in snapshots:
            product = session.get(WorkflowCatalogItem, raw["product_id"])
            if product is None or not product.active:
                raise BankError("PRODUCT_NOT_FOUND", "所选模拟商户商品不存在。")
            if product.currency != "CNY" or product.price_minor <= 0:
                raise BankError("PRODUCT_RULE_INVALID", "模拟商户目录中的商品规则无效。")
            quantity = raw["quantity"]
            priced_items.append(
                {
                    "product_id": product.id,
                    "merchant_id": product.merchant_id,
                    "merchant_name": product.merchant_name,
                    "merchant_account_id": self._merchant_account(session, product.merchant_id),
                    "name": product.name,
                    "unit": product.unit,
                    "description": product.description,
                    "quantity": quantity,
                    "unit_price_minor": product.price_minor,
                    "total_minor": product.price_minor * quantity,
                    "currency": product.currency,
                    "catalog_version": product.version,
                    "cancel_supported": product.cancel_supported,
                }
            )
        total_minor = sum(row["total_minor"] for row in priced_items)
        if total_minor <= 0 or total_minor > MAX_SAFE_JSON_INTEGER:
            raise BankError("INVALID_AMOUNT", "报价金额超过安全整数范围。")
        if isinstance(budget_minor, bool) or not isinstance(budget_minor, int):
            raise BankError("INVALID_BUDGET", "预算必须是整数分。")
        if budget_minor <= 0 or budget_minor > MAX_SAFE_JSON_INTEGER:
            raise BankError("INVALID_BUDGET", "预算必须为大于零的安全整数分。")
        if total_minor > budget_minor:
            raise BankError("BUDGET_EXCEEDED", "模拟商户报价超过已填写预算，请调整商品或预算后重新预览。")
        if not isinstance(recipient_label, str) or not recipient_label.strip() or len(recipient_label.strip()) > 120:
            raise BankError("INVALID_RECIPIENT_LABEL", "请填写不超过120字的模拟收件人称呼。")
        recipient_label = recipient_label.strip()
        resolved_title = title
        builtin_service_ids = {row["id"] for row in CATALOG}
        if resolved_title is None and any(row["product_id"] not in builtin_service_ids for row in priced_items):
            resolved_title = f"给{recipient_label}的服务安排"

        from app.bank.reservations import available_balance

        if available_balance(session, account) < total_minor:
            raise BankError(
                "INSUFFICIENT_BALANCE", "账户可用余额不足以覆盖该报价；其他预留资金保持不变。"
            )
        generated_task_id = task_id or f"workflow-{uuid4()}"
        if not isinstance(generated_task_id, str) or not generated_task_id or len(generated_task_id) > 100:
            raise BankError("INVALID_TASK_ID", "task_id长度无效。")
        if session.get(WorkflowTask, generated_task_id) is not None:
            raise BankError("TASK_ID_CONFLICT", "task_id已被使用，请重新创建预览。")
        prior_plan = session.scalar(
            select(WorkflowPlan.id).where(
                WorkflowPlan.action == "arrange",
                WorkflowPlan.task_id == generated_task_id,
            )
        )
        if prior_plan is not None:
            raise BankError("TASK_ID_CONFLICT", "task_id已被其他安排计划使用，请重新创建预览。")

        revision = None
        if revision_source is not None:
            revision = _plan_revision(revision_source, {
                "title": resolved_title,
                "recipient_label": recipient_label,
                "budget_minor": budget_minor,
                "account_id": account.id,
                "execute_immediately": execute_immediately,
                "execute_at": scheduled_at,
                "items": priced_items,
            }, session)
            if not revision["changes"]:
                raise BankError("NO_PLAN_CHANGES", "修改内容与当前安排相同，没有需要更新的项目。")

        # This is the only invalidation point. All proposed values have now been
        # validated and quoted; an exception above leaves the source untouched.
        if conversation_id:
            self._invalidate_pending_in_session(session, conversation_id, now_real)
        plan = WorkflowPlan(
            id=plan_id,
            action="arrange",
            title=resolved_title,
            task_id=generated_task_id,
            account_id=account.id,
            recipient_label=recipient_label,
            budget_minor=budget_minor,
            total_minor=total_minor,
            items_json=json.dumps(priced_items, ensure_ascii=False, sort_keys=True),
            revision_json=json.dumps(revision or {}, ensure_ascii=False, sort_keys=True),
            execute_immediately=execute_immediately,
            execute_at=scheduled_at,
            deadline_at=scheduled_at + EXECUTION_WINDOW,
            status="pending",
            conversation_id=conversation_id,
            created_at=now_real,
            expires_at=now_real + PLAN_TTL,
        )
        session.add(plan)
        session.flush()
        return self._plan_dict(plan)

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
            return self._plan_dict(plan)

    def pending_for_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        if not conversation_id:
            return None
        now = _utcnow()
        with self._transaction() as session:
            plans = self._plans_for_conversation(session, conversation_id)
            for plan in plans:
                if plan.status == "pending" and _as_utc(plan.expires_at) <= now:
                    self._mark_expired(plan, now)
            pending = [plan for plan in plans if plan.status == "pending"]
            if not pending:
                return None
            return self._plan_dict(max(pending, key=lambda plan: (plan.created_at, plan.id)))

    def invalidate_pending_plans(self, conversation_id: str) -> int:
        if not conversation_id:
            return 0
        with self._transaction() as session:
            return self._invalidate_pending_in_session(session, conversation_id, _utcnow())

    def decide_plan(self, plan_id: str, decision: str, conversation_id: str | None = None) -> dict[str, Any]:
        if decision not in {"approve", "decline"}:
            raise BankError("INVALID_DECISION", "确认操作只能是 approve 或 decline。")
        if not plan_id:
            raise BankError("PLAN_NOT_FOUND", "未找到生日安排计划。")
        # Read identifiers before taking locks. Every write transaction then follows
        # account IDs (sorted), task/plan/order rows, and finally the shared hold.
        with self._session_factory() as session:
            identity = session.get(WorkflowPlan, plan_id)
            if identity is None:
                raise BankError("PLAN_NOT_FOUND", "未找到生日安排计划。")
            account_ids = self._account_ids_for_plan(session, identity)
            task_id = identity.task_id
            action = identity.action

        immediate_task_id: str | None = None
        cancel_task_id: str | None = None
        with self._transaction() as session:
            self._lock_accounts(session, account_ids)
            plan = self._get_plan_for_update(session, plan_id)
            if plan is None:
                raise BankError("PLAN_NOT_FOUND", "未找到生日安排计划。")
            if conversation_id is not None and plan.conversation_id != conversation_id:
                raise BankError("CONVERSATION_MISMATCH", "该生日安排计划属于另一条会话。")

            if plan.status in {"completed", "declined", "expired"} and plan.outcome_status is not None:
                if plan.decision is not None and plan.decision != decision:
                    raise BankError("PLAN_ALREADY_DECIDED", "该生日安排计划已完成决定，不能更改。")
                return self._decision_result(session, plan)
            if plan.status == "invalidated":
                raise BankError("PLAN_INVALIDATED", "生日安排信息已更新，旧计划已失效。")
            now = _utcnow()
            if plan.status == "pending" and _as_utc(plan.expires_at) <= now:
                self._mark_expired(plan, now, decision=decision)
                return self._decision_result(session, plan)
            if plan.status != "pending":
                raise BankError("PLAN_STATE_INVALID", "生日安排计划状态已变化，请重新发起。")
            if decision == "decline":
                plan.status = "declined"
                plan.decision = decision
                plan.outcome_status = "declined"
                plan.outcome_code = None
                plan.outcome_message = "已取消这份模拟生日安排预览，账户资金未变化。"
                plan.decided_at = now
                return self._decision_result(session, plan)

            if action == "arrange":
                assert plan.task_id is not None and plan.account_id is not None
                account = next(
                    (
                        row
                        for row in self._locked_account_rows(session, account_ids)
                        if row.id == plan.account_id
                    ),
                    None,
                )
                if account is None or account.owner_id != DEMO_USER_ID or account.currency != "CNY":
                    self._decline_plan(plan, now, "ACCOUNT_NOT_FOUND", "本人结算账户已不可用，未预留或下单。")
                    return self._decision_result(session, plan)
                snapshots = json.loads(plan.items_json)
                if not self._quote_still_current(session, snapshots):
                    self._decline_plan(
                        plan, now, "PRODUCT_RULE_CHANGED", "商品价格或目录版本已变化，未下单；请重新预览。"
                    )
                    return self._decision_result(session, plan)
                from app.bank.reservations import available_balance, reserve_funds

                assert plan.total_minor is not None and plan.budget_minor is not None
                if (
                    plan.total_minor > plan.budget_minor
                    or available_balance(session, account) < plan.total_minor
                ):
                    self._decline_plan(
                        plan, now, "INSUFFICIENT_BALANCE", "当前可用余额不足以覆盖确认报价，未下单。"
                    )
                    return self._decision_result(session, plan)
                hold_reference = f"workflow-hold-{_digest(plan.task_id)}"
                hold = reserve_funds(session, account.id, plan.total_minor, hold_reference)
                task_now = _task_now(session)
                assert plan.execute_at is not None and plan.deadline_at is not None
                task = WorkflowTask(
                    id=plan.task_id,
                    plan_id=plan.id,
                    title=plan.title,
                    account_id=account.id,
                    recipient_label=plan.recipient_label or "模拟收件人",
                    budget_minor=plan.budget_minor,
                    total_minor=plan.total_minor,
                    reserved_minor=plan.total_minor,
                    paid_minor=0,
                    refunded_minor=0,
                    refund_pending_minor=0,
                    net_spent_minor=0,
                    status="scheduled",
                    execute_at=plan.execute_at,
                    deadline_at=plan.deadline_at,
                    hold_id=hold.id,
                    conversation_id=plan.conversation_id,
                    state_version=1,
                    message=(
                        f"{plan.title}已确认并预留实际报价；这是模拟订单流程，不代表商品已配送。"
                        if plan.title
                        else "生日安排已确认并预留实际报价；这是模拟订单流程，不代表商品已配送。"
                    ),
                    cancel_requested=False,
                    fault_response_lost_done=False,
                    related_plan_ids_json="[]",
                    created_at=task_now,
                    updated_at=task_now,
                )
                session.add(task)
                for index, snapshot in enumerate(snapshots):
                    session.add(
                        WorkflowStep(
                            id=_step_id(task.id, index),
                            task_id=task.id,
                            step_index=index,
                            product_id=snapshot["product_id"],
                            merchant_id=snapshot["merchant_id"],
                            merchant_name=snapshot["merchant_name"],
                            product_name=snapshot["name"],
                            catalog_version=snapshot["catalog_version"],
                            unit_price_minor=snapshot["unit_price_minor"],
                            quantity=snapshot["quantity"],
                            total_minor=snapshot["total_minor"],
                            cancel_supported=snapshot["cancel_supported"],
                            merchant_account_id=snapshot["merchant_account_id"],
                            order_id=None,
                            status="pending",
                            message="等待模拟商户处理",
                            refund_attempts=0,
                            created_at=task_now,
                            updated_at=task_now,
                        )
                    )
                plan.status = "completed"
                plan.decision = decision
                plan.outcome_status = "completed"
                plan.outcome_code = None
                plan.outcome_message = task.message
                plan.operation_id = f"workflow-{_digest(task.id)}-accepted"
                plan.decided_at = now
                self._add_event(
                    session,
                    task,
                    f"{plan.title}已确认" if plan.title else "生日安排已确认",
                    task.message,
                    now,
                    plan.operation_id,
                )
                if _as_utc(plan.execute_at) <= _as_utc(task_now):
                    immediate_task_id = task.id
            else:
                if not task_id:
                    raise BankError("TASK_NOT_FOUND", "取消计划缺少原生日安排标识。")
                task = self._get_task_for_update(session, task_id)
                if task is None:
                    self._decline_plan(plan, now, "TASK_NOT_FOUND", "原生日安排已不存在，无法取消。")
                    return self._decision_result(session, plan)
                if task.status in {"cancelled", "expired"}:
                    self._decline_plan(
                        plan, now, "TASK_NOT_CANCELLABLE", "原生日安排已经结束，未发生额外资金变动。"
                    )
                    return self._decision_result(session, plan)
                related = set(json.loads(task.related_plan_ids_json or "[]"))
                related.add(plan.id)
                task.related_plan_ids_json = json.dumps(sorted(related))
                task.cancel_requested = True
                task.message = "已接受取消请求；正在核对原订单并按授权范围处理模拟退款。"
                self._release_task_hold(session, task)
                self._refresh_totals(session, task)
                self._transition(session, task, "running", task.message, "已受理生日安排取消", now)
                plan.status = "completed"
                plan.decision = decision
                plan.outcome_status = "completed"
                plan.outcome_code = None
                plan.outcome_message = task.message
                plan.operation_id = f"workflow-{_digest(task.id)[:32]}-cancel-{_digest(plan.id)[:8]}"
                plan.decided_at = now
                cancel_task_id = task.id

        if immediate_task_id:
            self.process_due()
            with self._session_factory() as session:
                plan = session.get(WorkflowPlan, plan_id)
                return self._decision_result(session, plan) if plan is not None else {"status": "completed"}
        if cancel_task_id:
            task = self.recover(cancel_task_id)
            with self._session_factory() as session:
                plan = session.get(WorkflowPlan, plan_id)
                result = self._decision_result(session, plan) if plan is not None else {"status": "completed"}
            result.update(task_id=task["id"], task_status=task["status"], state_version=task["state_version"])
            result["message"] = task["message"]
            return result
        with self._session_factory() as session:
            plan = session.get(WorkflowPlan, plan_id)
            return self._decision_result(session, plan) if plan is not None else {"status": "completed"}

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            tasks = session.scalars(
                select(WorkflowTask).order_by(WorkflowTask.created_at, WorkflowTask.id)
            ).all()
            return [self._task_dict(session, task) for task in tasks]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        if not task_id:
            return None
        with self._session_factory() as session:
            task = session.get(WorkflowTask, task_id)
            return self._task_dict(session, task) if task is not None else None

    def process_due(self, as_of: datetime | str | None = None) -> list[dict[str, Any]]:
        effective = _parse_aware_datetime(as_of) if as_of is not None else None
        with self._session_factory() as session:
            if effective is None:
                effective = _task_now(session)
            task_ids = session.scalars(
                select(WorkflowTask.id)
                .where(
                    WorkflowTask.status.in_(["scheduled", "running", "unknown"]),
                    WorkflowTask.execute_at <= effective,
                )
                .order_by(WorkflowTask.execute_at, WorkflowTask.created_at, WorkflowTask.id)
            ).all()

        changed: list[dict[str, Any]] = []
        for task_id in task_ids:
            previous_version: int | None = None
            while True:
                task_result, progressed = self._process_one_step(task_id, effective)
                if task_result is None:
                    break
                if previous_version != task_result["state_version"]:
                    changed.append(task_result)
                    previous_version = task_result["state_version"]
                if not progressed:
                    break
                if task_result["status"] in {"failed", "partial", "refund_pending", "unknown", "expired"}:
                    break
            current = self.get_task(task_id)
            if current and current.get("failure_code") and current.get("status") != "expired":
                self._compensate_task(task_id, explicit=False)
        return changed

    def recover(self, task_id: str) -> dict[str, Any]:
        if not task_id:
            raise BankError("TASK_NOT_FOUND", "请提供要核对的生日安排 task_id。")
        account_ids: set[str] = set()
        with self._session_factory() as session:
            task = session.get(WorkflowTask, task_id)
            if task is None:
                raise BankError("TASK_NOT_FOUND", "未找到该生日安排。")
            account_ids.add(task.account_id)
            account_ids.update(
                session.scalars(
                    select(WorkflowStep.merchant_account_id).where(WorkflowStep.task_id == task_id)
                ).all()
            )
        with self._transaction() as session:
            self._lock_accounts(session, account_ids)
            task = self._get_task_for_update(session, task_id)
            if task is None:
                raise BankError("TASK_NOT_FOUND", "未找到该生日安排。")
            steps = self._steps_for_update(session, task_id)
            reconciled = False
            for step in steps:
                order = self._order_for_update(session, task_id, step.step_index)
                if order is None:
                    continue
                if order.receipt_lost or step.status == "unknown":
                    # Recovery only looks up the original persisted order. It never
                    # creates a merchant order or authorizes a new consumption.
                    order.receipt_lost = False
                    order.updated_at = _utcnow()
                    step.status = "confirmed"
                    step.message = "已查询原订单并核对付款记录，没有重复下单或扣款。"
                    step.updated_at = _utcnow()
                    reconciled = True
            if reconciled:
                self._refresh_totals(session, task)
                if task.status == "unknown":
                    task.status = "running"
                task.message = "原商户订单已核对；安排仍只表示模拟订单确认，不代表配送。"
                self._transition(session, task, task.status, task.message, "原商户订单已核对", _utcnow())
            self._release_task_hold(session, task) if task.cancel_requested or task.failure_code else None
            if task.cancel_requested or task.failure_code or task.status == "refund_pending":
                self._finish_compensation_state(session, task, _utcnow())

        if self._task_needs_refund(task_id):
            self._compensate_task(task_id, explicit=True)
        result = self.get_task(task_id)
        if result is None:
            raise BankError("TASK_NOT_FOUND", "未找到该生日安排。")
        return result

    def completed_events(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            events = session.scalars(
                select(WorkflowEvent).order_by(WorkflowEvent.created_at, WorkflowEvent.id)
            ).all()
            return [_event_dict(event) for event in events]

    def _process_one_step(self, task_id: str, as_of: datetime) -> tuple[dict[str, Any] | None, bool]:
        account_ids: set[str] = set()
        with self._session_factory() as session:
            task = session.get(WorkflowTask, task_id)
            if task is None:
                return None, False
            account_ids.add(task.account_id)
            account_ids.update(
                session.scalars(
                    select(WorkflowStep.merchant_account_id).where(WorkflowStep.task_id == task_id)
                ).all()
            )

        with self._transaction() as session:
            self._lock_accounts(session, account_ids)
            task = self._get_task_for_update(session, task_id, skip_locked=True)
            if task is None:
                return None, False
            if task.status not in {"scheduled", "running", "unknown"}:
                return self._task_dict(session, task), False
            now = _as_utc(as_of)
            if _as_utc(task.execute_at) > now:
                return self._task_dict(session, task), False

            steps = self._steps_for_update(session, task_id)
            orders = {
                step.step_index: self._order_for_update(session, task_id, step.step_index) for step in steps
            }
            if now > _as_utc(task.deadline_at):
                task.failure_code = "EXECUTION_WINDOW_EXPIRED"
                task.failure_message = "安排超过24小时执行窗口，未继续调用后续模拟商户。"
                task.message = task.failure_message
                for step in steps:
                    if step.status == "pending":
                        step.status = "failed"
                        step.message = task.failure_message
                        step.updated_at = _utcnow()
                self._release_task_hold(session, task)
                self._refresh_totals(session, task)
                if task.paid_minor:
                    self._transition(
                        session, task, "running", task.message, "生日安排执行窗口已过期", _utcnow()
                    )
                else:
                    self._transition(session, task, "expired", task.message, "生日安排已过期", _utcnow())
                return self._task_dict(session, task), False

            if task.cancel_requested or task.failure_code:
                self._release_task_hold(session, task)
                self._finish_compensation_state(session, task, _utcnow())
                return self._task_dict(session, task), False

            # Reconcile a lost receipt against the exact original order. The next
            # step may proceed only after this persisted order is found.
            for step in steps:
                order = orders[step.step_index]
                if step.status == "unknown" and order is not None:
                    if not self._order_has_payment(session, order):
                        task.message = "原订单存在但付款流水无法核对，已暂停且不会重复扣款。"
                        task.status = "unknown"
                        self._transition(
                            session, task, "unknown", task.message, "订单回执核对待处理", _utcnow()
                        )
                        return self._task_dict(session, task), False
                    order.receipt_lost = False
                    order.updated_at = _utcnow()
                    step.status = "confirmed"
                    step.message = "已查询原订单并核对付款流水，没有重复下单或扣款。"
                    step.updated_at = _utcnow()
                    task.status = "running"
                    task.message = step.message
                    self._refresh_totals(session, task)
                    self._transition(
                        session, task, "running", task.message, "已核对原商户订单", order.operation_id
                    )
                    return self._task_dict(session, task), True

            pending = next((step for step in steps if step.status == "pending"), None)
            if pending is None:
                if steps and all(step.status == "confirmed" for step in steps):
                    message = _completed_message(steps)
                    self._release_task_hold(session, task)
                    self._refresh_totals(session, task)
                    if task.status != "completed" or task.message != message:
                        task.message = message
                        self._transition(session, task, "completed", message, "模拟生日商户订单已确认")
                return self._task_dict(session, task), False

            existing_order = orders[pending.step_index]
            if existing_order is not None:
                if not self._order_has_payment(session, existing_order):
                    task.status = "unknown"
                    task.message = "查到原订单，但账务记录暂时不一致，已暂停且不会再次扣款。"
                    self._transition(session, task, "unknown", task.message, "原订单账务核对待处理")
                    return self._task_dict(session, task), False
                pending.order_id = existing_order.id
                pending.status = "confirmed"
                pending.message = "已查询原订单并核对付款记录，没有重复下单或扣款。"
                pending.updated_at = _utcnow()
                task.status = "running"
                task.message = pending.message
                self._refresh_totals(session, task)
                self._transition(
                    session, task, "running", task.message, "已核对原商户订单", existing_order.operation_id
                )
                return self._task_dict(session, task), True

            source = self._lock_accounts(session, account_ids)
            source_account = source.get(task.account_id)
            merchant_account = source.get(pending.merchant_account_id)
            merchant = session.get(WorkflowMerchant, pending.merchant_id)
            if (
                source_account is None
                or source_account.owner_id != DEMO_USER_ID
                or source_account.currency != "CNY"
                or merchant_account is None
                or merchant is None
                or merchant_account.owner_id != merchant.owner_id
                or merchant_account.currency != "CNY"
            ):
                pending.status = "failed"
                pending.message = "本人结算账户或模拟商户账户身份已变化，未付款。"
                pending.updated_at = _utcnow()
                task.failure_code = "ACCOUNT_NOT_FOUND"
                task.failure_message = pending.message
                task.message = pending.message
                self._release_task_hold(session, task)
                self._refresh_totals(session, task)
                self._transition(session, task, "running", task.message, "结算账户校验失败，停止后续商户")
                return self._task_dict(session, task), False

            if (
                task.total_minor > task.budget_minor
                or task.paid_minor + pending.total_minor > task.budget_minor
                or pending.total_minor > task.budget_minor
                or sum(step.total_minor for step in steps) != task.total_minor
            ):
                pending.status = "failed"
                pending.message = "订单金额与已确认预算不一致，未调用该商户。"
                pending.updated_at = _utcnow()
                task.failure_code = "BUDGET_MISMATCH"
                task.failure_message = pending.message
                task.message = pending.message
                self._release_task_hold(session, task)
                self._refresh_totals(session, task)
                self._transition(session, task, "running", task.message, "预算校验失败，停止后续商户")
                return self._task_dict(session, task), False

            if (
                self.fault_mode in {"cake_out_of_stock", "refund_failure"}
                and pending.product_id == "cake-basic"
            ):
                pending.status = "failed"
                pending.message = "云朵烘焙模拟库存不足；没有创建蛋糕订单或扣款。"
                pending.updated_at = _utcnow()
                task.failure_code = "OUT_OF_STOCK"
                task.failure_message = pending.message
                task.message = pending.message
                self._release_task_hold(session, task)
                self._refresh_totals(session, task)
                self._transition(session, task, "running", task.message, "模拟商户库存不足，停止后续执行")
                return self._task_dict(session, task), False

            if task.hold_id is None:
                raise BankError("HOLD_NOT_FOUND", "生日安排缺少已确认的资金预留，已停止付款。")
            operation_id = _payment_operation_id(task.id, pending.step_index)
            order_id = _order_id(task.id, pending.step_index)
            existing_order = session.get(WorkflowMerchantOrder, order_id)
            if existing_order is not None:
                # Stable business key is checked before any new debit. Normally the
                # order and ledger commit atomically; this is a defensive recovery.
                if not self._order_has_payment(session, existing_order):
                    task.status = "unknown"
                    task.message = "原订单账务结果无法确认，已停止且不会重试消费。"
                    self._transition(session, task, "unknown", task.message, "原订单账务核对待处理")
                    return self._task_dict(session, task), False
                pending.order_id = existing_order.id
                pending.status = "confirmed"
                pending.message = "已核对原订单及付款流水。"
                pending.updated_at = _utcnow()
                self._refresh_totals(session, task)
                self._transition(
                    session, task, "running", pending.message, "已核对原商户订单", existing_order.operation_id
                )
                return self._task_dict(session, task), True

            order = WorkflowMerchantOrder(
                id=order_id,
                business_key=f"{task.id}:{pending.step_index}",
                task_id=task.id,
                step_index=pending.step_index,
                product_id=pending.product_id,
                merchant_id=pending.merchant_id,
                merchant_name=pending.merchant_name,
                account_id=task.account_id,
                merchant_account_id=pending.merchant_account_id,
                amount_minor=pending.total_minor,
                currency="CNY",
                status="CONFIRMED",
                operation_id=operation_id,
                refund_operation_id=None,
                refunded_minor=0,
                refund_pending_minor=0,
                receipt_lost=False,
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
            session.add(order)
            from app.bank.reservations import consume_funds

            try:
                consume_funds(
                    session,
                    task.hold_id,
                    pending.merchant_account_id,
                    pending.total_minor,
                    operation_id,
                    f"模拟商户消费·{pending.merchant_name}",
                )
            except BankError as error:
                session.expunge(order)
                pending.status = "failed"
                pending.message = f"资金预留核验失败（{error.code}），未创建商户订单。"
                pending.updated_at = _utcnow()
                task.failure_code = error.code
                task.failure_message = pending.message
                task.message = pending.message
                self._release_task_hold(session, task)
                self._refresh_totals(session, task)
                self._transition(session, task, "running", task.message, "资金预留核验失败，停止后续商户")
                return self._task_dict(session, task), False
            lose_receipt = self.fault_mode == "response_lost" and not task.fault_response_lost_done
            if lose_receipt:
                task.fault_response_lost_done = True
                order.receipt_lost = True
                pending.status = "unknown"
                pending.message = (
                    "付款和原订单已持久化，但模拟商户回执丢失；下一轮将查询原订单。"
                    "这是模拟订单状态，不代表商品已配送。"
                )
                task.status = "unknown"
            else:
                pending.status = "confirmed"
                pending.message = f"{pending.merchant_name}模拟订单已确认，尚不表示商品已配送。"
                task.status = "running"
            pending.order_id = order.id
            pending.updated_at = _utcnow()
            order.updated_at = _utcnow()
            task.message = pending.message
            self._refresh_totals(session, task)
            self._transition(
                session,
                task,
                task.status,
                task.message,
                "模拟商户订单付款已记录" if not lose_receipt else "付款已记录，正在核对原商户订单",
                operation_id,
            )
            return self._task_dict(session, task), not lose_receipt

    def _compensate_task(self, task_id: str, *, explicit: bool) -> None:
        with self._session_factory() as session:
            task = session.get(WorkflowTask, task_id)
            if task is None:
                return
            account_ids = {task.account_id}
            account_ids.update(
                session.scalars(
                    select(WorkflowStep.merchant_account_id).where(WorkflowStep.task_id == task_id)
                ).all()
            )
        with self._session_factory() as session:
            indexes = session.scalars(
                select(WorkflowStep.step_index)
                .where(WorkflowStep.task_id == task_id)
                .order_by(WorkflowStep.step_index)
            ).all()

        for step_index in indexes:
            with self._transaction() as session:
                self._lock_accounts(session, account_ids)
                task = self._get_task_for_update(session, task_id, skip_locked=True)
                if task is None:
                    return
                step = self._get_step_for_update(session, task_id, step_index)
                order = self._order_for_update(session, task_id, step_index)
                if step is None or order is None or order.status in {"REFUNDED", "FAILED"}:
                    continue
                if order.status == "FULFILLED" or step.status == "fulfilled":
                    step.status = "fulfilled"
                    step.message = "该模拟订单已标记履约，按商户状态不可退款。"
                    step.updated_at = _utcnow()
                    self._refresh_totals(session, task)
                    continue
                if not step.cancel_supported:
                    order.status = "FULFILLED"
                    step.status = "fulfilled"
                    step.message = "当前模拟商户条款不支持取消，已付款金额保留并如实计入支出。"
                    order.updated_at = step.updated_at = _utcnow()
                    self._refresh_totals(session, task)
                    self._transition(session, task, "partial", step.message, "订单不可退款")
                    continue
                if order.refund_pending_minor and not explicit:
                    continue
                if order.refunded_minor >= order.amount_minor:
                    order.status = "REFUNDED"
                    step.status = "refunded"
                    continue

                if self.fault_mode == "refund_failure" and step.refund_attempts == 0 and not explicit:
                    step.refund_attempts += 1
                    order.status = "REFUND_PENDING"
                    order.refund_pending_minor = order.amount_minor - order.refunded_minor
                    step.status = "refund_pending"
                    step.message = "模拟退款暂时失败；资金仍在该模拟商户账户，可点击“核对处理进度”重试原订单退款。"
                    step.updated_at = order.updated_at = _utcnow()
                    self._refresh_totals(session, task)
                    task.message = step.message
                    self._transition(session, task, "refund_pending", task.message, "模拟商户退款待重试")
                    continue

                refund_amount = order.amount_minor - order.refunded_minor
                if refund_amount <= 0:
                    continue
                refund_operation = order.refund_operation_id or _refund_operation_id(task.id, step_index)
                try:
                    from app.bank.reservations import transfer_funds

                    transfer_funds(
                        session,
                        order.merchant_account_id,
                        order.account_id,
                        refund_amount,
                        refund_operation,
                        f"模拟商户退款·{order.merchant_name}",
                    )
                except BankError as error:
                    step.refund_attempts += 1
                    order.status = "REFUND_PENDING"
                    order.refund_pending_minor = refund_amount
                    step.status = "refund_pending"
                    step.message = f"模拟退款暂未完成（{error.code}）；资金仍归属模拟商户，可点击“核对处理进度”重试。"
                    step.updated_at = order.updated_at = _utcnow()
                    self._refresh_totals(session, task)
                    task.message = step.message
                    self._transition(session, task, "refund_pending", task.message, "模拟商户退款待重试")
                    continue

                order.refund_operation_id = refund_operation
                order.refunded_minor += refund_amount
                order.refund_pending_minor = 0
                order.status = "REFUNDED"
                order.updated_at = order.refunded_at = _utcnow()
                step.status = "refunded"
                step.refund_attempts += 1
                step.message = f"已按原订单退款{_format_cny(refund_amount)}，没有新增消费。"
                step.updated_at = _utcnow()
                self._refresh_totals(session, task)
                task.message = step.message
                self._transition(
                    session,
                    task,
                    "running",
                    task.message,
                    "模拟商户退款已入账",
                    refund_operation,
                )

        with self._transaction() as session:
            self._lock_accounts(session, account_ids)
            task = self._get_task_for_update(session, task_id)
            if task is None:
                return
            self._release_task_hold(session, task)
            self._refresh_totals(session, task)
            self._finish_compensation_state(session, task, _utcnow())

    def _finish_compensation_state(self, session: Session, task: WorkflowTask, now: datetime) -> None:
        self._refresh_totals(session, task)
        if task.refund_pending_minor:
            status = "refund_pending"
            message = (
                f"有{_format_cny(task.refund_pending_minor)}模拟退款待处理，资金仍归属对应模拟商户；"
                "点击“核对处理进度”可重试原订单退款，不会新增消费。"
            )
        elif task.net_spent_minor > 0:
            status = "partial"
            message = (
                f"安排已停止，已退款{_format_cny(task.refunded_minor)}，仍有"
                f"{_format_cny(task.net_spent_minor)}属于已履约或不可退款订单。"
            )
        elif task.cancel_requested:
            status = "cancelled"
            subject = task.title or "生日安排"
            message = f"{subject}已取消；所有已付款且可退的模拟订单均已退款，未产生净消费。"
        else:
            status = "failed"
            subject = task.title or "生日安排"
            message = f"{subject}未能完成；已释放剩余预留，并完成可退模拟订单的退款。"
        if task.status != status or task.message != message:
            task.message = message
            self._transition(session, task, status, message, _status_title(status), now)

    def _task_needs_refund(self, task_id: str) -> bool:
        with self._session_factory() as session:
            task = session.get(WorkflowTask, task_id)
            return bool(
                task and (task.cancel_requested or task.failure_code or task.status == "refund_pending")
            )

    def _refresh_totals(self, session: Session, task: WorkflowTask) -> None:
        from app.bank.models import FundHold

        orders = session.scalars(
            select(WorkflowMerchantOrder).where(WorkflowMerchantOrder.task_id == task.id)
        ).all()
        task.paid_minor = sum(order.amount_minor for order in orders)
        task.refunded_minor = sum(order.refunded_minor for order in orders)
        task.refund_pending_minor = sum(order.refund_pending_minor for order in orders)
        task.net_spent_minor = task.paid_minor - task.refunded_minor
        hold = session.get(FundHold, task.hold_id) if task.hold_id else None
        task.reserved_minor = hold.remaining_minor if hold is not None and hold.status == "active" else 0

    def _release_task_hold(self, session: Session, task: WorkflowTask) -> None:
        if not task.hold_id:
            task.reserved_minor = 0
            return
        from app.bank.models import FundHold
        from app.bank.reservations import release_funds

        hold = session.get(FundHold, task.hold_id)
        if hold is not None and hold.status == "active":
            release_funds(session, task.hold_id)
        self._refresh_totals(session, task)

    def _add_event(
        self,
        session: Session,
        task: WorkflowTask,
        title: str,
        text: str,
        created_at: datetime,
        operation_id: str | None = None,
    ) -> None:
        receipt = self._receipt(session, task)
        session.add(
            WorkflowEvent(
                id=f"workflow:{task.id}:{task.state_version}",
                task_id=task.id,
                state_version=task.state_version,
                kind="workflow",
                source="workflow",
                title=title,
                text=text,
                created_at=_as_utc(created_at),
                plan_id=task.plan_id,
                conversation_id=task.conversation_id,
                operation_id=operation_id,
                receipt_json=json.dumps(receipt, ensure_ascii=False, sort_keys=True),
            )
        )

    def _transition(
        self,
        session: Session,
        task: WorkflowTask,
        status: str,
        message: str,
        title: str,
        operation_id: str | None = None,
    ) -> None:
        task.status = status
        task.message = message
        task.state_version += 1
        task.updated_at = _utcnow()
        self._add_event(session, task, title, message, task.updated_at, operation_id)

    def _receipt(self, session: Session, task: WorkflowTask) -> dict[str, Any]:
        return {
            "task_id": task.id,
            "task_status": task.status,
            "state_version": task.state_version,
            "plan_id": task.plan_id,
            "related_plan_ids": json.loads(task.related_plan_ids_json or "[]"),
            "paid_minor": task.paid_minor,
            "refunded_minor": task.refunded_minor,
            "refund_pending_minor": task.refund_pending_minor,
            "net_spent_minor": task.net_spent_minor,
            "reserved_minor": task.reserved_minor,
            "message": task.message,
            "steps": [
                {
                    "step_id": step.id,
                    "merchant_name": step.merchant_name,
                    "product_name": step.product_name,
                    "status": step.status,
                    "order_id": step.order_id,
                    "total_minor": step.total_minor,
                }
                for step in session.scalars(
                    select(WorkflowStep)
                    .where(WorkflowStep.task_id == task.id)
                    .order_by(WorkflowStep.step_index)
                ).all()
            ],
        }

    def _task_dict(self, session: Session, task: WorkflowTask | None) -> dict[str, Any] | None:
        if task is None:
            return None
        plan = session.get(WorkflowPlan, task.plan_id)
        units = {item["product_id"]: item.get("unit", "份")
                 for item in json.loads(plan.items_json or "[]")} if plan else {}
        steps = session.scalars(
            select(WorkflowStep).where(WorkflowStep.task_id == task.id).order_by(WorkflowStep.step_index)
        ).all()
        result_steps = []
        for step in steps:
            order = session.get(WorkflowMerchantOrder, step.order_id) if step.order_id else None
            result_steps.append(
                {
                    "step_id": step.id,
                    "merchant_id": step.merchant_id,
                    "merchant_name": step.merchant_name,
                    "product_id": step.product_id,
                    "name": step.product_name,
                    "quantity": step.quantity,
                    "unit": units.get(step.product_id, "份"),
                    "unit_price_minor": step.unit_price_minor,
                    "total_minor": step.total_minor,
                    "status": step.status,
                    "order_id": order.id if order is not None else step.order_id,
                    "order_status": order.status if order is not None else None,
                    "operation_id": order.operation_id if order is not None else None,
                    "refund_operation_id": order.refund_operation_id if order is not None else None,
                    "refunded_minor": order.refunded_minor if order is not None else 0,
                    "refund_pending_minor": order.refund_pending_minor if order is not None else 0,
                    "message": step.message,
                }
            )
        return {
            "id": task.id,
            "title": task.title or f"给{task.recipient_label}的模拟生日安排",
            "recipient_label": task.recipient_label,
            "account_id": task.account_id,
            "budget_minor": task.budget_minor,
            "total_minor": task.total_minor,
            "reserved_minor": task.reserved_minor,
            "paid_minor": task.paid_minor,
            "refunded_minor": task.refunded_minor,
            "refund_pending_minor": task.refund_pending_minor,
            "net_spent_minor": task.net_spent_minor,
            "status": task.status,
            "execute_at": _iso_utc(task.execute_at),
            "deadline_at": _iso_utc(task.deadline_at),
            "hold_id": task.hold_id,
            "steps": result_steps,
            "conversation_id": task.conversation_id,
            "plan_id": task.plan_id,
            "related_plan_ids": json.loads(task.related_plan_ids_json or "[]"),
            "state_version": task.state_version,
            "message": task.message,
            "failure_code": task.failure_code,
        }

    def _plan_dict(self, plan: WorkflowPlan) -> dict[str, Any]:
        items = json.loads(plan.items_json or "[]")
        is_cancel = plan.action == "cancel"
        result: dict[str, Any] = {
            "id": plan.id,
            "kind": "workflow",
            "action": plan.action,
            "title": (
                f"取消{plan.title}"
                if is_cancel and plan.title
                else "取消模拟生日安排"
                if is_cancel
                else plan.title or f"给{plan.recipient_label}安排模拟生日商品"
            ),
            "description": (
                "确认后会核对原模拟商户订单，只对可取消且已付款的订单尝试原单退款；履约或不可退金额会保留在实际支出中。"
                if is_cancel
                else (
                    f"{plan.title}，预算上限{_format_cny(plan.budget_minor or 0)}，"
                    f"实际报价{_format_cny(plan.total_minor or 0)}。确认后先预留实际报价，再按顺序提交模拟订单；商户确认不代表配送。"
                    if plan.title
                    else (
                    f"为{plan.recipient_label}安排模拟生日商品，预算上限{_format_cny(plan.budget_minor or 0)}，"
                    f"实际报价{_format_cny(plan.total_minor or 0)}。确认后先预留实际报价，再按计划逐项提交模拟订单；商户确认不代表配送。"
                    )
                )
            ),
            "status": plan.status,
            "expires_at": _iso_utc(plan.expires_at),
            "conversation_id": plan.conversation_id,
            "task_id": plan.task_id,
            "account_id": plan.account_id,
            "recipient_label": plan.recipient_label,
            "budget_minor": plan.budget_minor,
            "total_minor": plan.total_minor,
            "reserved_minor": plan.total_minor if plan.action == "arrange" else 0,
            "execute_immediately": bool(plan.execute_immediately),
            "execute_at": _iso_utc(plan.execute_at) if plan.execute_at is not None else None,
            "deadline_at": _iso_utc(plan.deadline_at) if plan.deadline_at is not None else None,
            "items": [
                {
                    "product_id": row["product_id"],
                    "merchant_name": row["merchant_name"],
                    "name": row["name"],
                    "unit": row.get("unit", "份"),
                    "description": row.get("description", ""),
                    "cancel_supported": row["cancel_supported"],
                    "quantity": row["quantity"],
                    "unit_price_minor": row["unit_price_minor"],
                    "total_minor": row["total_minor"],
                }
                for row in items
            ],
            "cancellation_notice": (
                "取消只核对原订单并处理可退金额，不新增预留或消费。"
                if is_cancel else
                "确认后预留实际报价；商户订单状态不代表配送，退款按每笔原订单的商户状态核对，不承诺所有订单可退。"
            ),
        }
        if plan.operation_id is not None:
            result["operation_id"] = plan.operation_id
        revision = json.loads(plan.revision_json or "{}")
        if revision:
            result["revision"] = revision
        return result

    def _decision_result(self, session: Session, plan: WorkflowPlan | None) -> dict[str, Any]:
        if plan is None:
            return {"status": "declined", "message": "生日安排计划不存在。"}
        result: dict[str, Any] = {
            "status": plan.outcome_status or "declined",
            "message": plan.outcome_message or "生日安排计划已处理。",
        }
        if plan.outcome_code is not None:
            result["code"] = plan.outcome_code
        if plan.operation_id is not None:
            result["operation_id"] = plan.operation_id
        if plan.conversation_id is not None:
            result["conversation_id"] = plan.conversation_id
        if plan.task_id is not None:
            task = session.get(WorkflowTask, plan.task_id)
            if task is not None:
                result.update(
                    task_id=task.id,
                    task_status=task.status,
                    state_version=task.state_version,
                    message=task.message,
                    receipt=self._receipt(session, task),
                )
        return result

    def _step_snapshots(self, session: Session, task_id: str) -> list[dict[str, Any]]:
        task = session.get(WorkflowTask, task_id)
        plan = session.get(WorkflowPlan, task.plan_id) if task else None
        units = {item["product_id"]: item.get("unit", "份")
                 for item in json.loads(plan.items_json or "[]")} if plan else {}
        return [
            {
                "product_id": step.product_id,
                "merchant_id": step.merchant_id,
                "merchant_name": step.merchant_name,
                "merchant_account_id": step.merchant_account_id,
                "name": step.product_name,
                "quantity": step.quantity,
                "unit": units.get(step.product_id, "份"),
                "unit_price_minor": step.unit_price_minor,
                "total_minor": step.total_minor,
                "cancel_supported": step.cancel_supported,
                "catalog_version": step.catalog_version,
            }
            for step in session.scalars(
                select(WorkflowStep).where(WorkflowStep.task_id == task_id).order_by(WorkflowStep.step_index)
            ).all()
        ]

    def _account_ids_for_plan(self, session: Session, plan: WorkflowPlan) -> set[str]:
        if plan.action == "arrange":
            ids = {plan.account_id} if plan.account_id else set()
            ids.update(row["merchant_account_id"] for row in json.loads(plan.items_json or "[]"))
            return ids
        ids: set[str] = {plan.account_id} if plan.account_id else set()
        if plan.task_id:
            ids.update(
                session.scalars(
                    select(WorkflowStep.merchant_account_id).where(WorkflowStep.task_id == plan.task_id)
                ).all()
            )
        return ids

    def _quote_still_current(self, session: Session, snapshots: list[dict[str, Any]]) -> bool:
        for snapshot in snapshots:
            product = session.get(WorkflowCatalogItem, snapshot["product_id"])
            if (
                product is None
                or not product.active
                or product.version != snapshot["catalog_version"]
                or product.price_minor != snapshot["unit_price_minor"]
                or product.merchant_id != snapshot["merchant_id"]
                or product.merchant_name != snapshot["merchant_name"]
                or product.name != snapshot["name"]
                or product.cancel_supported != snapshot["cancel_supported"]
            ):
                return False
        return True

    @staticmethod
    def _lock_accounts(session: Session, account_ids: Sequence[str] | set[str]) -> dict[str, Account]:
        from app.bank.reservations import lock_accounts

        ids = sorted(set(account_ids))
        if not ids:
            raise BankError("ACCOUNT_NOT_FOUND", "联动安排缺少结算账户。")
        return lock_accounts(session, *ids)

    @staticmethod
    def _locked_account_rows(session: Session, account_ids: Sequence[str] | set[str]) -> list[Account]:
        return list(WorkflowService._lock_accounts(session, account_ids).values())

    def _get_plan_for_update(self, session: Session, plan_id: str) -> WorkflowPlan | None:
        return session.scalar(
            select(WorkflowPlan)
            .where(WorkflowPlan.id == plan_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def _get_task_for_update(
        self, session: Session, task_id: str, *, skip_locked: bool = False
    ) -> WorkflowTask | None:
        query = (
            select(WorkflowTask).where(WorkflowTask.id == task_id).with_for_update(skip_locked=skip_locked)
        )
        return session.scalar(query.execution_options(populate_existing=True))

    def _steps_for_update(self, session: Session, task_id: str) -> list[WorkflowStep]:
        return session.scalars(
            select(WorkflowStep)
            .where(WorkflowStep.task_id == task_id)
            .order_by(WorkflowStep.step_index)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()

    def _get_step_for_update(self, session: Session, task_id: str, step_index: int) -> WorkflowStep | None:
        return session.scalar(
            select(WorkflowStep)
            .where(WorkflowStep.task_id == task_id, WorkflowStep.step_index == step_index)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def _order_for_update(
        self, session: Session, task_id: str, step_index: int
    ) -> WorkflowMerchantOrder | None:
        return session.scalar(
            select(WorkflowMerchantOrder)
            .where(
                WorkflowMerchantOrder.task_id == task_id,
                WorkflowMerchantOrder.step_index == step_index,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    @staticmethod
    def _order_has_payment(session: Session, order: WorkflowMerchantOrder) -> bool:
        rows = session.scalars(
            select(LedgerTransaction).where(LedgerTransaction.operation_id == order.operation_id)
        ).all()
        description = f"模拟商户消费·{order.merchant_name}"
        actual = {(row.account_id, row.amount_minor, row.description) for row in rows}
        expected = {
            (order.account_id, -order.amount_minor, description),
            (order.merchant_account_id, order.amount_minor, description),
        }
        return len(rows) == 2 and actual == expected

    @staticmethod
    def _plans_for_conversation(session: Session, conversation_id: str) -> list[WorkflowPlan]:
        query = (
            select(WorkflowPlan)
            .where(WorkflowPlan.conversation_id == conversation_id)
            .order_by(WorkflowPlan.created_at.desc(), WorkflowPlan.id.desc())
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            query = query.with_for_update()
        return session.scalars(query).all()

    @staticmethod
    def _invalidate_pending_in_session(session: Session, conversation_id: str, now: datetime) -> int:
        plans = session.scalars(
            select(WorkflowPlan)
            .where(WorkflowPlan.conversation_id == conversation_id, WorkflowPlan.status == "pending")
            .with_for_update()
        ).all()
        for plan in plans:
            plan.status = "invalidated"
            plan.outcome_status = "declined"
            plan.outcome_message = "生日安排信息已更新，旧计划已失效；账户资金未变化。"
            plan.decided_at = now
        return len(plans)

    @staticmethod
    def _decline_plan(plan: WorkflowPlan, now: datetime, code: str, message: str) -> None:
        plan.status = "declined"
        plan.decision = "approve"
        plan.outcome_status = "declined"
        plan.outcome_code = code
        plan.outcome_message = message
        plan.decided_at = now

    @staticmethod
    def _mark_expired(plan: WorkflowPlan, now: datetime, decision: str | None = None) -> None:
        plan.status = "expired"
        plan.outcome_status = "declined"
        plan.outcome_code = "PLAN_EXPIRED"
        plan.outcome_message = "生日安排预览已过期，未预留资金或调用商户。"
        plan.decision = decision
        plan.decided_at = now


def _plan_revision(
    source: WorkflowPlan,
    target: dict[str, Any],
    session: Session,
) -> dict[str, Any]:
    source_items = json.loads(source.items_json or "[]")

    def item_summary(rows: list[dict[str, Any]]) -> str:
        return "、".join(f"{row.get('name') or row['product_id']} × {row['quantity']}" for row in rows)

    def account_summary(account_id: str | None) -> str:
        account = session.get(Account, account_id) if account_id else None
        return account.name if account is not None else (account_id or "未设置")

    before_fields = {
        "title": source.title,
        "recipient_label": source.recipient_label,
        "budget_minor": source.budget_minor,
        "account_id": source.account_id,
    }
    after_fields = {key: target[key] for key in before_fields}
    changes: list[dict[str, str]] = []
    labels = {
        "title": "安排名称",
        "recipient_label": "安排对象",
        "budget_minor": "预算",
        "account_id": "付款账户",
    }

    def value_summary(field: str, value: Any) -> str:
        if field == "title":
            return value or "自动生成"
        if field == "budget_minor":
            return _format_cny(value or 0)
        if field == "account_id":
            return account_summary(value)
        return str(value or "")

    for field, label in labels.items():
        before, after = before_fields[field], after_fields[field]
        if before != after:
            before_text = value_summary(field, before)
            after_text = value_summary(field, after)
            changes.append({
                "field": field,
                "label": label,
                "before": before_text,
                "after": after_text,
                "summary": f"{label}：{before_text} → {after_text}",
            })

    source_immediate = bool(source.execute_immediately)
    target_immediate = bool(target["execute_immediately"])
    source_execute_at = _as_utc(source.execute_at) if source.execute_at is not None else None
    target_execute_at = target["execute_at"]
    if source_immediate != target_immediate or (
        not source_immediate and source_execute_at != target_execute_at
    ):
        before_text = "确认后立即开始" if source_immediate else _iso_utc(source_execute_at)
        after_text = "确认后立即开始" if target_immediate else _iso_utc(target_execute_at)
        changes.append({
            "field": "execute_at",
            "label": "执行时间",
            "before": before_text,
            "after": after_text,
            "summary": f"执行时间：{before_text} → {after_text}",
        })

    before_signature = [
        (row["product_id"], row["quantity"], row.get("unit_price_minor"), row.get("catalog_version"))
        for row in source_items
    ]
    after_signature = [
        (row["product_id"], row["quantity"], row.get("unit_price_minor"), row.get("catalog_version"))
        for row in target["items"]
    ]
    if before_signature != after_signature:
        before_text = item_summary(source_items)
        after_text = item_summary(target["items"])
        changes.append({
            "field": "items",
            "label": "服务",
            "before": before_text,
            "after": after_text,
            "summary": f"服务：{before_text} → {after_text}",
        })

    return {
        "source_plan_id": source.id,
        "before_total_minor": source.total_minor,
        "after_total_minor": sum(row["total_minor"] for row in target["items"]),
        "changes": changes,
    }


def _normalize_items(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if items is None:
        items = [
            {"product_id": definition["id"], "quantity": 1}
            for definition in CATALOG
        ]
    if not isinstance(items, list) or not 1 <= len(items) <= 10:
        raise BankError("INVALID_ITEMS", "一次安排须选择1至10种不同的模拟服务。")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"product_id", "quantity"}:
            raise BankError("INVALID_ITEMS", "每项商品只能填写 product_id 和 quantity。")
        product_id = item.get("product_id")
        quantity = item.get("quantity")
        if not isinstance(product_id, str) or not product_id.strip() or len(product_id) > 80:
            raise BankError("PRODUCT_NOT_FOUND", "请选择有效的模拟服务。")
        if product_id in seen:
            raise BankError("DUPLICATE_ITEM", "同一商品不能重复添加，请调整其数量。")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 5:
            raise BankError("INVALID_QUANTITY", "每种商品数量须为1至5份。")
        seen.add(product_id)
        normalized.append({"product_id": product_id, "quantity": quantity})
    return normalized


def _validate_service_fields(
    *,
    name: str,
    merchant_name: str,
    price_minor: int,
    unit: str,
    description: str,
    cancel_supported: bool,
    client_request_id: str | None,
) -> dict[str, Any]:
    def label(value: Any, field: str, maximum: int) -> str:
        if not isinstance(value, str):
            raise BankError("INVALID_SERVICE", f"{field}必须是文字。")
        result = value.strip()
        if not result or len(result) > maximum:
            raise BankError("INVALID_SERVICE", f"{field}长度须为1至{maximum}字。")
        return result

    normalized_name = label(name, "服务名称", 60)
    normalized_merchant = label(merchant_name, "模拟商户名称", 60)
    normalized_unit = label(unit, "计价单位", 40)
    if not isinstance(description, str) or len(description) > 500:
        raise BankError("INVALID_SERVICE", "服务说明最多500字。")
    if isinstance(price_minor, bool) or not isinstance(price_minor, int):
        raise BankError("INVALID_SERVICE_PRICE", "服务单价必须是整数分。")
    if not 1 <= price_minor <= MAX_SAFE_JSON_INTEGER:
        raise BankError("INVALID_SERVICE_PRICE", "服务单价须大于零且不超过安全整数范围。")
    if not isinstance(cancel_supported, bool):
        raise BankError("INVALID_SERVICE", "取消规则必须选择可取消或不可取消。")
    if client_request_id is not None:
        if not isinstance(client_request_id, str) or not client_request_id.strip() or len(client_request_id) > 120:
            raise BankError("INVALID_REQUEST_ID", "服务请求标识须为1至120字。")
        client_request_id = client_request_id.strip()
    return {
        "name": normalized_name,
        "merchant_name": normalized_merchant,
        "price_minor": price_minor,
        "unit": normalized_unit,
        "description": description.strip(),
        "cancel_supported": cancel_supported,
        "client_request_id": client_request_id,
    }


def _task_now(session: Session) -> datetime:
    from app.task_clock import task_now

    return task_now(session)


def _parse_aware_datetime(value: datetime | str | None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            iso_value = value[:-1] + "+00:00" if value.endswith("Z") else value
            parsed = datetime.fromisoformat(iso_value)
        except ValueError as error:
            raise BankError("INVALID_EXECUTE_AT", "执行时间必须是带时区的有效ISO日期时间。") from error
    else:
        raise BankError("INVALID_EXECUTE_AT", "执行时间必须是带时区的有效ISO日期时间。")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BankError("INVALID_EXECUTE_AT", "执行时间必须包含时区，不能使用模糊或本地时间。")
    return parsed.astimezone(UTC)


def _as_utc(value: datetime) -> datetime:
    # SQLite drops timezone metadata on round trips. Stored values are always
    # written in UTC, so a naive value read from that backend means UTC here.
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:40]


def _step_id(task_id: str, step_index: int) -> str:
    return f"wf-step-{_digest(task_id)}-{step_index}"


def _order_id(task_id: str, step_index: int) -> str:
    return f"wf-order-{_digest(task_id)}-{step_index}"


def _payment_operation_id(task_id: str, step_index: int) -> str:
    return f"wf-pay-{_digest(task_id)}-{step_index}"


def _refund_operation_id(task_id: str, step_index: int) -> str:
    return f"wf-refund-{_digest(task_id)}-{step_index}"


def _format_cny(amount_minor: int) -> str:
    return f"¥{amount_minor // 100:,}.{amount_minor % 100:02d}"


def _product_dict(product: WorkflowCatalogItem) -> dict[str, Any]:
    return {
        "id": product.id,
        "merchant_id": product.merchant_id,
        "merchant_name": product.merchant_name,
        "name": product.name,
        "price_minor": product.price_minor,
        "currency": product.currency,
        "version": product.version,
        "cancel_supported": product.cancel_supported,
        "unit": product.unit,
        "description": product.description,
        "active": product.active,
    }


def _event_dict(event: WorkflowEvent) -> dict[str, Any]:
    try:
        receipt = json.loads(event.receipt_json)
    except (TypeError, ValueError):
        receipt = {}
    return {
        "id": event.id,
        "kind": event.kind,
        "source": event.source,
        "title": event.title,
        "text": event.text,
        "created_at": _iso_utc(event.created_at),
        "plan_id": event.plan_id,
        "conversation_id": event.conversation_id,
        "operation_id": event.operation_id,
        "receipt": receipt,
    }


def _completed_message(steps: Sequence[WorkflowStep]) -> str:
    descriptions = "、".join(f"{step.merchant_name}·{step.product_name}" for step in steps)
    return f"{descriptions}的模拟订单均已确认；这是模拟商户订单确认，不表示商品已配送。"


def _status_title(status: str) -> str:
    return {
        "cancelled": "模拟安排已取消",
        "failed": "模拟安排失败且退款已核对",
        "partial": "模拟安排部分不可退款",
        "refund_pending": "模拟商户退款待处理",
    }.get(status, "模拟安排状态已更新")
