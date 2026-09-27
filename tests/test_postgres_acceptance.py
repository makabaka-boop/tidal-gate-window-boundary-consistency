"""真实 PostgreSQL 验收：损坏旧记录的稳定拒绝 + 重启后确定性 + 合法旧记录可用。

与单元测试不同，本模块：
1. 直接在真实 PostgreSQL 中预置「升级/恢复后」的坏数据
   （重复闸门、重复 position、损坏窗口、非法边界、损坏方案）；
2. 对每项异常**重复查询**，并在其间**重启 uvicorn 服务进程**，
   核对异常结果（状态码、错误类型、完整响应体）跨重启完全一致；
3. 失败前后对 calendars/gates/plans 表做逐项快照比对，确认任何
   异常路径都不新增方案、不改写原有日历、方案与见证；
4. 最后验证完整的合法旧记录仍能完成探测、采纳和跨日历重放。

运行方式（需一个可写的真实 PostgreSQL 库）::

    PG_ACCEPTANCE=1 \
    PG_ACCEPTANCE_URL=postgresql+psycopg://user:pass@host:port/db \
    python3 -m pytest tests/test_postgres_acceptance.py -s

默认跳过；SQLite 环境不参与本验收。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import closing

import httpx
import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.skipif(
    os.environ.get("PG_ACCEPTANCE") != "1",
    reason="需真实 PostgreSQL：设置 PG_ACCEPTANCE=1 与 PG_ACCEPTANCE_URL",
)

DB_URL = os.environ.get(
    "PG_ACCEPTANCE_URL",
    "postgresql+psycopg://tide@localhost:5432/tide_acceptance",
)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAIT_TIMEOUT = 30.0


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """以独立进程运行 uvicorn，模拟调度员在升级/恢复后重启服务。"""

    def __init__(self, port: int, log_path: str) -> None:
        self.port = port
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        env = dict(os.environ)
        env["DATABASE_URL"] = DB_URL
        env["PYTHONPATH"] = ROOT
        log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + WAIT_TIMEOUT
        last = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("uvicorn 提前退出，见 " + self.log_path)
            try:
                r = httpx.get(f"http://127.0.0.1:{self.port}/health", timeout=1)
                if r.status_code == 200:
                    return
            except httpx.TransportError as exc:
                last = exc
            time.sleep(0.25)
        raise RuntimeError(f"服务未就绪: {last}")

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.proc = None

    def restart(self) -> None:
        self.stop()
        self.start()


# 各异常日历使用的预置闸门行：(position, gate_id, windows 原始文本)
CORRUPT_CALENDARS: dict[str, dict] = {
    "dup_gid": {
        "type": "CALENDAR_DUPLICATE_GATE_ID",
        "gates": [
            (0, "G1", json.dumps([[10, 20]])),
            (1, "G1", json.dumps([[30, 40]])),
        ],
    },
    "dup_pos": {
        "type": "CALENDAR_DUPLICATE_POSITION",
        "gates": [
            (0, "G1", json.dumps([[10, 20]])),
            (0, "G2", json.dumps([[30, 40]])),
        ],
    },
    "win_not_json": {
        "type": "CALENDAR_GATE_WINDOWS_NOT_JSON",
        "gates": [(0, "G1", "{oops")],
    },
    "win_shape": {
        "type": "CALENDAR_WINDOWS_MALFORMED",
        "gates": [(0, "G1", json.dumps([[10]]))],
    },
    "win_boundary": {
        "type": "CALENDAR_WINDOW_OUT_OF_DOMAIN",
        "gates": [(0, "G1", json.dumps([[0, 10**12 + 1]]))],
    },
    "win_inverted": {
        "type": "CALENDAR_WINDOW_INVERTED",
        "gates": [(0, "G1", json.dumps([[20, 10]]))],
    },
    "win_overlap": {
        "type": "CALENDAR_WINDOWS_OVERLAP",
        "gates": [(0, "G1", json.dumps([[0, 10], [9, 20]]))],
    },
    "no_gates": {"type": "CALENDAR_NO_GATES", "gates": []},
}

LEGACY_DEFINITION = {
    "gates": ["G1", "G2"],
    "legs": [5],
    "max_waits": [5, 5],
}
LEGACY_WITNESSES = [
    {"gate_id": "G1", "arrival": 15, "entry": 15, "wait": 0},
    {"gate_id": "G2", "arrival": 20, "entry": 25, "wait": 5},
]


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(DB_URL, future=True)
    try:
        with eng.connect() as conn:
            conn.execute(
                text(
                    "DROP TABLE IF EXISTS gates, plans, calendars CASCADE"
                )
            )
            conn.commit()
    except Exception as exc:  # pragma: no cover - 环境不可用时跳过
        pytest.skip(f"无法连接真实 PostgreSQL {DB_URL}: {exc}")
    # 让应用自己建表
    env = dict(os.environ, DATABASE_URL=DB_URL, PYTHONPATH=ROOT)
    subprocess.run(
        [sys.executable, "-c", "from app.database import init_db; init_db()"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def server(engine, tmp_path_factory):
    log_path = str(tmp_path_factory.mktemp("pgacc") / "uvicorn.log")
    srv = Server(_free_port(), log_path)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def http(server):
    return httpx.Client(base_url=f"http://127.0.0.1:{server.port}", timeout=10)


@pytest.fixture(scope="module", autouse=True)
def seed(engine):
    """预置全部坏日历、坏方案与一组完整合法旧记录。"""
    with engine.begin() as conn:
        # ---- 损坏日历 ----
        for cid, spec in CORRUPT_CALENDARS.items():
            conn.execute(
                text("INSERT INTO calendars (id) VALUES (:id)"), {"id": cid}
            )
            for position, gid, raw in spec["gates"]:
                conn.execute(
                    text(
                        "INSERT INTO gates (calendar_id, position, gate_id, "
                        "windows) VALUES (:c, :p, :g, :w)"
                    ),
                    {"c": cid, "p": position, "g": gid, "w": raw},
                )

        # ---- 损坏方案（挂在合法日历 good_cal 上）----
        conn.execute(text("INSERT INTO calendars (id) VALUES ('good_cal')"))
        conn.execute(
            text(
                "INSERT INTO gates (calendar_id, position, gate_id, windows) "
                "VALUES ('good_cal', 0, 'G1', :w)"
            ),
            {"w": json.dumps([[0, 100]])},
        )
        conn.execute(
            text(
                "INSERT INTO plans (id, calendar_id, payload, departure, "
                "witnesses) VALUES ('plan_bad_payload', 'good_cal', :p, 10, :w)"
            ),
            {"p": "{broken", "w": json.dumps(LEGACY_WITNESSES[:1])},
        )
        conn.execute(
            text(
                "INSERT INTO plans (id, calendar_id, payload, departure, "
                "witnesses) VALUES ('plan_bad_def', 'good_cal', :p, 10, :w)"
            ),
            {
                "p": json.dumps({"gates": ["G1"], "legs": [9], "max_waits": [1]}),
                "w": json.dumps(LEGACY_WITNESSES[:1]),
            },
        )
        conn.execute(
            text(
                "INSERT INTO plans (id, calendar_id, payload, departure, "
                "witnesses) VALUES ('plan_bad_wit', 'good_cal', :p, 10, :w)"
            ),
            {
                "p": json.dumps(LEGACY_DEFINITION),
                "w": "not-json",
            },
        )
        conn.execute(
            text(
                "INSERT INTO plans (id, calendar_id, payload, departure, "
                "witnesses) VALUES ('plan_wit_shape', 'good_cal', :p, 10, :w)"
            ),
            {
                "p": json.dumps(LEGACY_DEFINITION),
                "w": json.dumps([{"gate_id": "G1"}] * 2),
            },
        )
        conn.execute(
            text(
                "INSERT INTO plans (id, calendar_id, payload, departure, "
                "witnesses) VALUES ('plan_wit_mismatch', 'good_cal', :p, 10, :w)"
            ),
            {
                "p": json.dumps(LEGACY_DEFINITION),
                "w": json.dumps(
                    [
                        {"gate_id": "G1", "arrival": 15, "entry": 15, "wait": 0},
                        {"gate_id": "G2", "arrival": 20, "entry": 25, "wait": 99},
                    ]
                ),
            },
        )
        # 结构与每项等待算术自洽，但后闸到达不等于前闸入闸+航行时长
        conn.execute(
            text(
                "INSERT INTO plans (id, calendar_id, payload, departure, "
                "witnesses) VALUES ('plan_wit_bad_recurrence', 'legacy', :p, 15, :w)"
            ),
            {
                "p": json.dumps(LEGACY_DEFINITION),
                "w": json.dumps(
                    [
                        {"gate_id": "G1", "arrival": 15, "entry": 15, "wait": 0},
                        {"gate_id": "G2", "arrival": 21, "entry": 25, "wait": 4},
                    ]
                ),
            },
        )
        # 结构与每项等待算术自洽，但入闸不是原日历最早开放时刻
        conn.execute(
            text(
                "INSERT INTO plans (id, calendar_id, payload, departure, "
                "witnesses) VALUES ('plan_wit_not_earliest', 'legacy', :p, 15, :w)"
            ),
            {
                "p": json.dumps(LEGACY_DEFINITION),
                "w": json.dumps(
                    [
                        {"gate_id": "G1", "arrival": 15, "entry": 16, "wait": 1},
                        {"gate_id": "G2", "arrival": 21, "entry": 25, "wait": 4},
                    ]
                ),
            },
        )

        # ---- 完整合法旧日历（两闸）与旧方案 ----
        conn.execute(text("INSERT INTO calendars (id) VALUES ('legacy')"))
        for p, gid, windows in (
            (0, "G1", json.dumps([[10, 20]])),
            (1, "G2", json.dumps([[25, 35]])),
        ):
            conn.execute(
                text(
                    "INSERT INTO gates (calendar_id, position, gate_id, windows) "
                    "VALUES ('legacy', :p, :g, :w)"
                ),
                {"p": p, "g": gid, "w": windows},
            )
        conn.execute(
            text(
                "INSERT INTO plans (id, calendar_id, payload, departure, "
                "witnesses) VALUES ('legacy_plan', 'legacy', :p, 15, :w)"
            ),
            {
                "p": json.dumps(LEGACY_DEFINITION),
                "w": json.dumps(LEGACY_WITNESSES),
            },
        )
        # 跨日历重放目标：G2 右移 -> INVALID
        conn.execute(text("INSERT INTO calendars (id) VALUES ('legacy_new')"))
        for p, gid, windows in (
            (0, "G1", json.dumps([[10, 20]])),
            (1, "G2", json.dumps([[26, 35]])),
        ):
            conn.execute(
                text(
                    "INSERT INTO gates (calendar_id, position, gate_id, windows) "
                    "VALUES ('legacy_new', :p, :g, :w)"
                ),
                {"p": p, "g": gid, "w": windows},
            )
        # 跨日历重放目标：同结构 -> STILL_VALID（稍后由合法流程复用）
        conn.execute(text("INSERT INTO calendars (id) VALUES ('legacy_same')"))
        for p, gid, windows in (
            (0, "G1", json.dumps([[10, 20]])),
            (1, "G2", json.dumps([[25, 35]])),
        ):
            conn.execute(
                text(
                    "INSERT INTO gates (calendar_id, position, gate_id, windows) "
                    "VALUES ('legacy_same', :p, :g, :w)"
                ),
                {"p": p, "g": gid, "w": windows},
            )


def _snapshot(engine) -> dict:
    """三张表全部内容的逐项快照（按稳定顺序排序）。"""
    with engine.connect() as conn:
        calendars = sorted(
            r[0]
            for r in conn.execute(text("SELECT id FROM calendars"))
        )
        gates = sorted(
            tuple(r)
            for r in conn.execute(
                text(
                    "SELECT calendar_id, position, gate_id, windows "
                    "FROM gates ORDER BY calendar_id, position, id"
                )
            )
        )
        plans = sorted(
            tuple(r)
            for r in conn.execute(
                text(
                    "SELECT id, calendar_id, payload, departure, witnesses "
                    "FROM plans ORDER BY id"
                )
            )
        )
    return {"calendars": calendars, "gates": gates, "plans": plans}


PROBE_BODY = {
    "gates": ["G1"],
    "legs": [],
    "max_waits": [100],
    "search_start": 0,
    "search_end": 50,
}
ADOPT_BODY = {
    "gates": ["G1"],
    "legs": [],
    "max_waits": [100],
    "departure": 15,
}


def _anomaly_calls(http, cid):
    """对一个损坏日历执行：详情、探测、采纳、以旧方案重放。"""
    return [
        ("GET", http.get(f"/calendars/{cid}")),
        (
            "PROBE",
            http.post(
                "/voyages/probe", json={"calendar_id": cid, **PROBE_BODY}
            ),
        ),
        (
            "ADOPT",
            http.post("/plans", json={"calendar_id": cid, **ADOPT_BODY}),
        ),
        (
            "REPLAY",
            http.post(f"/plans/legacy_plan/replay/{cid}"),
        ),
    ]


def test_01_corrupt_calendars_anomaly_deterministic_across_restart(
    http, engine, server
):
    snapshot_before = _snapshot(engine)

    def collect():
        out = {}
        for cid, spec in CORRUPT_CALENDARS.items():
            out[cid] = sorted(
                (name, r.status_code, r.json())
                for name, r in _anomaly_calls(http, cid)
            )
        return out

    first = collect()
    # 重启服务进程后再次重复查询：结果必须逐项一致
    server.restart()
    second = collect()
    assert second == first

    # 每项调用都必须是 422 + 对应的稳定错误类型（绝不 500/409/200）
    for cid, spec in CORRUPT_CALENDARS.items():
        expected = spec["type"]
        for name, status, body in first[cid]:
            assert status == 422, (cid, name, status, body)
            detail = body["detail"]
            assert detail["error"] == "DATA_ANOMALY", (cid, name, body)
            assert detail["type"] == expected, (cid, name, body)

    # 详情接口不得再把重复闸门同时返回为两个 gate
    dup = http.get("/calendars/dup_gid")
    assert dup.status_code == 422
    assert dup.json()["detail"]["type"] == "CALENDAR_DUPLICATE_GATE_ID"

    # 重复 position 不再导致顺序不稳定：永远是同一个异常
    assert snapshot_before == _snapshot(engine)


def test_02_corrupt_plans_anomaly_deterministic_across_restart(
    http, engine, server
):
    snapshot_before = _snapshot(engine)
    bad_plans = {
        "plan_bad_payload": "PLAN_PAYLOAD_NOT_JSON",
        "plan_bad_def": "PLAN_DEFINITION_MALFORMED",
        "plan_bad_wit": "PLAN_WITNESSES_NOT_JSON",
        "plan_wit_shape": "PLAN_WITNESSES_SHAPE_MALFORMED",
        "plan_wit_mismatch": "PLAN_WITNESS_MISMATCH",
        "plan_wit_bad_recurrence": "PLAN_WITNESS_MISMATCH",
        "plan_wit_not_earliest": "PLAN_WITNESS_MISMATCH",
    }

    def collect():
        out = {}
        for pid in bad_plans:
            got = http.get(f"/plans/{pid}")
            rep = http.post(f"/plans/{pid}/replay/legacy_same")
            out[pid] = sorted(
                [
                    ("GET", got.status_code, got.json()),
                    ("REPLAY", rep.status_code, rep.json()),
                ]
            )
        return out

    first = collect()
    server.restart()
    second = collect()
    assert second == first
    for pid, expected in bad_plans.items():
        for name, status, body in first[pid]:
            assert status == 422, (pid, name, status, body)
            assert body["detail"]["error"] == "DATA_ANOMALY"
            assert body["detail"]["type"] == expected, (pid, name, body)

    # 原损坏方案一行都未被改写
    assert snapshot_before == _snapshot(engine)


def test_03_anomalies_create_no_rows_and_leave_tables_unchanged(http, engine):
    # 对全部异常对象再打一轮，确认 plans/calendars/gates 无任何变化
    snapshot_before = _snapshot(engine)
    for cid in CORRUPT_CALENDARS:
        _anomaly_calls(http, cid)
    for pid in (
        "plan_bad_payload",
        "plan_bad_def",
        "plan_bad_wit",
        "plan_wit_shape",
        "plan_wit_mismatch",
        "plan_wit_bad_recurrence",
        "plan_wit_not_earliest",
    ):
        http.get(f"/plans/{pid}")
        http.post(f"/plans/{pid}/replay/legacy_same")
    assert _snapshot(engine) == snapshot_before


def test_04_legal_legacy_probe_adopt_replay_after_restart(http, engine, server):
    # 重启后完整旧记录仍工作
    server.restart()

    # 探测区间与升级前一致
    r = http.post(
        "/voyages/probe",
        json={
            "calendar_id": "legacy",
            "gates": ["G1", "G2"],
            "legs": [5],
            "max_waits": [5, 5],
            "search_start": 0,
            "search_end": 50,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["intervals"] == [[15, 20]]

    # 旧方案见证读回不变
    r = http.get("/plans/legacy_plan")
    assert r.status_code == 200
    plan_body = r.json()
    assert plan_body["departure"] == 15
    assert plan_body["witnesses"] == LEGACY_WITNESSES

    # 跨日历重放：同结构 STILL_VALID；右移日历 INVALID
    r = http.post("/plans/legacy_plan/replay/legacy_same")
    assert r.status_code == 200
    assert r.json()["status"] == "STILL_VALID"
    r = http.post("/plans/legacy_plan/replay/legacy_new")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "INVALID"
    assert body["failed_gate_id"] == "G2"
    assert body["failed_index"] == 1
    assert body["arrival"] == 20
    assert body["wait_deadline"] == 25

    # 采纳新方案成功（这是异常场景之外唯一允许的新增），且不影响旧方案
    before_plan_ids = {
        r[0] for r in _snapshot(engine)["plans"]
    }
    r = http.post(
        "/plans",
        json={
            "calendar_id": "legacy",
            "gates": ["G1", "G2"],
            "legs": [5],
            "max_waits": [5, 5],
            "departure": 15,
        },
    )
    assert r.status_code == 201, r.text
    new_pid = r.json()["id"]
    assert new_pid not in before_plan_ids
    assert r.json()["witnesses"] == LEGACY_WITNESSES

    # 原旧方案与日历未被改写
    assert http.get("/plans/legacy_plan").json()["witnesses"] == LEGACY_WITNESSES
    cal = http.get("/calendars/legacy").json()
    assert cal["gates"] == [
        {"gate_id": "G1", "windows": [[10, 20]]},
        {"gate_id": "G2", "windows": [[25, 35]]},
    ]

    # 新方案同样可跨日历重放
    r = http.post(f"/plans/{new_pid}/replay/legacy_new")
    assert r.json()["status"] == "INVALID"
