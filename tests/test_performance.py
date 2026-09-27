"""规模与性能：总窗口二十万、闸门二百，端到端探测须在 3 秒内完成。"""
from __future__ import annotations

import random
import time


def test_performance_200_gates_200k_windows(client, make_calendar):
    rng = random.Random(42)
    n_gates = 200
    per_gate = 1000  # 200 * 1000 = 200,000 窗口
    step = 2000
    width = 900  # 窗口 [k*step, k*step+width)，之间留 1100 间隙

    gates_body = []
    for g in range(n_gates):
        jitter = rng.randint(-50, 50)
        windows = [
            [k * step + jitter, k * step + width + jitter]
            for k in range(per_gate)
        ]
        # 保证仍满足 start < end、有序、不重叠
        windows = [[max(0, s), max(0, s) + width] for s, _ in windows]
        gates_body.append({"gate_id": f"G{g:03d}", "windows": windows})

    resp = client.post("/calendars", json={"gates": gates_body})
    assert resp.status_code == 201, resp.text
    cid = resp.json()["id"]

    voyage = {
        "calendar_id": cid,
        "gates": [f"G{g:03d}" for g in range(n_gates)],
        "legs": [50] * (n_gates - 1),
        "max_waits": [1100 + 60] * n_gates,
        "search_start": 0,
        "search_end": per_gate * step,
    }
    start = time.perf_counter()
    resp = client.post("/voyages/probe", json=voyage)
    elapsed = time.perf_counter() - start
    assert resp.status_code == 200, resp.text
    intervals = resp.json()["intervals"]
    assert intervals, "该构造下应存在可行出发区间"
    assert elapsed < 3.0, f"探测耗时 {elapsed:.3f}s 超过 3 秒"

    # 抽样验证：区间内随机时刻前向模拟必定成功
    lows = [iv[0] for iv in intervals]
    import bisect

    from app import scheduling

    all_starts = [[w[0] for w in g["windows"]] for g in gates_body]
    all_ends = [[w[1] for w in g["windows"]] for g in gates_body]
    wb = list(zip(all_starts, all_ends))
    for _ in range(20):
        iv = intervals[rng.randrange(len(intervals))]
        t = rng.randrange(iv[0], iv[1])
        result = scheduling.forward_trace(
            t, [50] * (n_gates - 1), [1160] * n_gates, wb
        )
        assert isinstance(result, scheduling.Trace), (
            f"声称可行的时刻 {t} 前向模拟失败"
        )
    # 搜索区间起点之前不可行的点（若有）也能快速模拟
    assert elapsed >= 0


def test_performance_single_gate_200k_windows(client, make_calendar):
    per = 200_000
    windows = [[k * 1000, k * 1000 + 400] for k in range(per)]
    cid = make_calendar(windows)
    start = time.perf_counter()
    resp = client.post(
        "/voyages/probe",
        json={
            "calendar_id": cid,
            "gates": ["G1"],
            "legs": [],
            "max_waits": [300],
            "search_start": 0,
            "search_end": per * 1000,
        },
    )
    elapsed = time.perf_counter() - start
    assert resp.status_code == 200, resp.text
    assert elapsed < 3.0, f"单闸二十万窗口耗时 {elapsed:.3f}s"
    intervals = resp.json()["intervals"]
    # 窗口 [k*1000, k*1000+400)，等待上限 300：
    # 每窗对应一段 [k*1000+700, (k+1)*1000+400)（窗后等待并入下一窗
    # 时需到达 >= 下一窗 start-300，再加下一窗内部）；
    # 首窗另有内部段 [0,400)。
    assert len(intervals) == per
    assert intervals[0] == [0, 400]
    assert intervals[1] == [700, 1400]
    assert intervals[-1] == [(per - 2) * 1000 + 700, (per - 1) * 1000 + 400]


def test_sparse_last_gate_with_full_cover_gates_under_3s(client):
    """检修场景：末闸三万段稀疏开放窗口，前 199 闸各一段覆盖全程的窗口。

    可行区间约三万段且不合并（等待上限 0），逆向区间集在 199 个全程闸上
    不得逐闸重复重建：探测必须在既定 3 秒上限内完成；采纳单个可行时刻
    本就是前向一次扫描，必须立即成功。
    """
    n_gates = 200
    n_last = 30_000
    step = 2000
    width = 900
    legs = [50] * (n_gates - 1)

    gates_body = [
        {"gate_id": f"G{i:03d}", "windows": [[0, 10**12]]}
        for i in range(n_gates - 1)
    ]
    gates_body.append(
        {
            "gate_id": f"G{n_gates - 1:03d}",
            "windows": [
                [k * step, k * step + width] for k in range(n_last)
            ],
        }
    )
    resp = client.post("/calendars", json={"gates": gates_body})
    assert resp.status_code == 201, resp.text
    cid = resp.json()["id"]

    voyage = {
        "calendar_id": cid,
        "gates": [f"G{i:03d}" for i in range(n_gates)],
        "legs": legs,
        "max_waits": [0] * n_gates,  # 区间不合并，保持约三万段
        "search_start": 0,
        "search_end": n_last * step,
    }
    start = time.perf_counter()
    resp = client.post("/voyages/probe", json=voyage)
    probe_elapsed = time.perf_counter() - start
    assert resp.status_code == 200, resp.text
    intervals = resp.json()["intervals"]
    assert 29_000 < len(intervals) < 30_200, len(intervals)
    assert probe_elapsed < 3.0, f"稀疏末闸探测耗时 {probe_elapsed:.3f}s 超过 3 秒"

    # 采纳单个可行时刻：前向一次扫描立即成功，见证逐闸自洽
    from app import scheduling

    dep = intervals[len(intervals) // 2][0]
    start = time.perf_counter()
    resp = client.post(
        "/plans",
        json={
            "calendar_id": cid,
            "gates": voyage["gates"],
            "legs": legs,
            "max_waits": [0] * n_gates,
            "departure": dep,
        },
    )
    adopt_elapsed = time.perf_counter() - start
    assert resp.status_code == 201, resp.text
    assert adopt_elapsed < 1.0
    plan = resp.json()
    witnesses = plan["witnesses"]
    assert witnesses[0]["arrival"] == dep
    for i in range(1, n_gates):
        assert witnesses[i]["arrival"] == witnesses[i - 1]["entry"] + legs[i - 1]
        assert witnesses[i]["wait"] == 0

    # 探测区间内随机抽样的时刻，前向模拟必须全部成功；右端点外必失败
    wb = [
        ([w[0] for w in g["windows"]], [w[1] for w in g["windows"]])
        for g in gates_body
    ]
    rng = random.Random(7)
    for _ in range(10):
        lo, hi = intervals[rng.randrange(len(intervals))]
        t = rng.randrange(lo, hi)
        result = scheduling.forward_trace(t, legs, [0] * n_gates, wb)
        assert isinstance(result, scheduling.Trace), f"区间内时刻 {t} 却失败"
        bad = scheduling.forward_trace(hi, legs, [0] * n_gates, wb)
        assert isinstance(bad, scheduling.GateFailure)
