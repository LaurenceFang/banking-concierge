"""Service for AA collection requests and authorized scheduled transfers."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.bank.errors import BankError
from app.bank.models import Account, FundHold, Recipient, User
from app.bank.reservations import (
    available_balance,
    consume_funds,
    lock_accounts,
    release_funds,
    reserve_funds,
    transfer_funds,
)
from app.task_clock import TaskClock, task_now

from .models import AAParticipant, AATask, Base, ScheduledTransfer, TaskActionPlan, TaskEvent

DEMO_USER_ID = "demo-user-001"
MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991
PLAN_TTL = timedelta(minutes=10)
MAX_SCHEDULE_AHEAD = timedelta(days=365)
AUTHORIZATION_WINDOW = timedelta(hours=24)
SHANGHAI = ZoneInfo("Asia/Shanghai")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# The API contract calls the serialized action a TaskAction. Keeping this alias
# structural lets FastAPI and existing action routers use the same plain mapping
# shape as cards and wealth.
TaskAction = dict[str, Any]


class TasksService:
    """Persist task plans and money effects in the same database transaction."""

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
        # task_clock has its own metadata. Initialize its durable offset table
        # against this same database, then leave clock ownership to the app.
        clock = TaskClock(database_url)
        clock.close()

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
        if self.dialect_name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        with self._session_factory() as session, session.begin():
            self._begin_write(session)
            yield session

    def _lock_conversation(self, session: Session, conversation_id: str) -> None:
        if self.dialect_name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:conversation_id, 0))"),
                {"conversation_id": f"tasks:{DEMO_USER_ID}:{conversation_id}"},
            )

    def seed_demo(self) -> None:
        """Tasks have no automatic fixtures; in particular, never seed completions."""

    def create_action_plan(
        self,
        action: str,
        conversation_id: str,
        *,
        title: str | None = None,
        account_id: str | None = None,
        total_minor: int | None = None,
        participant_ids: Sequence[str] | None = None,
        recipient_id: str | None = None,
        amount_minor: int | None = None,
        execute_at: datetime | str | None = None,
        task_id: str | None = None,
    ) -> TaskAction:
        if action not in {"create_aa", "cancel_aa", "schedule_transfer", "cancel_schedule"}:
            raise BankError("INVALID_ACTION", "任务操作类型无效。")
        _validate_conversation_id(conversation_id)
        now_real = _utcnow()
        plan_id = f"task-plan-{uuid4()}"
        normalized_title = _validate_title(title)

        with self._transaction() as session:
            now_task = _aware(task_now(session))
            values: dict[str, Any] = {
                "account_id": None,
                "total_minor": None,
                "collectable_minor": None,
                "participants": None,
                "recipient_id": None,
                "destination_account_id": None,
                "recipient_name": None,
                "amount_minor": None,
                "execute_at": None,
                "deadline_at": None,
                "reserved_minor": None,
                "task_version": None,
            }
            generated_task_id = _validated_task_id(task_id) if task_id is not None else f"task-{uuid4()}"
            description: str
            default_title: str
            if action in {"create_aa", "schedule_transfer"}:
                collision = (
                    session.get(AATask, generated_task_id) is not None
                    or session.get(ScheduledTransfer, generated_task_id) is not None
                    or session.scalar(
                        select(TaskActionPlan.id).where(
                            TaskActionPlan.owner_id == DEMO_USER_ID,
                            TaskActionPlan.task_id == generated_task_id,
                            TaskActionPlan.status.in_(("pending", "completed")),
                        )
                    )
                    is not None
                )
                if collision:
                    raise BankError("TASK_ID_CONFLICT", "该任务标识已使用，请重新发起。")

            if action == "create_aa":
                if not isinstance(account_id, str) or not account_id:
                    raise BankError("INVALID_ACTION_ARGUMENTS", "AA收款需要指定我方收款账户。")
                if any(value is not None for value in (recipient_id, amount_minor, execute_at)):
                    raise BankError("INVALID_ACTION_ARGUMENTS", "AA收款不需要指定单一收款人、单笔金额或执行时间。")
                _validate_amount(total_minor)
                ordered_ids = _validate_participant_ids(participant_ids)
                # Account locks precede any task or plan lock. AA creation only
                # checks the current-user source account; it moves no money.
                accounts = lock_accounts(session, account_id or "")
                source = accounts[account_id or ""]
                _require_demo_account(source)
                participants = _resolve_participants(session, ordered_ids)
                shares = _split_amount(total_minor or 0, participants)
                serialized_participants = []
                for entry, share in zip(participants, shares, strict=True):
                    serialized_participants.append(
                        {
                            "id": entry["id"],
                            "name": entry["name"],
                            "destination_account_id": entry["destination_account_id"],
                            "share_minor": share,
                            "status": "self" if entry["id"] == "self" else ("paid" if share == 0 else "pending"),
                        }
                    )
                self_share = next(item["share_minor"] for item in serialized_participants if item["id"] == "self")
                values.update(
                    account_id=account_id,
                    total_minor=total_minor,
                    collectable_minor=total_minor - self_share,
                    participants=serialized_participants,
                )
                default_title = normalized_title or "AA收款"
                description = (
                    f"确认后会创建{len(ordered_ids)}人AA收款请求，总额{_format_cny(total_minor)}；"
                    "创建请求不会扣款，也不会把待付款记成到账。"
                )
            elif action == "schedule_transfer":
                if total_minor is not None or participant_ids is not None:
                    raise BankError("INVALID_ACTION_ARGUMENTS", "定时转账不需要提供AA总额或参与者。")
                if not isinstance(recipient_id, str) or not recipient_id:
                    raise BankError("INVALID_ACTION_ARGUMENTS", "定时转账需要指定已保存的收款人。")
                _validate_amount(amount_minor)
                recipient = _validated_recipient(session, recipient_id)
                if not isinstance(account_id, str) or not account_id:
                    raise BankError("INVALID_ACTION_ARGUMENTS", "定时转账需要指定付款账户。")
                accounts = lock_accounts(session, account_id, recipient.destination_account_id)
                source = accounts[account_id]
                _require_demo_account(source)
                _require_external_recipient(recipient, accounts[recipient.destination_account_id])
                when = _parse_execute_at(execute_at)
                if when <= now_task:
                    raise BankError("INVALID_EXECUTE_AT", "执行时间必须晚于当前模拟时间。")
                if when > now_task + MAX_SCHEDULE_AHEAD:
                    raise BankError("INVALID_EXECUTE_AT", "执行时间不能超过当前模拟时间365天。")
                if available_balance(session, source) < (amount_minor or 0):
                    raise BankError("INSUFFICIENT_BALANCE", "可用余额不足，无法按该金额安排转账。")
                deadline = when + AUTHORIZATION_WINDOW
                values.update(
                    account_id=account_id,
                    recipient_id=recipient.id,
                    destination_account_id=recipient.destination_account_id,
                    recipient_name=recipient.name,
                    amount_minor=amount_minor,
                    execute_at=when,
                    deadline_at=deadline,
                    reserved_minor=amount_minor,
                )
                default_title = normalized_title or f"定时转账给{recipient.name}"
                description = (
                    f"确认后预留{_format_cny(amount_minor or 0)}，计划于{_format_shanghai(when)}转给"
                    f"{recipient.name}；执行窗口至{_format_shanghai(deadline)}。"
                )
            else:
                if any(value is not None for value in (account_id, total_minor, participant_ids, recipient_id, amount_minor, execute_at)):
                    raise BankError("INVALID_ACTION_ARGUMENTS", "取消任务只需要提供task_id。")
                target_id = _validated_task_id(task_id)
                aa = session.scalar(
                    select(AATask).where(AATask.id == target_id, AATask.owner_id == DEMO_USER_ID)
                )
                schedule = session.scalar(
                    select(ScheduledTransfer).where(
                        ScheduledTransfer.id == target_id,
                        ScheduledTransfer.owner_id == DEMO_USER_ID,
                    )
                )
                if aa is None and schedule is None:
                    raise BankError("TASK_NOT_FOUND", "未找到当前演示用户的任务。")
                if action == "cancel_aa":
                    if schedule is not None:
                        raise BankError("TASK_KIND_MISMATCH", "该任务不是AA收款请求。")
                    assert aa is not None
                    lock_accounts(session, aa.account_id)
                    aa = _lock_aa(session, target_id)
                    if aa.status == "cancelled":
                        current = _aa_dict(session, aa)
                        default_title = normalized_title or f"取消{aa.title}"
                        description = "该AA请求已经取消，重复确认不会改变已收款记录。"
                    elif aa.status == "completed":
                        raise BankError("TASK_NOT_CANCELLABLE", "该AA请求已全部收款，不能取消。")
                    else:
                        current = _aa_dict(session, aa)
                        default_title = normalized_title or f"取消{aa.title}"
                        description = (
                            f"确认后停止{aa.title}的未付款部分；已收款{_format_cny(aa.collected_minor)}会保留，不会退款。"
                        )
                    values.update(
                        account_id=aa.account_id,
                        total_minor=aa.total_minor,
                        collectable_minor=aa.collectable_minor,
                        participants=current["participants"],
                        task_version=aa.state_version,
                    )
                    task_status = aa.status
                else:
                    if aa is not None:
                        raise BankError("TASK_KIND_MISMATCH", "该任务不是定时转账。")
                    assert schedule is not None
                    lock_accounts(session, schedule.account_id)
                    schedule = _lock_schedule(session, target_id)
                    if schedule.status == "cancelled":
                        current = _schedule_dict(session, schedule, _hold_remaining(session, schedule.hold_id))
                        description = "该定时转账已经撤销，重复确认不会重复释放资金。"
                    elif schedule.status != "scheduled":
                        raise BankError("TASK_NOT_CANCELLABLE", "该定时转账已执行或已结束，不能撤销。")
                    else:
                        current = _schedule_dict(session, schedule, _hold_remaining(session, schedule.hold_id))
                        description = (
                            f"确认后撤销转给{schedule.recipient_name}的定时转账，并释放尚未使用的"
                            f"{_format_cny(schedule.reserved_minor)}预留资金。"
                        )
                    default_title = normalized_title or f"撤销转给{schedule.recipient_name}的定时转账"
                    values.update(
                        account_id=schedule.account_id,
                        recipient_id=schedule.recipient_id,
                        destination_account_id=schedule.destination_account_id,
                        recipient_name=schedule.recipient_name,
                        amount_minor=schedule.amount_minor,
                        execute_at=_aware(schedule.execute_at),
                        deadline_at=_aware(schedule.deadline_at),
                        reserved_minor=schedule.reserved_minor,
                        task_version=schedule.state_version,
                    )
                    task_status = schedule.status
                # ``current`` is used only to validate and build a fresh preview.
                del current
                if task_status not in {"scheduled", "awaiting_payment", "partial", "cancelled"}:
                    raise BankError("TASK_NOT_CANCELLABLE", "该任务当前状态不能取消。")

            self._lock_conversation(session, conversation_id)
            _invalidate_pending_in_session(session, conversation_id, now_real)
            session.flush()
            plan = TaskActionPlan(
                id=plan_id,
                owner_id=DEMO_USER_ID,
                action=action,
                task_id=generated_task_id,
                title=default_title,
                description=description,
                status="pending",
                conversation_id=conversation_id,
                created_at=now_real,
                expires_at=now_real + PLAN_TTL,
                **values,
            )
            session.add(plan)
            session.flush()
            return _action_dict(plan)

    def list_aa(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(AATask)
                .where(AATask.owner_id == DEMO_USER_ID)
                .order_by(AATask.created_at.desc(), AATask.id)
            ).all()
            return [_aa_dict(session, row) for row in rows]

    def list_schedules(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(ScheduledTransfer)
                .where(ScheduledTransfer.owner_id == DEMO_USER_ID)
                .order_by(ScheduledTransfer.execute_at, ScheduledTransfer.id)
            ).all()
            return [
                _schedule_dict(session, row, _hold_remaining(session, row.hold_id)) for row in rows
            ]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        if not isinstance(task_id, str) or not task_id:
            return None
        with self._session_factory() as session:
            aa = session.scalar(
                select(AATask).where(AATask.id == task_id, AATask.owner_id == DEMO_USER_ID)
            )
            if aa is not None:
                return _aa_dict(session, aa)
            schedule = session.scalar(
                select(ScheduledTransfer).where(
                    ScheduledTransfer.id == task_id,
                    ScheduledTransfer.owner_id == DEMO_USER_ID,
                )
            )
            if schedule is not None:
                return _schedule_dict(session, schedule, _hold_remaining(session, schedule.hold_id))
            return None

    def simulate_payment(self, task_id: str, participant_id: str) -> dict[str, Any]:
        if not isinstance(task_id, str) or not task_id:
            raise BankError("TASK_NOT_FOUND", "未找到当前演示用户的AA收款请求。")
        if not isinstance(participant_id, str) or not participant_id or participant_id == "self":
            raise BankError("INVALID_PARTICIPANT", "请选择有效的AA付款成员。")
        with self._transaction() as session:
            initial_task = session.scalar(
                select(AATask).where(AATask.id == task_id, AATask.owner_id == DEMO_USER_ID)
            )
            if initial_task is None:
                raise BankError("TASK_NOT_FOUND", "未找到当前演示用户的AA收款请求。")
            initial_participant = session.scalar(
                select(AAParticipant).where(
                    AAParticipant.task_id == task_id,
                    AAParticipant.participant_id == participant_id,
                )
            )
            if initial_participant is None:
                raise BankError("PARTICIPANT_NOT_FOUND", "该成员不在此AA收款请求中。")
            if initial_participant.destination_account_id is None:
                raise BankError("PARTICIPANT_NOT_PAYABLE", "我方份额不能作为对方付款处理。")
            destination_id = initial_participant.destination_account_id
            accounts = lock_accounts(session, initial_task.account_id, destination_id)
            task = _lock_aa(session, task_id)
            participant = _lock_participant(session, task_id, participant_id)
            if participant.status == "paid":
                return _payment_receipt(task, participant, duplicate=True)
            if task.status == "cancelled" or participant.status == "cancelled":
                raise BankError("TASK_CANCELLED", "AA收款请求已取消，不能再收取未付款项。")
            if task.status == "completed" or participant.status != "pending":
                raise BankError("PARTICIPANT_NOT_PAYABLE", "该成员当前没有待收款项。")
            if participant.share_minor == 0:
                participant.status = "paid"
                task.state_version += 1
                _add_task_event(
                    session,
                    task,
                    now=_aware(task_now(session)),
                    title=f"{participant.name}无需付款",
                    text="该成员分摊金额为0分，没有发生资金转移。",
                    plan_id=None,
                    operation_id=None,
                )
                return _payment_receipt(task, participant, duplicate=False)

            recipient = _validated_recipient(session, participant_id)
            destination = accounts[destination_id]
            _require_external_recipient(recipient, destination)
            if recipient.destination_account_id != destination_id:
                raise BankError("RECIPIENT_CHANGED", "成员收款账户已变更，不能按原预览模拟付款。")
            operation_id = _stable_operation("aa-pay", task.id, participant.participant_id)
            transfer_funds(
                session,
                destination_id,
                task.account_id,
                participant.share_minor,
                operation_id,
                f"模拟AA付款·{participant.name}",
            )
            participant.status = "paid"
            participant.operation_id = operation_id
            task.collected_minor += participant.share_minor
            pending = session.scalar(
                select(AAParticipant.id).where(
                    AAParticipant.task_id == task_id,
                    AAParticipant.status == "pending",
                    AAParticipant.share_minor > 0,
                ).limit(1)
            )
            task.status = "completed" if pending is None else "partial"
            task.message = _aa_status_message(task)
            task.state_version += 1
            _add_task_event(
                session,
                task,
                now=_aware(task_now(session)),
                title=f"收到{participant.name}的AA付款" if task.status != "completed" else "AA收款已完成",
                text=(
                    f"已从{participant.name}的模拟账户转入{_format_cny(participant.share_minor)}。"
                    f"累计到账{_format_cny(task.collected_minor)}。"
                ),
                plan_id=_origin_plan_for_task(session, task_id),
                operation_id=operation_id,
            )
            return _payment_receipt(task, participant, duplicate=False)

    def get_plan(self, plan_id: str) -> TaskAction | None:
        if not isinstance(plan_id, str) or not plan_id:
            return None
        now = _utcnow()
        with self._transaction() as session:
            plan = _lock_plan(session, plan_id)
            if plan is None or plan.owner_id != DEMO_USER_ID:
                return None
            if plan.status == "pending" and _aware(plan.expires_at) <= now:
                _mark_expired(plan, now)
            return _action_dict(plan)

    def pending_for_conversation(self, conversation_id: str) -> TaskAction | None:
        if not conversation_id:
            return None
        _validate_conversation_id(conversation_id)
        now = _utcnow()
        with self._transaction() as session:
            plans = _plans_for_conversation(session, conversation_id, pending_only=True)
            for plan in plans:
                if _aware(plan.expires_at) <= now:
                    _mark_expired(plan, now)
            active = [plan for plan in plans if plan.status == "pending"]
            if not active:
                return None
            plan = max(active, key=lambda row: (row.created_at, row.id))
            return _action_dict(plan)

    def invalidate_pending_plans(self, conversation_id: str) -> int:
        if not conversation_id:
            return 0
        _validate_conversation_id(conversation_id)
        now = _utcnow()
        with self._transaction() as session:
            self._lock_conversation(session, conversation_id)
            return _invalidate_pending_in_session(session, conversation_id, now)

    def decide_plan(self, plan_id: str, decision: str) -> dict[str, Any]:
        if not isinstance(plan_id, str) or not plan_id:
            raise BankError("PLAN_NOT_FOUND", "未找到当前演示用户的任务操作预览。")
        if decision not in {"approve", "decline"}:
            raise BankError("INVALID_DECISION", "操作决定无效，只能确认或拒绝本次任务操作。")
        real_now = _utcnow()

        with self._transaction() as session:
            # Read only the immutable account binding first, then follow the
            # global lock order: sorted account rows, then plan/task/hold rows.
            initial = session.scalar(
                select(TaskActionPlan).where(
                    TaskActionPlan.id == plan_id,
                    TaskActionPlan.owner_id == DEMO_USER_ID,
                )
            )
            if initial is None:
                raise BankError("PLAN_NOT_FOUND", "未找到当前演示用户的任务操作预览。")
            account_ids = _plan_lock_accounts(session, initial)
            if account_ids:
                lock_accounts(session, *account_ids)
            plan = _lock_plan(session, plan_id)
            if plan is None or plan.owner_id != DEMO_USER_ID:
                raise BankError("PLAN_NOT_FOUND", "未找到当前演示用户的任务操作预览。")

            if plan.status in {"completed", "declined", "expired"} and plan.outcome_status is not None:
                if plan.decision is not None and plan.decision != decision:
                    raise BankError("PLAN_ALREADY_DECIDED", "该任务操作预览已完成决定，不能更改。")
                if plan.status == "expired" and plan.decision is None:
                    plan.decision = decision
                    plan.decided_at = real_now
                task = None
                if plan.status == "completed" and plan.task_id:
                    if plan.action in {"create_aa", "cancel_aa"}:
                        task = _lock_aa(session, plan.task_id)
                    elif plan.action in {"schedule_transfer", "cancel_schedule"}:
                        task = _lock_schedule(session, plan.task_id)
                return _decision_result(plan, task)
            if plan.status == "invalidated":
                raise BankError("PLAN_INVALIDATED", "任务信息已更新，旧预览已失效。")
            if plan.status != "pending":
                raise BankError("PLAN_STATE_INVALID", "任务操作预览状态已变化，请重新发起。")
            if _aware(plan.expires_at) <= real_now:
                _mark_expired(plan, real_now, decision=decision)
                return _decision_result(plan)
            if decision == "decline":
                plan.status = "declined"
                plan.decision = "decline"
                plan.outcome_status = "declined"
                plan.outcome_message = "已取消本次任务操作，没有执行资金变更。"
                plan.decided_at = real_now
                return _decision_result(plan)

            task_time = _aware(task_now(session))
            if plan.action == "create_aa":
                result = self._approve_create_aa(session, plan, task_time)
            elif plan.action == "schedule_transfer":
                result = self._approve_schedule(session, plan, task_time)
            elif plan.action == "cancel_aa":
                result = self._approve_cancel_aa(session, plan, task_time)
            elif plan.action == "cancel_schedule":
                result = self._approve_cancel_schedule(session, plan, task_time)
            else:
                raise BankError("PLAN_STATE_INVALID", "任务操作预览中的操作类型无效。")
            return result

    def _approve_create_aa(
        self, session: Session, plan: TaskActionPlan, now: datetime
    ) -> dict[str, Any]:
        assert plan.account_id is not None and plan.total_minor is not None
        source = session.get(Account, plan.account_id)
        if source is None or source.owner_id != DEMO_USER_ID:
            return _decline_plan(plan, "ACCOUNT_NOT_FOUND", "付款账户已不可用，AA请求没有创建。")
        participants = plan.participants or []
        if not _participants_still_match(session, participants):
            return _decline_plan(plan, "RECIPIENT_CHANGED", "成员收款账户信息已变化，请重新预览AA请求。")
        collectable = plan.collectable_minor or 0
        status = "awaiting_payment" if collectable > 0 else "completed"
        task = AATask(
            id=plan.task_id,
            owner_id=DEMO_USER_ID,
            title=plan.title,
            account_id=plan.account_id,
            total_minor=plan.total_minor,
            collectable_minor=collectable,
            collected_minor=0,
            status=status,
            created_at=now,
            conversation_id=plan.conversation_id,
            state_version=1,
            message=(
                "AA收款请求已创建，等待成员模拟付款；当前到账0元。"
                if collectable > 0
                else "AA收款请求已创建；除我方份额外没有待收款金额。"
            ),
        )
        session.add(task)
        for position, item in enumerate(participants):
            session.add(
                AAParticipant(
                    id=f"aa-participant-{uuid4()}",
                    task_id=task.id,
                    participant_id=str(item["id"]),
                    name=str(item["name"]),
                    destination_account_id=item.get("destination_account_id"),
                    position=position,
                    share_minor=int(item["share_minor"]),
                    status=str(item["status"]),
                )
            )
        operation_id = _stable_operation("aa-create", task.id)
        plan.status = "completed"
        plan.decision = "approve"
        plan.outcome_status = "completed"
        plan.outcome_message = task.message
        plan.operation_id = operation_id
        plan.decided_at = now
        session.flush()
        _add_task_event(
            session,
            task,
            now=now,
            title="AA收款请求已创建",
            text=task.message,
            plan_id=plan.id,
            operation_id=operation_id,
        )
        return _decision_result(plan, task)

    def _approve_schedule(
        self, session: Session, plan: TaskActionPlan, now: datetime
    ) -> dict[str, Any]:
        assert plan.account_id and plan.recipient_id and plan.destination_account_id
        assert plan.amount_minor is not None and plan.execute_at and plan.deadline_at
        source = session.get(Account, plan.account_id)
        recipient = session.get(Recipient, plan.recipient_id)
        destination = session.get(Account, plan.destination_account_id)
        if (
            source is None
            or source.owner_id != DEMO_USER_ID
            or recipient is None
            or recipient.owner_id != DEMO_USER_ID
            or recipient.destination_account_id != plan.destination_account_id
            or destination is None
            or destination.owner_id == DEMO_USER_ID
            or recipient.name != plan.recipient_name
        ):
            return _decline_plan(plan, "RECIPIENT_CHANGED", "账户或收款人信息已变化，定时转账没有预留资金。")
        reference = f"task-schedule:{plan.task_id}"
        try:
            hold = reserve_funds(session, plan.account_id, plan.amount_minor, reference)
        except BankError as error:
            if error.code not in {"INSUFFICIENT_BALANCE", "ACCOUNT_NOT_FOUND", "IDEMPOTENCY_CONFLICT", "HOLD_FINALIZED"}:
                raise
            return _decline_plan(plan, error.code, error.message)
        operation_id = _stable_operation("schedule-pay", plan.task_id)
        task = ScheduledTransfer(
            id=plan.task_id,
            owner_id=DEMO_USER_ID,
            account_id=plan.account_id,
            recipient_id=plan.recipient_id,
            destination_account_id=plan.destination_account_id,
            recipient_name=plan.recipient_name or recipient.name,
            amount_minor=plan.amount_minor,
            execute_at=_aware(plan.execute_at),
            deadline_at=_aware(plan.deadline_at),
            status="scheduled",
            hold_id=hold.id,
            reserved_minor=hold.remaining_minor,
            operation_id=operation_id,
            message=f"已确认定时转账，已预留{_format_cny(plan.amount_minor)}，等待到期执行。",
            created_at=now,
            conversation_id=plan.conversation_id,
            state_version=1,
        )
        session.add(task)
        plan.status = "completed"
        plan.decision = "approve"
        plan.outcome_status = "completed"
        plan.outcome_message = task.message
        plan.operation_id = operation_id
        plan.decided_at = now
        session.flush()
        _add_task_event(
            session,
            task,
            now=now,
            title="定时转账已受理",
            text=task.message,
            plan_id=plan.id,
            operation_id=operation_id,
        )
        return _decision_result(plan, task)

    def _approve_cancel_aa(
        self, session: Session, plan: TaskActionPlan, now: datetime
    ) -> dict[str, Any]:
        task = _lock_aa(session, plan.task_id)
        if task is None:
            return _decline_plan(plan, "TASK_NOT_FOUND", "AA收款请求已不存在。")
        operation_id = _stable_operation("aa-cancel", task.id, plan.id)
        if task.status == "cancelled":
            message = "AA收款请求此前已取消，已收款记录保留，没有重复变更。"
        elif task.status == "completed":
            return _decline_plan(plan, "TASK_NOT_CANCELLABLE", "该AA请求已全部收款，不能取消。")
        elif task.state_version != plan.task_version:
            return _decline_plan(plan, "TASK_STATE_CHANGED", "AA收款进度已变化，请查看最新状态后重新操作。")
        else:
            participants = session.scalars(
                select(AAParticipant)
                .where(AAParticipant.task_id == task.id)
                .order_by(AAParticipant.position)
                .with_for_update()
            ).all()
            for participant in participants:
                if participant.status == "pending":
                    participant.status = "cancelled"
            task.status = "cancelled"
            task.message = (
                f"AA收款请求已取消，已收到{_format_cny(task.collected_minor)}；"
                "未付款部分已停止，不会自动退款。"
            )
            task.state_version += 1
            message = task.message
            _add_task_event(
                session,
                task,
                now=now,
                title="AA收款请求已取消",
                text=message,
                plan_id=plan.id,
                operation_id=operation_id,
            )
        _complete_plan(plan, operation_id, message, now)
        return _decision_result(plan, task)

    def _approve_cancel_schedule(
        self, session: Session, plan: TaskActionPlan, now: datetime
    ) -> dict[str, Any]:
        task = _lock_schedule(session, plan.task_id)
        if task is None:
            return _decline_plan(plan, "TASK_NOT_FOUND", "定时转账已不存在。")
        operation_id = _stable_operation("schedule-cancel", task.id, plan.id)
        if task.status == "cancelled":
            message = "定时转账此前已撤销，资金已经释放；本次没有重复扣款或重复释放。"
        elif task.status != "scheduled":
            return _decline_plan(plan, "TASK_NOT_CANCELLABLE", "该定时转账已执行或已结束，不能撤销。")
        elif task.state_version != plan.task_version:
            return _decline_plan(plan, "TASK_STATE_CHANGED", "定时转账状态已变化，请查看最新状态后重新操作。")
        else:
            hold = release_funds(session, task.hold_id)
            task.reserved_minor = hold.remaining_minor
            task.status = "cancelled"
            task.message = "定时转账已撤销，尚未使用的预留资金已释放。"
            task.state_version += 1
            message = task.message
            _add_task_event(
                session,
                task,
                now=now,
                title="定时转账已撤销",
                text=message,
                plan_id=plan.id,
                operation_id=operation_id,
            )
        _complete_plan(plan, operation_id, message, now)
        return _decision_result(plan, task)

    def process_due(self, as_of: datetime | None = None) -> list[dict[str, Any]]:
        """Execute due schedules in independent, recoverable transactions."""

        fixed_now = _parse_as_of(as_of) if as_of is not None else None
        with self._session_factory() as session:
            observed_now = fixed_now or _aware(task_now(session))
            task_ids = session.scalars(
                select(ScheduledTransfer.id)
                .where(
                    ScheduledTransfer.owner_id == DEMO_USER_ID,
                    ScheduledTransfer.status == "scheduled",
                    ScheduledTransfer.execute_at <= observed_now,
                )
                .order_by(ScheduledTransfer.execute_at, ScheduledTransfer.id)
            ).all()
        results: list[dict[str, Any]] = []
        for task_id in task_ids:
            result = self._process_one_schedule(task_id, observed_now)
            if result is not None:
                results.append(result)
        return results

    def _process_one_schedule(self, task_id: str, as_of: datetime) -> dict[str, Any] | None:
        with self._transaction() as session:
            initial = session.scalar(
                select(ScheduledTransfer).where(
                    ScheduledTransfer.id == task_id,
                    ScheduledTransfer.owner_id == DEMO_USER_ID,
                )
            )
            if initial is None or initial.status != "scheduled":
                return None
            # Destination may have been removed; only lock accounts that still
            # exist, then recheck the recipient binding after task-row locking.
            ids = [initial.account_id, initial.destination_account_id]
            existing_ids = session.scalars(select(Account.id).where(Account.id.in_(ids))).all()
            if initial.account_id not in existing_ids:
                task = _lock_schedule(session, task_id)
                if task is None or task.status != "scheduled":
                    return None
                return self._finish_schedule_failure(
                    session, task, as_of, "ACCOUNT_NOT_FOUND", "付款账户已不可用，未能执行定时转账。"
                )
            # If the destination disappeared, lock the source first and let the
            # post-lock validation persist a clean failure/release.
            lock_accounts(session, *[identity for identity in ids if identity in existing_ids])
            task = _lock_schedule(session, task_id)
            if task is None or task.status != "scheduled":
                return None
            now = as_of
            if now > _aware(task.deadline_at):
                hold = release_funds(session, task.hold_id)
                task.reserved_minor = hold.remaining_minor
                task.status = "expired"
                task.message = "定时转账已超过确认时的24小时执行窗口，预留资金已释放。"
                task.state_version += 1
                _add_task_event(
                    session,
                    task,
                    now=now,
                    title="定时转账已过期",
                    text=task.message,
                    plan_id=_origin_plan_for_task(session, task.id),
                    operation_id=task.operation_id,
                )
                return _schedule_dict(session, task, hold.remaining_minor)
            recipient = session.get(Recipient, task.recipient_id)
            destination = session.get(Account, task.destination_account_id)
            source = session.get(Account, task.account_id)
            if (
                recipient is None
                or recipient.owner_id != DEMO_USER_ID
                or recipient.destination_account_id != task.destination_account_id
                or recipient.name != task.recipient_name
                or destination is None
                or destination.owner_id == DEMO_USER_ID
                or source is None
                or source.owner_id != DEMO_USER_ID
            ):
                return self._finish_schedule_failure(
                    session, task, now, "RECIPIENT_CHANGED", "收款人或账户信息已变化，定时转账未执行，预留资金已释放。"
                )
            try:
                # A savepoint allows a business failure to release the hold in
                # the outer transaction while discarding any helper-side writes.
                with session.begin_nested():
                    consume_funds(
                        session,
                        task.hold_id,
                        task.destination_account_id,
                        task.amount_minor,
                        task.operation_id,
                        f"定时转账·{task.recipient_name}",
                    )
            except BankError as error:
                return self._finish_schedule_failure(
                    session, task, now, error.code, f"定时转账未执行：{error.message}预留资金已释放。"
                )
            hold = session.scalar(select(FundHold).where(FundHold.id == task.hold_id))
            task.reserved_minor = hold.remaining_minor if hold is not None else 0
            task.status = "completed"
            task.message = f"已按原确认授权向{task.recipient_name}转账{_format_cny(task.amount_minor)}。"
            task.state_version += 1
            _add_task_event(
                session,
                task,
                now=now,
                title="定时转账已完成",
                text=task.message,
                plan_id=_origin_plan_for_task(session, task.id),
                operation_id=task.operation_id,
            )
            return _schedule_dict(session, task, task.reserved_minor)

    @staticmethod
    def _finish_schedule_failure(
        session: Session,
        task: ScheduledTransfer,
        now: datetime,
        code: str,
        message: str,
    ) -> dict[str, Any]:
        hold = release_funds(session, task.hold_id)
        task.reserved_minor = hold.remaining_minor
        task.status = "failed"
        task.message = message
        task.state_version += 1
        _add_task_event(
            session,
            task,
            now=now,
            title="定时转账未能执行",
            text=message,
            plan_id=_origin_plan_for_task(session, task.id),
            operation_id=task.operation_id,
            extra_receipt={"error_code": code},
        )
        return _schedule_dict(session, task, task.reserved_minor)

    def completed_events(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(TaskEvent)
                .where(TaskEvent.owner_id == DEMO_USER_ID)
                .order_by(TaskEvent.created_at, TaskEvent.id)
            ).all()
            result = []
            for row in rows:
                event = _event_dict(row)
                origin = _origin_plan_for_task(session, row.task_id)
                if origin and origin != row.plan_id:
                    event["related_plan_ids"] = [origin]
                result.append(event)
            return result


def _validate_title(title: str | None) -> str | None:
    if title is None:
        return None
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 160:
        raise BankError("INVALID_TITLE", "任务标题需要为1至160个字符。")
    return title.strip()


def _validate_conversation_id(conversation_id: str) -> None:
    if not isinstance(conversation_id, str) or not conversation_id.strip() or len(conversation_id) > 128:
        raise BankError("INVALID_CONVERSATION", "会话标识长度无效。")


def _validated_task_id(task_id: str | None) -> str:
    if not isinstance(task_id, str) or not _SAFE_ID.fullmatch(task_id):
        raise BankError("INVALID_TASK_ID", "任务标识格式无效。")
    return task_id


def _validate_amount(amount_minor: int | None) -> None:
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise BankError("INVALID_AMOUNT", "金额必须是整数分。")
    if amount_minor <= 0 or amount_minor > MAX_SAFE_JSON_INTEGER:
        raise BankError("INVALID_AMOUNT", "金额须为大于零的整数分。")


def _validate_participant_ids(participant_ids: Sequence[str] | None) -> list[str]:
    if isinstance(participant_ids, (str, bytes)) or not isinstance(participant_ids, Sequence):
        raise BankError("INVALID_PARTICIPANTS", "AA参与者需要是有序列表。")
    ids = list(participant_ids)
    if not 2 <= len(ids) <= 20 or "self" not in ids:
        raise BankError("INVALID_PARTICIPANTS", "AA参与者须为2至20人，并且必须包含self。")
    if any(not isinstance(identity, str) or not identity or len(identity) > 64 for identity in ids):
        raise BankError("INVALID_PARTICIPANTS", "AA成员需使用有效的self或Recipient编号。")
    if len(ids) != len(set(ids)):
        raise BankError("INVALID_PARTICIPANTS", "AA参与者不能重复。")
    return ids


def _resolve_participants(session: Session, ordered_ids: list[str]) -> list[dict[str, Any]]:
    user = session.get(User, DEMO_USER_ID)
    if user is None:
        raise BankError("BANK_NOT_SEEDED", "演示账户尚未初始化。")
    result: list[dict[str, Any]] = []
    for identity in ordered_ids:
        if identity == "self":
            result.append({"id": "self", "name": user.name, "destination_account_id": None})
            continue
        recipient = _validated_recipient(session, identity)
        destination = session.get(Account, recipient.destination_account_id)
        if destination is None or destination.owner_id == DEMO_USER_ID:
            raise BankError("RECIPIENT_NOT_FOUND", "AA成员需要使用当前用户已保存的外部收款人。")
        result.append(
            {
                "id": recipient.id,
                "name": recipient.name,
                "destination_account_id": recipient.destination_account_id,
            }
        )
    return result


def _split_amount(total_minor: int, participants: list[dict[str, Any]]) -> list[int]:
    quotient, remainder = divmod(total_minor, len(participants))
    return [quotient + (1 if index < remainder else 0) for index in range(len(participants))]


def _validated_recipient(session: Session, recipient_id: str) -> Recipient:
    if not isinstance(recipient_id, str) or not recipient_id:
        raise BankError("INVALID_RECIPIENT", "请选择已保存的收款人编号。")
    recipient = session.scalar(
        select(Recipient).where(Recipient.id == recipient_id, Recipient.owner_id == DEMO_USER_ID)
    )
    if recipient is None:
        raise BankError("RECIPIENT_NOT_FOUND", "未找到当前演示用户已保存的该收款人。")
    return recipient


def _require_demo_account(account: Account) -> None:
    if account.owner_id != DEMO_USER_ID:
        raise BankError("ACCOUNT_NOT_FOUND", "付款账户不属于当前演示用户。")


def _require_external_recipient(recipient: Recipient, destination: Account) -> None:
    if recipient.owner_id != DEMO_USER_ID or destination.owner_id == DEMO_USER_ID:
        raise BankError("RECIPIENT_NOT_FOUND", "收款人未绑定有效的模拟外部账户。")


def _parse_execute_at(value: datetime | str | None) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as error:
            raise BankError("INVALID_EXECUTE_AT", "执行时间格式无效，请提供带时区的日期时间。") from error
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BankError("INVALID_EXECUTE_AT", "执行时间必须包含时区，不能使用模糊或本地时间。")
    return value.astimezone(UTC)


def _parse_as_of(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BankError("INVALID_EXECUTE_AT", "处理时间必须包含时区。")
    return value.astimezone(UTC)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _format_cny(amount_minor: int) -> str:
    return f"{Decimal(amount_minor) / 100:,.2f}元"


def _format_shanghai(value: datetime) -> str:
    return _aware(value).astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S 北京时间")


def _iso_utc(value: datetime) -> str:
    return _aware(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _stable_operation(*parts: str) -> str:
    digest = sha256(":".join(parts).encode("utf-8")).hexdigest()[:40]
    prefix = parts[0].replace("_", "-")[:16]
    return f"{prefix}-{digest}"[:64]


def _lock_aa(session: Session, task_id: str) -> AATask | None:
    return session.scalar(
        select(AATask)
        .where(AATask.id == task_id, AATask.owner_id == DEMO_USER_ID)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _lock_schedule(session: Session, task_id: str) -> ScheduledTransfer | None:
    return session.scalar(
        select(ScheduledTransfer)
        .where(ScheduledTransfer.id == task_id, ScheduledTransfer.owner_id == DEMO_USER_ID)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _lock_participant(session: Session, task_id: str, participant_id: str) -> AAParticipant:
    participant = session.scalar(
        select(AAParticipant)
        .where(AAParticipant.task_id == task_id, AAParticipant.participant_id == participant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if participant is None:
        raise BankError("PARTICIPANT_NOT_FOUND", "该成员不在此AA收款请求中。")
    return participant


def _lock_plan(session: Session, plan_id: str) -> TaskActionPlan | None:
    return session.scalar(
        select(TaskActionPlan)
        .where(TaskActionPlan.id == plan_id, TaskActionPlan.owner_id == DEMO_USER_ID)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _plan_lock_accounts(session: Session, plan: TaskActionPlan) -> list[str]:
    if plan.action in {"create_aa", "schedule_transfer"}:
        ids = [plan.account_id]
        if plan.action == "schedule_transfer":
            ids.append(plan.destination_account_id)
    elif plan.action == "cancel_aa":
        task = session.scalar(select(AATask).where(AATask.id == plan.task_id))
        ids = [task.account_id] if task is not None else []
    else:
        task = session.scalar(select(ScheduledTransfer).where(ScheduledTransfer.id == plan.task_id))
        ids = [task.account_id] if task is not None else []
    return sorted({identity for identity in ids if identity})


def _plans_for_conversation(
    session: Session, conversation_id: str, *, pending_only: bool
) -> list[TaskActionPlan]:
    query = select(TaskActionPlan).where(
        TaskActionPlan.owner_id == DEMO_USER_ID,
        TaskActionPlan.conversation_id == conversation_id,
    )
    if pending_only:
        query = query.where(TaskActionPlan.status == "pending")
    return session.scalars(query.order_by(TaskActionPlan.created_at, TaskActionPlan.id).with_for_update()).all()


def _invalidate_pending_in_session(session: Session, conversation_id: str, now: datetime) -> int:
    plans = _plans_for_conversation(session, conversation_id, pending_only=True)
    for plan in plans:
        if _aware(plan.expires_at) <= now:
            _mark_expired(plan, now)
        else:
            plan.status = "invalidated"
            plan.outcome_status = "declined"
            plan.outcome_message = "会话中的任务信息已更新，旧预览已失效。"
            plan.decided_at = now
    return len(plans)


def _mark_expired(plan: TaskActionPlan, now: datetime, decision: str | None = None) -> None:
    plan.status = "expired"
    plan.decision = decision
    plan.outcome_status = "declined"
    plan.outcome_code = "PLAN_EXPIRED"
    plan.outcome_message = "任务操作预览已过期，没有执行资金变更。"
    plan.decided_at = now


def _decline_plan(plan: TaskActionPlan, code: str, message: str) -> dict[str, Any]:
    plan.status = "declined"
    plan.decision = "approve"
    plan.outcome_status = "declined"
    plan.outcome_code = code
    plan.outcome_message = message
    plan.decided_at = _utcnow()
    return _decision_result(plan)


def _complete_plan(plan: TaskActionPlan, operation_id: str | None, message: str, now: datetime) -> None:
    plan.status = "completed"
    plan.decision = "approve"
    plan.outcome_status = "completed"
    plan.outcome_message = message
    plan.operation_id = operation_id
    plan.decided_at = now


def _decision_result(plan: TaskActionPlan, task: AATask | ScheduledTransfer | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": plan.outcome_status or "declined",
        "message": plan.outcome_message or "任务操作未执行。",
    }
    if plan.operation_id:
        result["operation_id"] = plan.operation_id
    if plan.outcome_code:
        result["code"] = plan.outcome_code
    if plan.conversation_id:
        result["conversation_id"] = plan.conversation_id
    if plan.task_id:
        result["task_id"] = plan.task_id
    if task is not None:
        result["task_status"] = task.status
        result["state_version"] = task.state_version
    elif plan.task_version is not None:
        result["state_version"] = plan.task_version
    return result


def _action_dict(plan: TaskActionPlan) -> TaskAction:
    result: TaskAction = {
        "id": plan.id,
        "kind": "task",
        "action": plan.action,
        "title": plan.title,
        "description": plan.description,
        "status": plan.status,
        "expires_at": _iso_utc(plan.expires_at),
        "conversation_id": plan.conversation_id,
        "task_id": plan.task_id,
    }
    optional = {
        "account_id": plan.account_id,
        "total_minor": plan.total_minor,
        "collectable_minor": plan.collectable_minor,
        "participants": plan.participants,
        "recipient_id": plan.recipient_id,
        "recipient_name": plan.recipient_name,
        "amount_minor": plan.amount_minor,
        "execute_at": _iso_utc(plan.execute_at) if plan.execute_at else None,
        "deadline_at": _iso_utc(plan.deadline_at) if plan.deadline_at else None,
        "reserved_minor": plan.reserved_minor,
    }
    result.update({key: value for key, value in optional.items() if value is not None})
    return result


def _aa_status_message(task: AATask) -> str:
    if task.status == "completed":
        return f"AA收款已完成，累计到账{_format_cny(task.collected_minor)}。"
    return f"AA收款进行中，已到账{_format_cny(task.collected_minor)}，待收{_format_cny(task.collectable_minor - task.collected_minor)}。"


def _aa_dict(session: Session, task: AATask) -> dict[str, Any]:
    participants = session.scalars(
        select(AAParticipant)
        .where(AAParticipant.task_id == task.id)
        .order_by(AAParticipant.position, AAParticipant.id)
    ).all()
    return {
        "id": task.id,
        "title": task.title,
        "account_id": task.account_id,
        "total_minor": task.total_minor,
        "collectable_minor": task.collectable_minor,
        "collected_minor": task.collected_minor,
        "status": task.status,
        "participants": [
            {
                "id": row.participant_id,
                "name": row.name,
                "share_minor": row.share_minor,
                "status": row.status,
                **({"operation_id": row.operation_id} if row.operation_id else {}),
            }
            for row in participants
        ],
        "created_at": _iso_utc(task.created_at),
        "conversation_id": task.conversation_id,
        "state_version": task.state_version,
    }


def _schedule_dict(
    session: Session, task: ScheduledTransfer, remaining_minor: int | None = None
) -> dict[str, Any]:
    if remaining_minor is None:
        remaining_minor = _hold_remaining(session, task.hold_id)
    return {
        "id": task.id,
        "account_id": task.account_id,
        "recipient_id": task.recipient_id,
        "recipient_name": task.recipient_name,
        "amount_minor": task.amount_minor,
        "execute_at": _iso_utc(task.execute_at),
        "deadline_at": _iso_utc(task.deadline_at),
        "status": task.status,
        "hold_id": task.hold_id,
        "reserved_minor": remaining_minor,
        "operation_id": task.operation_id,
        "message": task.message,
        "conversation_id": task.conversation_id,
        "state_version": task.state_version,
    }


def _hold_remaining(session: Session, hold_id: str) -> int:
    hold = session.get(FundHold, hold_id)
    return hold.remaining_minor if hold is not None else 0


def _payment_receipt(task: AATask, participant: AAParticipant, *, duplicate: bool) -> dict[str, Any]:
    return {
        "status": "completed",
        "kind": "task",
        "task_id": task.id,
        "task_status": task.status,
        "state_version": task.state_version,
        "participant_id": participant.participant_id,
        "participant_status": participant.status,
        "amount_minor": participant.share_minor,
        "operation_id": participant.operation_id,
        "collected_minor": task.collected_minor,
        "duplicate": duplicate,
        "message": "已返回该成员原付款结果，没有重复转账。" if duplicate else task.message,
    }


def _participants_still_match(session: Session, participants: list[dict[str, object]]) -> bool:
    for item in participants:
        identity = str(item.get("id", ""))
        if identity == "self":
            continue
        recipient = session.get(Recipient, identity)
        if (
            recipient is None
            or recipient.owner_id != DEMO_USER_ID
            or recipient.destination_account_id != item.get("destination_account_id")
            or recipient.name != item.get("name")
        ):
            return False
        destination = session.get(Account, recipient.destination_account_id)
        if destination is None or destination.owner_id == DEMO_USER_ID:
            return False
    return True


def _origin_plan_for_task(session: Session, task_id: str) -> str | None:
    return session.scalar(
        select(TaskActionPlan.id)
        .where(
            TaskActionPlan.owner_id == DEMO_USER_ID,
            TaskActionPlan.task_id == task_id,
            TaskActionPlan.action.in_(("create_aa", "schedule_transfer")),
            TaskActionPlan.status == "completed",
        )
        .order_by(TaskActionPlan.decided_at, TaskActionPlan.id)
        .limit(1)
    )


def _add_task_event(
    session: Session,
    task: AATask | ScheduledTransfer,
    *,
    now: datetime,
    title: str,
    text: str,
    plan_id: str | None,
    operation_id: str | None,
    extra_receipt: dict[str, object] | None = None,
) -> None:
    receipt: dict[str, object] = {
        "task_id": task.id,
        "task_status": task.status,
        "state_version": task.state_version,
    }
    if isinstance(task, AATask):
        receipt.update(
            {
                "kind": "aa",
                "total_minor": task.total_minor,
                "collectable_minor": task.collectable_minor,
                "collected_minor": task.collected_minor,
            }
        )
    else:
        receipt.update(
            {
                "kind": "schedule",
                "amount_minor": task.amount_minor,
                "reserved_minor": task.reserved_minor,
                "execute_at": _iso_utc(task.execute_at),
                "deadline_at": _iso_utc(task.deadline_at),
            }
        )
    if extra_receipt:
        receipt.update(extra_receipt)
    identity = "task-event-" + sha256(f"{task.id}:{task.state_version}".encode()).hexdigest()[:32]
    session.add(
        TaskEvent(
            id=identity,
            owner_id=DEMO_USER_ID,
            task_id=task.id,
            kind="task",
            source="tasks",
            title=title,
            text=text,
            created_at=_aware(now),
            plan_id=plan_id,
            conversation_id=task.conversation_id,
            state_version=task.state_version,
            operation_id=operation_id,
            receipt=receipt,
        )
    )


def _event_dict(event: TaskEvent) -> dict[str, Any]:
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
        "receipt": event.receipt,
    }
