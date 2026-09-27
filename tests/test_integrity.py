"""既有持久化记录损坏时的完整性校验：稳定 422、确定结果、绝不改写。

这些用例直接向表中预置「升级/恢复后可能出现」的坏数据（重复闸门、
重复 position、损坏窗口、非法边界、损坏方案），验证读取、探测、采纳
与重放都返回可区分的 DATA_ANOMALY 响应，而不是 500 或静默挑选记录。
"""
from __future__ import annotations

import json

import pytest

from app import integrity
from app.database import CalendarRow, GateRow, PlanRow


def _seed_calendar(db, cid, gates):
    """gates: [(position, gate_id, windows_raw_json|list), ...]"""
    db.add(CalendarRow(id=cid))
    for position, gid, windows in gates:
        raw = windows if isinstance(windows, str) else json.dumps(windows)
        db.add(
            GateRow(
                calendar_id=cid,
                position=position,
                gate_id=gid,
                windows=raw,
            )
        )
    db.commit()


def _seed_plan(db, pid, cid, payload, departure, witnesses):
    db.add(
        PlanRow(
            id=pid,
            calendar_id=cid,
            payload=payload if isinstance(payload, str) else json.dumps(payload),
            departure=departure,
            witnesses=(
                witnesses
                if isinstance(witnesses, str)
                else json.dumps(witnesses)
            ),
        )
    )
    db.commit()


def _definition(gates=("G1",), legs=None, waits=None):
    n = len(gates)
    return {
        "gates": list(gates),
        "legs": list(legs if legs is not None else [0] * (n - 1)),
        "max_waits": list(waits if waits is not None else [100] * n),
    }


def _witnesses(gates=("G1",)):
    return [
        {"gate_id": g, "arrival": 10, "entry": 10, "wait": 0} for g in gates
    ]


@pytest.fixture()
def db(client):
    """复用 client 的全新内存表，直接拿一个 Session 预置坏数据。"""
    from app.database import SessionLocal

    session = SessionLocal()
    yield session
    session.close()


VOYAGE_BODY = {
    "gates": ["G1"],
    "legs": [],
    "max_waits": [100],
    "search_start": 0,
    "search_end": 50,
}
PLAN_BODY = {
    "gates": ["G1"],
    "legs": [],
    "max_waits": [100],
    "departure": 10,
}


def _assert_anomaly(resp, expected_type):
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "DATA_ANOMALY"
    assert detail["type"] == expected_type
    return detail


def test_duplicate_gate_id_all_endpoints_422(client, db):
    _seed_calendar(
        db,
        "dup_gid",
        [
            (0, "G1", [[10, 20]]),
            (1, "G1", [[30, 40]]),
        ],
    )
    # 详情不再同时返回两项
    _assert_anomaly(
        client.get("/calendars/dup_gid"), integrity.CALENDAR_DUPLICATE_GATE_ID
    )
    body = {**VOYAGE_BODY, "calendar_id": "dup_gid"}
    _assert_anomaly(
        client.post("/voyages/probe", json=body),
        integrity.CALENDAR_DUPLICATE_GATE_ID,
    )
    body = {**PLAN_BODY, "calendar_id": "dup_gid"}
    _assert_anomaly(
        client.post("/plans", json=body), integrity.CALENDAR_DUPLICATE_GATE_ID
    )
    # 重放也必须先拒绝损坏日历
    _seed_plan(
        db, "p1", "other", _definition(), 10, _witnesses()
    )
    _assert_anomaly(
        client.post("/plans/p1/replay/dup_gid"),
        integrity.CALENDAR_DUPLICATE_GATE_ID,
    )


