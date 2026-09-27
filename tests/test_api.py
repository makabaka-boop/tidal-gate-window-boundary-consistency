"""API 端到端流程：日历发布、探测、采纳、重放、校验与状态码。"""
from __future__ import annotations


def _calendar_body(g1_windows, g2_windows=None):
    gates = [{"gate_id": "G1", "windows": g1_windows}]
    if g2_windows is not None:
        gates.append({"gate_id": "G2", "windows": g2_windows})
    return {"gates": gates}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_publish_and_read_calendar(client):
    body = _calendar_body([[10, 20], [30, 40]])
    resp = client.post("/calendars", json=body)
    assert resp.status_code == 201
    cid = resp.json()["id"]
    got = client.get(f"/calendars/{cid}").json()
    assert got["gates"] == [
        {"gate_id": "G1", "windows": [[10, 20], [30, 40]]}
    ]
    assert client.get("/calendars/nope").status_code == 404


def test_illegal_calendar_rejected_and_not_stored(client):
    # start >= end
    bad = _calendar_body([[20, 20]])
    assert client.post("/calendars", json=bad).status_code == 422
    # 窗口重叠
    bad = _calendar_body([[0, 10], [9, 20]])
    assert client.post("/calendars", json=bad).status_code == 422
    # 重复闸门 ID
    resp = client.post(
        "/calendars",
        json={
            "gates": [
                {"gate_id": "A", "windows": [[0, 1]]},
                {"gate_id": "A", "windows": [[0, 1]]},
            ]
        },
    )
    assert resp.status_code == 422
    # 越界整数
    bad = _calendar_body([[0, 10**12 + 1]])
    assert client.post("/calendars", json=bad).status_code == 422
    # 空闸门列表 / 空窗口
    assert client.post("/calendars", json={"gates": []}).status_code == 422
    assert (
        client.post(
            "/calendars", json=_calendar_body([])
        ).status_code
        == 422
    )
    # 非整数 / 布尔不接受（严格整数）
    assert (
        client.post(
            "/calendars", json=_calendar_body([[0, True]])
        ).status_code
        == 422
    )
    # 非法整版不落库：日历表应为空
    resp = client.get("/health")
    assert resp.status_code == 200


def test_non_ascii_gate_id_rejected(client):
    resp = client.post(
        "/calendars",
        json={"gates": [{"gate_id": "闸A", "windows": [[0, 1]]}]},
    )
    assert resp.status_code == 422


