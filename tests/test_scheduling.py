"""核心算法测试：手工边界用例 + 随机小规模与逐秒穷举一致。"""
from __future__ import annotations

import random

from app import scheduling


def brute_force(legs, waits, windows_by_gate, search):
    """逐秒穷举可行出发时刻（仅用于小规模对照）。"""
    lo, hi = search
    feasible = []
    for dep in range(lo, hi):
        result = scheduling.forward_trace(
            dep, legs, waits, windows_by_gate
        )
        if isinstance(result, scheduling.Trace):
            feasible.append(dep)
    # 合并相邻整数点为 [lo, hi+1)
    merged: list[list[int]] = []
    for t in feasible:
        if merged and t == merged[-1][1]:
            merged[-1][1] = t + 1
        else:
            merged.append([t, t + 1])
    return merged


def ws(*pairs):
    return [list(p[0] for p in pairs), list(p[1] for p in pairs)]


def test_entry_exactly_at_end_is_closed():
    starts, ends = [10], [20]
    # 恰在 end 到达 -> 关闭，且后面无窗口
    assert scheduling.earliest_entry(20, starts, ends) is None
    # 提前一秒可以入
    assert scheduling.earliest_entry(19, starts, ends) == 19
    # 恰在 start 到达可以入
    assert scheduling.earliest_entry(10, starts, ends) == 10


def test_single_gate_wait_deadline_boundary():
    # 窗口 [10,20)，最大等待 3：到达 7 等待 3 可行；到达 6 等待 4 不可行。
    wb = ws((10, 20))
    iv = scheduling.feasible_departures([], [3], [wb], (0, 30))
    assert iv == [[7, 20]]  # [7,10) 等待 + [10,20) 窗口内，相接合并


def test_arrival_at_end_closed_propagates():
    # 到达 20 恰在 end，不可行；窗口内部只到 20（不含）。
    wb = ws((10, 20))
    iv = scheduling.feasible_departures([], [0], [wb], (0, 30))
    assert iv == [[10, 20]]
    # 到达 20 之后无窗口
    trace = scheduling.forward_trace(20, [], [0], [wb])
    assert isinstance(trace, scheduling.GateFailure)
    assert trace.reason == "NO_OPEN_WINDOW"


def test_two_gates_with_gap_and_wait():
    # G1 [10,20) wait 0；航行 5；G2 [25,30) wait 0。
    # 首闸入闸需恰好 [20,25)（+5 后落入 [25,30)）；但 G1 只能 [10,20)
    # 入闸，故入闸 20 来自首闸窗口不可达 -> 空。
    w1 = ws((10, 20))
    w2 = ws((25, 30))
    iv = scheduling.feasible_departures([5], [0, 0], [w1, w2], (0, 50))
    assert iv == []
    # 允许 G1 窗口内入闸 15..19 -> 到达 G2 20..24，G2 窗口 25，
    # 若 G2 max_wait >= 5 则可行（到达 20 等 5）。
    iv = scheduling.feasible_departures([5], [0, 5], [w1, w2], (0, 50))
    assert iv == [[15, 20]]


def test_touching_windows_share_boundary():
    # [10,20) 与 [20,30) 相接：到达 20 落入第二窗。
    wb = ws((10, 20), (20, 30))
    assert scheduling.earliest_entry(20, wb[0], wb[1]) == 20
    iv = scheduling.feasible_departures([], [0], [wb], (0, 40))
    assert iv == [[10, 30]]


def test_no_solution_returns_empty():
    wb = ws((100, 110))
    iv = scheduling.feasible_departures([], [0], [wb], (0, 50))
    assert iv == []


def test_search_clipping_half_open():
    wb = ws((10, 100))
    iv = scheduling.feasible_departures([], [1000], [wb], (50, 60))
    assert iv == [[50, 60]]


def test_multi_window_union_merges_across_windows():
    # [10,20), [30,40)，max_wait 10：
    # 第一窗：等待 [0,10) + 内部 [10,20) -> [0,20)；
    # 第二窗：等待 [20,30)（前窗 end=20 为界）+ 内部 [30,40) -> [20,40)；
    # 两段在 20 相接，合并为 [0,40)。
    wb = ws((10, 20), (30, 40))
    iv = scheduling.feasible_departures([], [10], [wb], (0, 50))
    assert iv == [[0, 40]]
    # 等待无限大时仍然只是各窗口与等待片段之并：到 40 为止
    merged = scheduling.feasible_departures([], [1000], [wb], (0, 50))
    assert merged == [[0, 40]]


def test_random_small_cases_match_brute_force():
    rng = random.Random(20260921)
    for case in range(300):
        n = rng.randint(1, 4)
        windows_by_gate = []
        for _ in range(n):
            starts, ends = [], []
            t = rng.randint(0, 5)
            count = rng.randint(1, 4)
            for _ in range(count):
                start = t + rng.randint(0, 6)
                end = start + rng.randint(1, 6)
                if starts and start < ends[-1]:
                    start = ends[-1]
                    end = start + rng.randint(1, 6)
                starts.append(start)
                ends.append(end)
                t = end + rng.randint(0, 3)
            windows_by_gate.append((starts, ends))
        legs = [rng.randint(0, 8) for _ in range(n - 1)]
        waits = [rng.randint(0, 8) for _ in range(n)]
        search = (0, 60)
        got = scheduling.feasible_departures(
            legs, waits, windows_by_gate, search
        )
        want = brute_force(legs, waits, windows_by_gate, search)
        assert got == want, (
            f"case {case}: n={n} legs={legs} waits={waits} "
            f"windows={windows_by_gate} got={got} want={want}"
        )


def test_feasible_departure_trace_succeeds_and_witness():
    w1 = ws((10, 20))
    w2 = ws((25, 35))
    iv = scheduling.feasible_departures([5], [3, 3], [w1, w2], (0, 50))
    # 至少应有可行区间；区间内逐点前向模拟都应成功，区间外失败
    assert iv
    lo, hi = iv[0]
    good = scheduling.forward_trace(lo, [5], [3, 3], [w1, w2])
    assert isinstance(good, scheduling.Trace)
    assert good.entries[0] >= good.arrivals[0]
    # 恰在右端点（区间外）必须失败
    bad = scheduling.forward_trace(hi, [5], [3, 3], [w1, w2])
    assert isinstance(bad, scheduling.GateFailure)