def test_duplicate_position_is_stable_anomaly(client, db):
    _seed_calendar(
        db,
        "dup_pos",
        [
            (0, "G1", [[10, 20]]),
            (0, "G2", [[30, 40]]),
        ],
    )
    responses = [
        _assert_anomaly(
            client.get("/calendars/dup_pos"),
            integrity.CALENDAR_DUPLICATE_POSITION,
        )
        for _ in range(3)
    ]
    # 定位上下文稳定
    assert all(r["position"] == 0 for r in responses)
    # 先报顺序冲突，不被重复 gate_id 掩盖
    _seed_calendar(
        db,
        "dup_both",
        [
            (0, "G1", [[10, 20]]),
            (0, "G1", [[30, 40]]),
        ],
    )
    _assert_anomaly(
        client.get("/calendars/dup_both"),
        integrity.CALENDAR_DUPLICATE_POSITION,
    )


def test_windows_not_json(client, db):
    _seed_calendar(db, "bad_json", [(0, "G1", "{not json")])
    _assert_anomaly(
        client.get("/calendars/bad_json"),
        integrity.CALENDAR_GATE_WINDOWS_NOT_JSON,
    )
    body = {**VOYAGE_BODY, "calendar_id": "bad_json"}
    _assert_anomaly(
        client.post("/voyages/probe", json=body),
        integrity.CALENDAR_GATE_WINDOWS_NOT_JSON,
    )


def test_windows_malformed_shapes(client, db):
    cases = {
        "not_array": "123",
        "empty": [],
        "pair_len_1": [[10]],
        "pair_len_3": [[10, 20, 30]],
        "not_ints": [[10, "20"]],
        "bool_pair": [[True, 20]],
    }
    for name, windows in cases.items():
        cid = f"wm_{name}"
        _seed_calendar(db, cid, [(0, "G1", windows)])
        _assert_anomaly(
            client.get(f"/calendars/{cid}"),
            integrity.CALENDAR_WINDOWS_MALFORMED,
        )


def test_window_out_of_domain(client, db):
    for name, windows in (
        ("neg", [[-1, 10]]),
        ("too_big", [[0, 10**12 + 1]]),
    ):
        cid = f"wo_{name}"
        _seed_calendar(db, cid, [(0, "G1", windows)])
        d = _assert_anomaly(
            client.get(f"/calendars/{cid}"),
            integrity.CALENDAR_WINDOW_OUT_OF_DOMAIN,
        )
        assert d["window_index"] == 0


def test_window_inverted_and_overlap(client, db):
    _seed_calendar(db, "inv", [(0, "G1", [[20, 20]])])
    _assert_anomaly(
        client.get("/calendars/inv"), integrity.CALENDAR_WINDOW_INVERTED
    )
    _seed_calendar(db, "inv2", [(0, "G1", [[30, 20]])])
    _assert_anomaly(
        client.get("/calendars/inv2"), integrity.CALENDAR_WINDOW_INVERTED
    )
    # 重叠（含倒置顺序）
    _seed_calendar(db, "ovl", [(0, "G1", [[0, 10], [9, 20]])])
    _assert_anomaly(
        client.get("/calendars/ovl"), integrity.CALENDAR_WINDOWS_OVERLAP
    )
    _seed_calendar(db, "ovl2", [(0, "G1", [[30, 40], [0, 10]])])
    _assert_anomaly(
        client.get("/calendars/ovl2"), integrity.CALENDAR_WINDOWS_OVERLAP
    )
    # 相接合法
    _seed_calendar(db, "touch", [(0, "G1", [[10, 20], [20, 30]])])
    assert client.get("/calendars/touch").status_code == 200


def test_no_gates_calendar(client, db):
    db.add(CalendarRow(id="hollow"))
    db.commit()
    _assert_anomaly(
        client.get("/calendars/hollow"), integrity.CALENDAR_NO_GATES
    )


