"""请求 / 响应 Pydantic 模型与整版校验。

整版校验失败统一由 FastAPI 转成 422，非法日历不会进入服务层、更不会落库。
"""
from __future__ import annotations

import re

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

MAX_TIME = 10**12
MAX_GATES = 200
MAX_GATE_ID_LEN = 128

# 不重复 ASCII 字符串：只接受可见 ASCII（不含空格），长度 1..128。
ASCII_ID = re.compile(r"^[\x21-\x7e]{1,%d}$" % MAX_GATE_ID_LEN)


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class GateIn(StrictModel):
    gate_id: str = Field(min_length=1, max_length=MAX_GATE_ID_LEN)
    windows: list[list[int]] = Field(min_length=1)

    @field_validator("gate_id")
    @classmethod
    def _gate_id_ascii(cls, v: str) -> str:
        if not ASCII_ID.fullmatch(v):
            raise ValueError("gate_id 必须为 1..128 个非空白 ASCII 字符")
        return v

    @field_validator("windows")
    @classmethod
    def _windows_valid(cls, v: list[list[int]]) -> list[list[int]]:
        prev_end = None
        for pair in v:
            start, end = pair
            if not (0 <= start <= MAX_TIME and 0 <= end <= MAX_TIME):
                raise ValueError("窗口时刻必须在 [0, 10^12] 内")
            if start >= end:
                raise ValueError("每个窗口必须满足 start < end")
            if prev_end is not None and start < prev_end:
                raise ValueError("窗口须按 start 升序且互不重叠（可相接）")
            prev_end = end
        return v


class CalendarIn(StrictModel):
    gates: list[GateIn] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_gate_ids(self) -> "CalendarIn":
        ids = [g.gate_id for g in self.gates]
        if len(set(ids)) != len(ids):
            raise ValueError("闸门 ID 不得重复")
        return self


class GateOut(BaseModel):
    gate_id: str
    windows: list[list[int]]


class CalendarOut(BaseModel):
    id: str
    gates: list[GateOut]


class VoyageIn(StrictModel):
    calendar_id: str
    gates: list[str] = Field(min_length=1, max_length=MAX_GATES)
    legs: list[int] = Field(min_length=0, max_length=MAX_GATES - 1)
    max_waits: list[int] = Field(min_length=0, max_length=MAX_GATES)
    search_start: int = Field(ge=0, le=MAX_TIME)
    search_end: int = Field(ge=0, le=MAX_TIME)

    @model_validator(mode="after")
    def _shape_ok(self) -> "VoyageIn":
        n = len(self.gates)
        if len(set(self.gates)) != n:
            raise ValueError("航程闸门必须互异")
        if len(self.legs) != n - 1:
            raise ValueError("legs 长度必须为闸门数 - 1")
        if len(self.max_waits) != n:
            raise ValueError("max_waits 长度必须等于闸门数")
        if any(x < 0 or x > MAX_TIME for x in self.legs):
            raise ValueError("航行时长必须在 [0, 10^12] 内")
        if any(x < 0 or x > MAX_TIME for x in self.max_waits):
            raise ValueError("最大等待时长必须在 [0, 10^12] 内")
        if self.search_start >= self.search_end:
            raise ValueError("出发搜索区间必须满足 start < end")
        return self


class PlanIn(StrictModel):
    calendar_id: str
    gates: list[str] = Field(min_length=1, max_length=MAX_GATES)
    legs: list[int] = Field(min_length=0, max_length=MAX_GATES - 1)
    max_waits: list[int] = Field(min_length=0, max_length=MAX_GATES)
    departure: int = Field(ge=0, le=MAX_TIME)

    @model_validator(mode="after")
    def _shape_ok(self) -> "PlanIn":
        n = len(self.gates)
        if len(set(self.gates)) != n:
            raise ValueError("航程闸门必须互异")
        if len(self.legs) != n - 1:
            raise ValueError("legs 长度必须为闸门数 - 1")
        if len(self.max_waits) != n:
            raise ValueError("max_waits 长度必须等于闸门数")
        if any(x < 0 or x > MAX_TIME for x in self.legs):
            raise ValueError("航行时长必须在 [0, 10^12] 内")
        if any(x < 0 or x > MAX_TIME for x in self.max_waits):
            raise ValueError("最大等待时长必须在 [0, 10^12] 内")
        return self


class WitnessEntry(BaseModel):
    gate_id: str
    arrival: int
    entry: int
    wait: int


class ProbeResponse(BaseModel):
    intervals: list[list[int]]


class PlanOut(BaseModel):
    id: str
    calendar_id: str
    gates: list[str]
    legs: list[int]
    max_waits: list[int]
    departure: int
    witnesses: list[WitnessEntry]


class ReplayResponse(BaseModel):
    status: str  # STILL_VALID | INVALID
    failed_gate_id: str | None = None
    failed_index: int | None = None
    arrival: int | None = None
    wait_deadline: int | None = None
    reason: str | None = None
