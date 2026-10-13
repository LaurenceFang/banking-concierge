"""Durable offset clock for scheduled demo tasks; never changes the OS clock."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import BigInteger, Integer, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

from .bank.errors import BankError


class Base(DeclarativeBase):
    pass


class TaskClockState(Base):
    __tablename__ = "task_clock_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offset_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


def task_now(session: Session) -> datetime:
    row = session.get(TaskClockState, 1)
    return datetime.now(UTC) + timedelta(seconds=row.offset_seconds if row else 0)


class TaskClock:
    def __init__(self, database_url: str):
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        options = {"pool_pre_ping": True}
        if database_url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 10}
            if database_url.endswith(":memory:"):
                options["poolclass"] = StaticPool
        self.engine = create_engine(database_url, **options)
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as session, session.begin():
            if session.get(TaskClockState, 1) is None:
                session.add(TaskClockState(id=1, offset_seconds=0))

    def now(self) -> datetime:
        with Session(self.engine) as session:
            return task_now(session)

    def advance(self, minutes: int) -> datetime:
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 525600:
            raise BankError("INVALID_TIME_ADVANCE", "模拟时间可推进1至525600分钟。")
        with Session(self.engine) as session, session.begin():
            if self.engine.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = session.scalar(select(TaskClockState).where(TaskClockState.id == 1).with_for_update())
            row.offset_seconds += minutes * 60
            session.flush()
            return task_now(session)

    def close(self):
        self.engine.dispose()