def test_corrupt_plan_payload_variants(client, db):
    _seed_calendar(db, "good_cal", [(0, "G1", [[0, 100]])])
    # payload 不是 JSON
    _seed_plan(db, "pj", "good_cal", "xxx", 10, _witnesses())
    _assert_anomaly(
        client.get("/plans/pj"), integrity.PLAN_PAYLOAD_NOT_JSON
    )
    # 定义结构损坏
    bad_defs = [
        "[]",  # 不是对象
        {**_definition(), "gates": []},
        {**_definition(), "gates": ["A", "A"]},
        {**_definition(("A", "B")), "legs": []},  # legs 长度不符
        {**_definition(), "max_waits": [1, 2]},
        {**_definition(), "legs": [-1]},
    ]
    for i, payload in enumerate(bad_defs):
        pid = f"pd_{i}"
        _seed_plan(db, pid, "good_cal", payload, 10, _witnesses())
        _assert_anomaly(
            client.get(f"/plans/{pid}"),
            integrity.PLAN_DEFINITION_MALFORMED,
        )


def test_corrupt_witnesses(client, db):
    _seed_calendar(db, "good_cal", [(0, "G1", [[0, 100]])])
    # 见证不是 JSON
    _seed_plan(db, "wj", "good_cal", _definition(), 10, "nope")
    _assert_anomaly(
        client.get("/plans/wj"), integrity.PLAN_WITNESSES_NOT_JSON
    )
    # 见证结构损坏
    shape_bad = ["[]", json.dumps({}), json.dumps([{"gate_id": "G1"}])]
    for i, witnesses in enumerate(shape_bad):
        pid = f"ws_{i}"
        _seed_plan(db, pid, "good_cal", _definition(), 10, witnesses)
        _assert_anomaly(
            client.get(f"/plans/{pid}"),
            integrity.PLAN_WITNESSES_SHAPE_MALFORMED,
        )
    # 见证与定义不一致：闸门次序对不上
    _seed_plan(
        db,
        "wm_1",
        "good_cal",
        _definition(("G1", "G2")),
        10,
        [
            {"gate_id": "G2", "arrival": 10, "entry": 10, "wait": 0},
            {"gate_id": "G1", "arrival": 10, "entry": 10, "wait": 0},
        ],
    )
    _assert_anomaly(
        client.get("/plans/wm_1"), integrity.PLAN_WITNESS_MISMATCH
    )
    # wait != entry - arrival
    _seed_plan(
        db,
        "wm_2",
        "good_cal",
        _definition(),
        10,
        [{"gate_id": "G1", "arrival": 10, "entry": 15, "wait": 99}],
    )
    _assert_anomaly(
        client.get("/plans/wm_2"), integrity.PLAN_WITNESS_MISMATCH
    )


def test_replay_corrupt_plan_is_anomaly_even_if_calendar_ok(client, db):
    _seed_calendar(db, "good_cal", [(0, "G1", [[0, 100]])])
    _seed_plan(db, "badp", "good_cal", "xxx", 10, _witnesses())
    _assert_anomaly(
        client.post("/plans/badp/replay/good_cal"),
        integrity.PLAN_PAYLOAD_NOT_JSON,
    )


def test_anomalies_are_deterministic_and_read_only(client, db):
    _seed_calendar(
        db,
        "corrupt",
        [(0, "G1", [[10, 20]]), (1, "G1", "bogus")],
    )

    def snapshot():
        return {
            "calendars": sorted(
                x.id for x in db.query(CalendarRow).all()
            ),
            "gates": sorted(
                (g.position, g.gate_id, g.windows)
                for g in db.query(GateRow)
                .filter(GateRow.calendar_id == "corrupt")
                .all()
            ),
            "plans": sorted(p.id for p in db.query(PlanRow).all()),
        }

    before = snapshot()
    bodies = []
    for _ in range(3):
        r = client.get("/calendars/corrupt")
        bodies.append(r.json())
        body = {**VOYAGE_BODY, "calendar_id": "corrupt"}
        bodies.append(client.post("/voyages/probe", json=body).json())
        body = {**PLAN_BODY, "calendar_id": "corrupt"}
        bodies.append(client.post("/plans", json=body).json())
    # 结果逐字节稳定
    for b in bodies[1:]:
        assert b == bodies[0]
    # 失败前后表内容逐项不变；且没有新增方案
    after = snapshot()
    assert after == before
    assert after["plans"] == []


