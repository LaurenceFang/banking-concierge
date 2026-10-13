"""Shared held-funds ledger. All helpers use the caller's transaction."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .errors import BankError
from .models import Account, FundHold, LedgerTransaction

MAX_MINOR = 9_007_199_254_740_991


def _amount(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_MINOR:
        raise BankError("INVALID_AMOUNT", "金额须为正整数分且在支持范围内。")


def reserved_balance(session: Session, account_id: str) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.sum(FundHold.remaining_minor), 0)).where(
                FundHold.account_id == account_id, FundHold.status == "active"
            )
        )
        or 0
    )


def available_balance(session: Session, account: Account) -> int:
    return account.balance_minor - reserved_balance(session, account.id)


def lock_accounts(session: Session, *ids: str) -> dict[str, Account]:
    rows = session.scalars(
        select(Account)
        .where(Account.id.in_(set(ids)))
        .order_by(Account.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    if len(rows) != len(set(ids)):
        raise BankError("ACCOUNT_NOT_FOUND", "相关模拟账户不存在。")
    if any(row.currency != "CNY" for row in rows):
        raise BankError("CURRENCY_UNSUPPORTED", "当前仅支持人民币模拟账户。")
    return {row.id: row for row in rows}


def reserve_funds(session: Session, account_id: str, amount_minor: int, reference: str) -> FundHold:
    _amount(amount_minor)
    if not isinstance(reference, str) or not 1 <= len(reference) <= 160:
        raise BankError("INVALID_REFERENCE", "资金预留标识无效。")
    source = lock_accounts(session, account_id)[account_id]
    if source.owner_id != "demo-user-001":
        raise BankError("ACCOUNT_NOT_FOUND", "该付款账户不属于当前演示用户。")
    old = session.scalar(select(FundHold).where(FundHold.reference == reference).with_for_update())
    if old:
        if old.account_id != account_id or old.amount_minor != amount_minor:
            raise BankError("IDEMPOTENCY_CONFLICT", "同一资金预留标识的参数发生变化。")
        if old.status != "active":
            raise BankError("HOLD_FINALIZED", "原资金预留已结束，不能重新占用。")
        return old
    if available_balance(session, source) < amount_minor:
        raise BankError("INSUFFICIENT_BALANCE", "可用余额不足，已预留资金不能重复使用。")
    hold = FundHold(
        id=f"hold-{uuid4()}",
        account_id=account_id,
        reference=reference,
        amount_minor=amount_minor,
        remaining_minor=amount_minor,
        status="active",
        created_at=datetime.now(UTC),
    )
    session.add(hold)
    session.flush()
    return hold


def _lock_hold(session: Session, identity: str) -> FundHold:
    row = session.scalar(
        select(FundHold)
        .where(FundHold.id == identity)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise BankError("HOLD_NOT_FOUND", "没有找到这笔资金预留。")
    return row


def release_funds(session: Session, hold_id: str) -> FundHold:
    initial = session.get(FundHold, hold_id)
    if initial is None:
        raise BankError("HOLD_NOT_FOUND", "没有找到这笔资金预留。")
    lock_accounts(session, initial.account_id)
    hold = _lock_hold(session, hold_id)
    if hold.status == "active":
        hold.remaining_minor, hold.status = 0, "released"
        session.flush()
    return hold


def _existing(session, source_id, destination_id, amount, operation_id, description):
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 64:
        raise BankError("INVALID_OPERATION", "资金操作标识无效。")
    if source_id == destination_id:
        raise BankError("INVALID_TRANSFER", "不能向同一账户付款。")
    rows = session.scalars(
        select(LedgerTransaction).where(LedgerTransaction.operation_id == operation_id)
    ).all()
    if not rows:
        return False
    actual = {(row.account_id, row.amount_minor, row.description) for row in rows}
    if len(rows) != 2 or actual != {(source_id, -amount, description), (destination_id, amount, description)}:
        raise BankError("IDEMPOTENCY_CONFLICT", "原资金操作与本次参数不一致。")
    return True


def _post(session, source, destination, amount, operation_id, description):
    if source.balance_minor < amount or destination.balance_minor + amount > MAX_MINOR:
        raise BankError("INSUFFICIENT_BALANCE", "账户资金不足或超过可支持范围。")
    source.balance_minor -= amount
    destination.balance_minor += amount
    now = datetime.now(UTC)
    for suffix, account, signed in (("debit", source, -amount), ("credit", destination, amount)):
        session.add(
            LedgerTransaction(
                id=f"{operation_id}:{suffix}",
                account_id=account.id,
                description=description[:240],
                amount_minor=signed,
                currency="CNY",
                created_at=now,
                operation_id=operation_id,
            )
        )
    session.flush()


def transfer_funds(
    session: Session,
    source_id: str,
    destination_id: str,
    amount_minor: int,
    operation_id: str,
    description: str,
) -> str:
    _amount(amount_minor)
    accounts = lock_accounts(session, source_id, destination_id)
    if _existing(session, source_id, destination_id, amount_minor, operation_id, description[:240]):
        return operation_id
    if available_balance(session, accounts[source_id]) < amount_minor:
        raise BankError("INSUFFICIENT_BALANCE", "可用余额不足，已预留资金不能再次支付。")
    _post(session, accounts[source_id], accounts[destination_id], amount_minor, operation_id, description)
    return operation_id


def consume_funds(
    session: Session,
    hold_id: str,
    destination_id: str,
    amount_minor: int,
    operation_id: str,
    description: str,
) -> str:
    _amount(amount_minor)
    initial = session.get(FundHold, hold_id)
    if initial is None:
        raise BankError("HOLD_NOT_FOUND", "没有找到这笔资金预留。")
    source_id = initial.account_id
    accounts = lock_accounts(session, source_id, destination_id)
    if _existing(session, source_id, destination_id, amount_minor, operation_id, description[:240]):
        return operation_id
    hold = _lock_hold(session, hold_id)
    if hold.status != "active" or hold.remaining_minor < amount_minor:
        raise BankError("HOLD_INSUFFICIENT", "资金预留已释放或剩余金额不足。")
    _post(session, accounts[source_id], accounts[destination_id], amount_minor, operation_id, description)
    hold.remaining_minor -= amount_minor
    if not hold.remaining_minor:
        hold.status = "consumed"
    session.flush()
    return operation_id
