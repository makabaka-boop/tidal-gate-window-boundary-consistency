"""PostgreSQL 持久化：不可变日历（含闸门窗口）与采纳方案。"""
from __future__ import annotations

import os

from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://tide:tide@db:5432/tide",
)

connect_args: dict = {}
if DATABASE_URL.startswith("sqlite"):
    # SQLite（仅测试用）需要允许跨线程共享同一个内存库。
    from sqlalchemy.pool import StaticPool

    connect_args = {"check_same_thread": False}
    engine = create_engine(
        DATABASE_URL,
        connect_args=connect_args,
        poolclass=StaticPool,
        future=True,
    )
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)


class Base(DeclarativeBase):
    pass


class CalendarRow(Base):
    __tablename__ = "calendars"

    id = Column(String(32), primary_key=True)


class GateRow(Base):
    __tablename__ = "gates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    calendar_id = Column(
        String(32), ForeignKey("calendars.id", ondelete="CASCADE"), nullable=False
    )
    position = Column(Integer, nullable=False)
    gate_id = Column(String(128), nullable=False)
    # [[start, end], ...]，已按 start 升序、互不重叠（可相接）。
    windows = Column(Text, nullable=False)

    calendar = relationship("CalendarRow")


class PlanRow(Base):
    __tablename__ = "plans"

    id = Column(String(32), primary_key=True)
    calendar_id = Column(String(32), nullable=False)
    # 方案的航程定义快照：JSON 文本。
    payload = Column(Text, nullable=False)
    departure = Column(BigInteger, nullable=False)
    witnesses = Column(Text, nullable=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