def test_legal_legacy_record_works_unchanged(client, db):
    """直接预置的完整旧记录：区间、见证、重放与升级前一致。"""
    _seed_calendar(
        db,
        "legacy",
        [(0, "G1", [[10, 20]]), (1, "G2", [[25, 35]])],
    )
    cid = "legacy"
    resp = client.post(
        "/voyages/probe",
        json={
            "calendar_id": cid,
            "gates": ["G1", "G2"],
            "legs": [5],
            "max_waits": [5, 5],
            "search_start": 0,
            "search_end": 50,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["intervals"] == [[15, 20]]

    # 预置一个旧方案并读回
    witnesses = [
        {"gate_id": "G1", "arrival": 15, "entry": 15, "wait": 0},
        {"gate_id": "G2", "arrival": 20, "entry": 25, "wait": 5},
    ]
    _seed_plan(
        db,
        "legacy_plan",
        cid,
        _definition(("G1", "G2"), legs=[5], waits=[5, 5]),
        15,
        witnesses,
    )
    got = client.get("/plans/legacy_plan").json()
    assert got["witnesses"] == witnesses
    # 跨日历重放：新日历窗口右移 -> INVALID
    _seed_calendar(
        db,
        "legacy_new",
        [(0, "G1", [[10, 20]]), (1, "G2", [[26, 35]])],
    )
    body = client.post("/plans/legacy_plan/replay/legacy_new").json()
    assert body["status"] == "INVALID"
    assert body["failed_gate_id"] == "G2"
    assert body["arrival"] == 20
    assert body["wait_deadline"] == 25
    # 原方案未被改写
    assert client.get("/plans/legacy_plan").json()["witnesses"] == witnesses


def _seed_three_gate_world(db):
    """合法三闸日历 + 其真实采纳见证（出发15）。"""
    _seed_calendar(
        db,
        "sem_cal",
        [
            (0, "G1", [[10, 20]]),
            (1, "G2", [[25, 35]]),
            (2, "G3", [[35, 45]]),
        ],
    )
    definition = {
        "gates": ["G1", "G2", "G3"],
        "legs": [5, 10],
        "max_waits": [5, 5, 5],
    }
    good_witnesses = [
        {"gate_id": "G1", "arrival": 15, "entry": 15, "wait": 0},
        {"gate_id": "G2", "arrival": 20, "entry": 25, "wait": 5},
        {"gate_id": "G3", "arrival": 35, "entry": 35, "wait": 0},
    ]
    return definition, good_witnesses


def test_witness_bad_arrival_recurrence_rejected(client, db):
    """结构自洽、每项等待算术合法，但后闸到达 != 前闸入闸+leg。"""
    definition, good = _seed_three_gate_world(db)
    bad = [
        {"gate_id": "G1", "arrival": 15, "entry": 15, "wait": 0},
        {"gate_id": "G2", "arrival": 21, "entry": 25, "wait": 4},  # 应为20
        {"gate_id": "G3", "arrival": 35, "entry": 35, "wait": 0},
    ]
    _seed_plan(db, "sem_bad_recurrence", "sem_cal", definition, 15, bad)

    # 查询与重放都不得把它当成有效采纳快照
    detail = _assert_anomaly(
        client.get("/plans/sem_bad_recurrence"),
        integrity.PLAN_WITNESS_MISMATCH,
    )
    assert detail["witness_index"] == 1
    assert detail["expected_arrival"] == 20
    _assert_anomaly(
        client.post("/plans/sem_bad_recurrence/replay/sem_cal"),
        integrity.PLAN_WITNESS_MISMATCH,
    )

    # 原表逐字节不变；响应重复查询稳定
    row = db.get(PlanRow, "sem_bad_recurrence")
    assert json.loads(row.witnesses)[1] == bad[1]
    again = client.get("/plans/sem_bad_recurrence").json()
    assert again["detail"] == detail

    # 合法方案继续可读
    _seed_plan(db, "sem_good", "sem_cal", definition, 15, good)
    assert client.get("/plans/sem_good").status_code == 200
    assert (
        client.post("/plans/sem_good/replay/sem_cal").json()["status"]
        == "STILL_VALID"
    )


def test_witness_not_earliest_entry_rejected(client, db):
    """结构自洽、等待算术合法，但入闸不是原日历最早开放时刻。"""
    definition, good = _seed_three_gate_world(db)
    # 15 落在 G1 [10,20) 内必须立即入闸，伪造成 16（wait=1 算术自洽）
    bad = [
        {"gate_id": "G1", "arrival": 15, "entry": 16, "wait": 1},
        {"gate_id": "G2", "arrival": 21, "entry": 25, "wait": 4},
        {"gate_id": "G3", "arrival": 35, "entry": 35, "wait": 0},
    ]
    _seed_plan(db, "sem_bad_earliest", "sem_cal", definition, 15, bad)
    detail = _assert_anomaly(
        client.get("/plans/sem_bad_earliest"),
        integrity.PLAN_WITNESS_MISMATCH,
    )
    assert detail["witness_index"] == 0
    assert detail["expected_entry"] == 15
    _assert_anomaly(
        client.post("/plans/sem_bad_earliest/replay/sem_cal"),
        integrity.PLAN_WITNESS_MISMATCH,
    )

    # 恰在 end 到达判为关闭：G2 到达 20 不得入 [10,20) 风格的伪造窗
    # —— 直接把末闸到达安排在窗口 end 上验证最早可入规则
    _seed_calendar(db, "edge_cal", [(0, "G1", [[10, 20]])])
    edge_def = {"gates": ["G1"], "legs": [], "max_waits": [10]}
    edge_bad = [{"gate_id": "G1", "arrival": 20, "entry": 20, "wait": 0}]
    _seed_plan(db, "sem_edge", "edge_cal", edge_def, 20, edge_bad)
    detail = _assert_anomaly(
        client.get("/plans/sem_edge"), integrity.PLAN_WITNESS_MISMATCH
    )
    # 20 在 end 上，其后无窗：没有任何可入时刻（详情里 expected_entry 为 null）
    assert detail.get("expected_entry") is None

    # 两张坏记录的原始见证都未被改写
    assert (
        json.loads(db.get(PlanRow, "sem_bad_earliest").witnesses)[0] == bad[0]
    )
    assert (
        json.loads(db.get(PlanRow, "sem_edge").witnesses)[0] == edge_bad[0]
    )

    # 合法见证（含到达后等待至下一窗）仍可读、可重放
    _seed_plan(db, "sem_good2", "sem_cal", definition, 15, good)
    assert client.get("/plans/sem_good2").json()["witnesses"] == good
    assert (
        client.post("/plans/sem_good2/replay/sem_cal").json()["status"]
        == "STILL_VALID"
    )


def test_witness_semantic_anomaly_is_read_only(client, db):
    """语义异常的重复查询不新增/不改写任何行。"""
    definition, _ = _seed_three_gate_world(db)
    bad = [
        {"gate_id": "G1", "arrival": 15, "entry": 15, "wait": 0},
        {"gate_id": "G2", "arrival": 21, "entry": 25, "wait": 4},
        {"gate_id": "G3", "arrival": 35, "entry": 35, "wait": 0},
    ]
    _seed_plan(db, "sem_ro", "sem_cal", definition, 15, bad)

    def snapshot():
        return sorted(
            (p.id, p.calendar_id, p.payload, p.departure, p.witnesses)
            for p in db.query(PlanRow).all()
        )

    before = snapshot()
    for _ in range(3):
        client.get("/plans/sem_ro")
        client.post("/plans/sem_ro/replay/sem_cal")
    assert snapshot() == before


def test_publish_semantics_unchanged(client):
    # 新发布日历仍走原有 422 校验，且成功后可正常读回
    resp = client.post(
        "/calendars",
        json={"gates": [{"gate_id": "G1", "windows": [[0, 10], [9, 20]]}]},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    # FastAPI 请求体校验错误不带 DATA_ANOMALY 标记
    assert not (isinstance(detail, dict) and detail.get("error") == "DATA_ANOMALY")