def test_probe_end_to_end(client, make_calendar):
    cid = make_calendar([[10, 20]])
    resp = client.post(
        "/voyages/probe",
        json={
            "calendar_id": cid,
            "gates": ["G1"],
            "legs": [],
            "max_waits": [3],
            "search_start": 0,
            "search_end": 30,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["intervals"] == [[7, 20]]


def test_probe_no_solution_empty_intervals(client, make_calendar):
    cid = make_calendar([[100, 110]])
    resp = client.post(
        "/voyages/probe",
        json={
            "calendar_id": cid,
            "gates": ["G1"],
            "legs": [],
            "max_waits": [0],
            "search_start": 0,
            "search_end": 50,
        },
    )
    assert resp.json()["intervals"] == []


def test_probe_unknown_gate_is_422(client, make_calendar):
    cid = make_calendar([[0, 10]])
    resp = client.post(
        "/voyages/probe",
        json={
            "calendar_id": cid,
            "gates": ["MISSING"],
            "legs": [],
            "max_waits": [0],
            "search_start": 0,
            "search_end": 5,
        },
    )
    assert resp.status_code == 422


def test_probe_bad_voyage_shape(client, make_calendar):
    cid = make_calendar([[0, 10]])
    base = {
        "calendar_id": cid,
        "gates": ["G1", "G1"],  # 重复
        "legs": [1],
        "max_waits": [0, 0],
        "search_start": 0,
        "search_end": 5,
    }
    assert client.post("/voyages/probe", json=base).status_code == 422
    base["gates"] = ["G1"]
    base["legs"] = [1]  # legs 应为 0 个
    assert client.post("/voyages/probe", json=base).status_code == 422
    base["legs"] = []
    base["search_start"] = 5
    base["search_end"] = 5  # start < end
    assert client.post("/voyages/probe", json=base).status_code == 422


def test_adopt_plan_and_get_witnesses(client, make_calendar):
    cid = make_calendar([[10, 20]], [[25, 35]])
    # G1 [10,20)，航行 5，G2 [25,35)，各允许等待 5
    # 出发 12：12 入 G1，17 到 G2，等 8 -> 超出 -> 409
    resp = client.post(
        "/plans",
        json={
            "calendar_id": cid,
            "gates": ["G1", "G2"],
            "legs": [5],
            "max_waits": [5, 5],
            "departure": 12,
        },
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["failed_gate_id"] == "G2"
    assert detail["arrival"] == 17
    assert detail["wait_deadline"] == 22

    # 出发 15：15 入 G1，20 到 G2，等 5 -> 恰好可行
    resp = client.post(
        "/plans",
        json={
            "calendar_id": cid,
            "gates": ["G1", "G2"],
            "legs": [5],
            "max_waits": [5, 5],
            "departure": 15,
        },
    )
    assert resp.status_code == 201, resp.text
    plan = resp.json()
    assert plan["witnesses"] == [
        {"gate_id": "G1", "arrival": 15, "entry": 15, "wait": 0},
        {"gate_id": "G2", "arrival": 20, "entry": 25, "wait": 5},
    ]
    # 可按 ID 取回
    got = client.get(f"/plans/{plan['id']}").json()
    assert got["departure"] == 15
    assert client.get("/plans/nope").status_code == 404


def test_replay_still_valid_and_invalid(client, make_calendar):
    cid1 = make_calendar([[10, 20]], [[25, 35]])
    resp = client.post(
        "/plans",
        json={
            "calendar_id": cid1,
            "gates": ["G1", "G2"],
            "legs": [5],
            "max_waits": [5, 5],
            "departure": 15,
        },
    )
    pid = resp.json()["id"]

    # 在同结构新日历上重放 -> STILL_VALID
    cid2 = make_calendar([[10, 20]], [[25, 35]])
    resp = client.post(f"/plans/{pid}/replay/{cid2}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "STILL_VALID"

    # 新日历中 G2 窗口右移：20 到达，窗口 26 才开，等待截止 25 -> INVALID
    cid3 = make_calendar([[10, 20]], [[26, 35]])
    resp = client.post(f"/plans/{pid}/replay/{cid3}")
    body = resp.json()
    assert body["status"] == "INVALID"
    assert body["failed_gate_id"] == "G2"
    assert body["failed_index"] == 1
    assert body["arrival"] == 20
    assert body["wait_deadline"] == 25

    # 恰在 end 到达：G2 变 [20,25)，20 到达可入 -> 有效
    cid4 = make_calendar([[10, 20]], [[20, 25]])
    assert (
        client.post(f"/plans/{pid}/replay/{cid4}").json()["status"]
        == "STILL_VALID"
    )
    # G2 变 [15,20)：20 到达恰在 end -> 关闭且无后续窗口 -> INVALID
    cid5 = make_calendar([[10, 20]], [[15, 20]])
    body = client.post(f"/plans/{pid}/replay/{cid5}").json()
    assert body["status"] == "INVALID"
    assert body["arrival"] == 20
    assert body["reason"] == "NO_OPEN_WINDOW"

    # 新日历缺闸门 -> INVALID（首闸还在，G2 缺失）
    cid6 = make_calendar([[10, 20]])
    body = client.post(f"/plans/{pid}/replay/{cid6}").json()
    assert body["status"] == "INVALID"
    assert body["failed_gate_id"] == "G2"

    # 原方案未被改写
    assert client.get(f"/plans/{pid}").json()["departure"] == 15
    # 不存在资源
    assert client.post(f"/plans/nope/replay/{cid2}").status_code == 404
    assert client.post(f"/plans/{pid}/replay/nope").status_code == 404


def test_immutable_calendar_replay_does_not_mutate(client, make_calendar):
    cid = make_calendar([[0, 100]])
    resp = client.post(
        "/plans",
        json={
            "calendar_id": cid,
            "gates": ["G1"],
            "legs": [],
            "max_waits": [0],
            "departure": 50,
        },
    )
    pid = resp.json()["id"]
    # 日历内容不可通过 API 修改（无写接口）；同 ID 重放仍有效
    assert (
        client.post(f"/plans/{pid}/replay/{cid}").json()["status"]
        == "STILL_VALID"
    )


def _adopt_three_gate_plan(client, make_calendar):
    # G1 15 入，leg 5 -> G2 到达20 等5 于25 入，leg 10 -> G3 到达35 入。
    cid = make_calendar([[10, 20]], [[25, 35]], [[35, 45]])
    resp = client.post(
        "/plans",
        json={
            "calendar_id": cid,
            "gates": ["G1", "G2", "G3"],
            "legs": [5, 10],
            "max_waits": [5, 5, 5],
            "departure": 15,
        },
    )
    assert resp.status_code == 201, resp.text
    return cid, resp.json()["id"]


def test_replay_front_failure_not_masked_by_later_missing_gate(
    client, make_calendar
):
    """前段已因窗口关闭/等待超限失效时，后段缺闸不得掩盖首个失效闸。"""
    _, pid = _adopt_three_gate_plan(client, make_calendar)

    # G1 30 才开：到达 15、等待截止 20 -> WAIT_EXCEEDED；同时 G3 缺失
    cid = make_calendar([[30, 40]], [[25, 35]])
    body = client.post(f"/plans/{pid}/replay/{cid}").json()
    assert body["status"] == "INVALID"
    assert body["failed_gate_id"] == "G1"
    assert body["failed_index"] == 0
    assert body["arrival"] == 15
    assert body["wait_deadline"] == 20
    assert body["reason"] == "WAIT_EXCEEDED"

    # G1 窗口 [0,10) 已关闭且无后续窗：到达 15 -> NO_OPEN_WINDOW；G3 缺失
    cid = make_calendar([[0, 10]], [[25, 35]])
    body = client.post(f"/plans/{pid}/replay/{cid}").json()
    assert body["failed_gate_id"] == "G1"
    assert body["failed_index"] == 0
    assert body["arrival"] == 15
    assert body["wait_deadline"] == 20
    assert body["reason"] == "NO_OPEN_WINDOW"

    # G1 可过、G2 窗口右移导致到达 20 超截止 25，且 G3 缺失 -> 报 G2
    cid = make_calendar([[10, 20]], [[26, 35]])
    body = client.post(f"/plans/{pid}/replay/{cid}").json()
    assert body["failed_gate_id"] == "G2"
    assert body["failed_index"] == 1
    assert body["arrival"] == 20
    assert body["wait_deadline"] == 25
    assert body["reason"] == "WAIT_EXCEEDED"

    # 前段全部可过、仅 G3 缺失 -> 仍按航程顺序报 G3 缺闸
    cid = make_calendar([[10, 20]], [[25, 35]])
    body = client.post(f"/plans/{pid}/replay/{cid}").json()
    assert body["failed_gate_id"] == "G3"
    assert body["failed_index"] == 2
    assert body["reason"] == "GATE_NOT_IN_CALENDAR"
    assert body["arrival"] is None
    assert body["wait_deadline"] is None


def test_replay_first_gate_missing_still_reported(client, make_calendar):
    """首闸就在新日历缺失：没有前段，直接报 GATE_NOT_IN_CALENDAR。"""
    _, pid = _adopt_three_gate_plan(client, make_calendar)
    cid = make_calendar([[25, 35]], [[35, 45]], gate_id_prefix="H")
    body = client.post(f"/plans/{pid}/replay/{cid}").json()
    assert body["status"] == "INVALID"
    assert body["failed_gate_id"] == "G1"
    assert body["failed_index"] == 0
    assert body["reason"] == "GATE_NOT_IN_CALENDAR"
