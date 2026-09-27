"""领域服务：日历落库、可行性探测、方案采纳与重放。

读取既有日历/方案时统一经 :mod:`app.integrity` 做整版校验：损坏记录产生
稳定的 422 数据异常，而不是 500 或静默挑选。探测、采纳、重放全程只读
既有记录，不新增方案，也不改写任何日历、方案与见证。
"""
from __future__ import annotations

import json
import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from . import integrity, scheduling
from .database import CalendarRow, GateRow, PlanRow
from .schemas import CalendarIn, PlanIn, VoyageIn


def _new_id() -> str:
    return uuid.uuid4().hex


def create_calendar(db: Session, payload: CalendarIn) -> CalendarRow:
    """整版原子写入：请求体已由 schemas 整版校验，任一异常则全部不落库。"""
    cal = CalendarRow(id=_new_id())
    db.add(cal)
    for pos, gate in enumerate(payload.gates):
        db.add(
            GateRow(
                calendar_id=cal.id,
                position=pos,
                gate_id=gate.gate_id,
                windows=json.dumps([list(w) for w in gate.windows]),
            )
        )
    db.commit()
    db.refresh(cal)
    return cal


def _windows_for_gate(
    stored: list[integrity.StoredGate], gate_ids: list[str]
) -> list[tuple[list[int], list[int]]]:
    by_id = {g.gate_id: g for g in stored}
    windows_by_gate: list[tuple[list[int], list[int]]] = []
    for gid in gate_ids:
        gate = by_id.get(gid)
        if gate is None:
            raise HTTPException(status_code=422, detail=f"闸门 {gid!r} 不在日历中")
        windows = gate.windows
        windows_by_gate.append(
            ([w[0] for w in windows], [w[1] for w in windows])
        )
    return windows_by_gate


def probe(db: Session, voyage: VoyageIn) -> list[list[int]]:
    stored = integrity.load_calendar(db, voyage.calendar_id)
    windows_by_gate = _windows_for_gate(stored, voyage.gates)
    return scheduling.feasible_departures(
        list(voyage.legs),
        list(voyage.max_waits),
        windows_by_gate,
        (voyage.search_start, voyage.search_end),
    )


def _witnesses(gate_ids: list[str], trace: scheduling.Trace) -> list[dict]:
    return [
        {
            "gate_id": gate_ids[i],
            "arrival": trace.arrivals[i],
            "entry": trace.entries[i],
            "wait": trace.entries[i] - trace.arrivals[i],
        }
        for i in range(len(gate_ids))
    ]


def adopt_plan(db: Session, payload: PlanIn) -> PlanRow:
    stored = integrity.load_calendar(db, payload.calendar_id)
    windows_by_gate = _windows_for_gate(stored, payload.gates)
    result = scheduling.forward_trace(
        payload.departure,
        list(payload.legs),
        list(payload.max_waits),
        windows_by_gate,
    )
    if isinstance(result, scheduling.GateFailure):
        deadline = result.arrival + payload.max_waits[result.gate_index]
        raise HTTPException(
            status_code=409,
            detail={
                "error": "DEPARTURE_INFEASIBLE",
                "failed_gate_id": payload.gates[result.gate_index],
                "failed_index": result.gate_index,
                "arrival": result.arrival,
                "wait_deadline": deadline,
                "reason": result.reason,
            },
        )

    witnesses = _witnesses(payload.gates, result)
    plan = PlanRow(
        id=_new_id(),
        calendar_id=payload.calendar_id,
        payload=json.dumps(
            {
                "gates": payload.gates,
                "legs": list(payload.legs),
                "max_waits": list(payload.max_waits),
            }
        ),
        departure=payload.departure,
        witnesses=json.dumps(witnesses),
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def replay_plan(
    db: Session, plan_id: str, new_calendar_id: str
) -> dict:
    """在新日历上重放方案；只读，原方案不改写。

    既有方案（含航程定义与见证）与新日历都先经整版完整性校验，损坏记录
    返回 422 数据异常；404 与合法的 INVALID/STILL_VALID 语义保持不变。

    逐闸按航程顺序模拟：缺闸与窗口失效都在*首个*出问题的闸门处返回，
    前段已经失效时，不会再被后段的缺闸（或其失效）掩盖。与探测、采纳
    使用同一个 earliest_entry 前向模拟（同一套左闭右开/最早可入规则）。
    """
    # 先校验新日历、再校验旧方案：损坏日历不会被方案语义问题掩盖，反之亦然。
    stored = integrity.load_calendar(db, new_calendar_id)
    plan, definition, _ = integrity.load_plan(db, plan_id)
    by_id = {g.gate_id: g for g in stored}

    gate_ids: list[str] = definition["gates"]
    legs: list[int] = definition["legs"]
    waits: list[int] = definition["max_waits"]

    arrival = plan.departure
    for i, gid in enumerate(gate_ids):
        gate = by_id.get(gid)
        if gate is None:
            # 到本闸为止航程仍可推进：缺闸按到达截止时刻之外的原因报告。
            return {
                "status": "INVALID",
                "failed_gate_id": gid,
                "failed_index": i,
                "arrival": None,
                "wait_deadline": None,
                "reason": "GATE_NOT_IN_CALENDAR",
            }
        starts = [w[0] for w in gate.windows]
        ends = [w[1] for w in gate.windows]
        entry = scheduling.earliest_entry(arrival, starts, ends)
        if entry is None:
            return {
                "status": "INVALID",
                "failed_gate_id": gid,
                "failed_index": i,
                "arrival": arrival,
                "wait_deadline": arrival + waits[i],
                "reason": "NO_OPEN_WINDOW",
            }
        if entry - arrival > waits[i]:
            return {
                "status": "INVALID",
                "failed_gate_id": gid,
                "failed_index": i,
                "arrival": arrival,
                "wait_deadline": arrival + waits[i],
                "reason": "WAIT_EXCEEDED",
            }
        if i < len(gate_ids) - 1:
            arrival = entry + legs[i]

    return {
        "status": "STILL_VALID",
        "failed_gate_id": None,
        "failed_index": None,
        "arrival": None,
        "wait_deadline": None,
        "reason": None,
    }


def get_plan_dict(db: Session, plan_id: str) -> dict:
    """读取既有方案（整版校验后）并转成 PlanOut 所需字典。"""
    plan, definition, witnesses = integrity.load_plan(db, plan_id)
    return {
        "id": plan.id,
        "calendar_id": plan.calendar_id,
        "gates": definition["gates"],
        "legs": definition["legs"],
        "max_waits": definition["max_waits"],
        "departure": plan.departure,
        "witnesses": witnesses,
    }
