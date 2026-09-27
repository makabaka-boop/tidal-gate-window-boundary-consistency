"""FastAPI 应用：潮汐闸门航程调度。"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from . import integrity, services
from .database import SessionLocal, init_db
from .schemas import (
    CalendarIn,
    CalendarOut,
    GateOut,
    PlanIn,
    PlanOut,
    ProbeResponse,
    ReplayResponse,
    VoyageIn,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 数据库容器可能稍后就绪：建表前短暂重试。
    last_error: Exception | None = None
    for _ in range(30):
        try:
            init_db()
            break
        except Exception as exc:  # pragma: no cover - 仅容器启动竞态
            last_error = exc
            time.sleep(1)
    else:
        raise RuntimeError(f"数据库不可用: {last_error}")
    yield


app = FastAPI(title="潮汐闸门航程调度 API", version="1.0.0", lifespan=lifespan)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _calendar_out(db: Session, calendar_id: str) -> CalendarOut:
    """读取日历并整版校验；损坏记录返回稳定 422，而非矛盾详情或 500。"""
    stored = integrity.load_calendar(db, calendar_id)
    return CalendarOut(
        id=calendar_id,
        gates=[
            GateOut(gate_id=g.gate_id, windows=g.windows) for g in stored
        ],
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/calendars", response_model=CalendarOut, status_code=201)
def publish_calendar(
    payload: CalendarIn, db: Session = Depends(get_db)
) -> CalendarOut:
    cal = services.create_calendar(db, payload)
    return _calendar_out(db, cal.id)


@app.get("/calendars/{calendar_id}", response_model=CalendarOut)
def read_calendar(
    calendar_id: str, db: Session = Depends(get_db)
) -> CalendarOut:
    return _calendar_out(db, calendar_id)


@app.post("/voyages/probe", response_model=ProbeResponse)
def probe_voyage(
    voyage: VoyageIn, db: Session = Depends(get_db)
) -> ProbeResponse:
    intervals = services.probe(db, voyage)
    return ProbeResponse(intervals=intervals)


@app.post("/plans", response_model=PlanOut, status_code=201)
def adopt_plan(payload: PlanIn, db: Session = Depends(get_db)) -> PlanOut:
    plan = services.adopt_plan(db, payload)
    return PlanOut(**services.get_plan_dict(db, plan.id))


@app.get("/plans/{plan_id}", response_model=PlanOut)
def read_plan(plan_id: str, db: Session = Depends(get_db)) -> PlanOut:
    return PlanOut(**services.get_plan_dict(db, plan_id))


@app.post(
    "/plans/{plan_id}/replay/{new_calendar_id}",
    response_model=ReplayResponse,
)
def replay_plan(
    plan_id: str, new_calendar_id: str, db: Session = Depends(get_db)
) -> ReplayResponse:
    return ReplayResponse(
        **services.replay_plan(db, plan_id, new_calendar_id)
    )
