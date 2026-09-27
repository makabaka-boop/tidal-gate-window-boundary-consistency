"""潮汐闸门调度核心算法。

确定性模型（最早可入原则）：
* 首闸到达时刻 = 出发时刻；
* 第 i 闸到达时刻 = 第 i-1 闸入闸时刻 + 相邻航行时长 legs[i-1]；
* 到达时若落在某窗口 [start, end) 内则立即入闸，否则等待下一个窗口开始；
* 恰在 end 到达视为闸门已关闭；
* 等待时长 = 入闸 - 到达，必须不超过该闸最大等待时长。

可行出发时刻集合通过从末闸向前传播"可行入闸时刻区间集"得到，
每个非平凡闸门只做一次线性双指针扫描，连续的全程开放闸门（恒等映射）
合并为一次坐标平移；总复杂度 O(总窗口数 + 总区间传播量 + 闸数)，
末闸数万稀疏窗口、前面近二百个全程闸也可在几十毫秒内完成。
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

# 时间域为 [0, 10^12]，传播时用一个足够大的右端点作为末闸哨兵。
INF = 10**30
DOMAIN_MAX = 10**12


@dataclass(frozen=True)
class GateFailure:
    """前向模拟在某闸失败。"""

    gate_index: int
    arrival: int
    reason: str  # NO_OPEN_WINDOW | WAIT_EXCEEDED


@dataclass(frozen=True)
class Trace:
    """前向模拟成功后的逐闸见证数据。"""

    arrivals: list[int]
    entries: list[int]


def earliest_entry(arrival: int, starts: list[int], ends: list[int]) -> int | None:
    """按最早可入原则返回入闸时刻；无未来窗口时返回 None。

    窗口按 start 升序、互不重叠。恰在 end 到达判为关闭（左闭右开）。
    """
    j = bisect_right(starts, arrival) - 1
    if j >= 0 and arrival < ends[j]:
        return arrival  # 已在窗口内，立即入闸
    j += 1  # 等待下一个窗口开始
    if j < len(starts):
        return starts[j]
    return None


def _is_identity_gate(starts: list[int], ends: list[int]) -> bool:
    """窗口恰好无缝铺满整个时间域：任何到达都能立即入闸，本闸不改变时刻。

    这类"覆盖全程的一段（或相接多段）窗口"在逆向传播时只是对区间集做一次
    平移，多个连续的此类闸门可合并为一次，避免对大区间集逐闸重建。
    """
    return (
        bool(starts)
        and starts[0] == 0
        and ends[-1] >= DOMAIN_MAX
        and all(starts[j] == ends[j - 1] for j in range(1, len(starts)))
    )


def _preimage(
    starts: list[int],
    ends: list[int],
    max_wait: int,
    dlo: list[int],
    dhi: list[int],
    offset: int,
) -> tuple[list[int], list[int]]:
    """求本闸"可行到达区间"，输出为并行的 lo/hi 列表（已合并、升序）。

    下游入闸要求集合 downstream（左闭右开、升序、互不重叠）用 (dlo,dhi)
    表示，其坐标相对本闸整体偏移 ``offset``：本闸时刻 ``x`` 的下游时刻为
    ``x + offset``。这样直接在平移后的坐标里比较，省去对整份区间集的
    逐元素平移。本闸可行到达分两类片段：

    1. 等待片段 [max(prev_end, s-wait), s)：入闸恒为窗口起点 s，
       仅当 s+offset ∈ downstream 时整段可行（首窗之前 prev_end 视为 -∞）；
    2. 窗口内部 [s, e)：入闸 == 到达，可行部分为 downstream 平移后之交。

    两个指针各只前进一次，输出片段数不超过窗口数与 downstream 区间数之和；
    片段首尾相接或重叠时就地合并。
    """
    olo: list[int] = []
    ohi: list[int] = []
    size = len(dlo)

    m = 0  # 等待片段常量目标 membership 扫描指针
    k = 0  # 窗口内部交集扫描指针
    last_hi: int | None = None

    for j in range(len(starts)):
        s, e = starts[j], ends[j]
        so = s + offset  # 若在 s 入闸，对下游的时刻坐标

        # 等待进入窗口 j 的片段
        gap_lo = s - max_wait if j == 0 else max(ends[j - 1], s - max_wait)
        if gap_lo < s:
            while m < size and dhi[m] <= so:
                m += 1
            # dhi[m] > so 已由推进保证
            if m < size and dlo[m] <= so:
                if last_hi is not None and gap_lo <= last_hi:
                    if s > last_hi:
                        ohi[-1] = s
                        last_hi = s
                else:
                    olo.append(gap_lo)
                    ohi.append(s)
                    last_hi = s

        # 窗口内部：入闸 == 到达，直接与 downstream 求交（换算回本闸坐标）
        while k < size and dhi[k] <= so:
            k += 1
        eo = e + offset
        q = k
        while q < size and dlo[q] < eo:
            a = dlo[q] - offset
            b = dhi[q] - offset
            lo = s if s > a else a
            hi = e if e < b else b
            if lo < hi:
                if last_hi is not None and lo <= last_hi:  # 相接/重叠 -> 合并
                    if hi > last_hi:
                        ohi[-1] = hi
                        last_hi = hi
                else:
                    olo.append(lo)
                    ohi.append(hi)
                    last_hi = hi
            q += 1

    return olo, ohi


def feasible_departures(
    legs: list[int],
    waits: list[int],
    gate_windows: list[tuple[list[int], list[int]]],
    search: tuple[int, int],
) -> list[list[int]]:
    """返回出发搜索区间内全部可行出发时刻的合并区间（左闭右开）。"""
    n = len(waits)
    # 末闸之后：任何入闸时刻都被接受（哨兵区间）。区间集始终用并行的
    # lo/hi 两个列表表示，避免逐区间分配嵌套小列表。
    lo: list[int] = [0]
    hi: list[int] = [INF]
    # 惰性坐标：存储值 = 真实"本闸入闸时刻" + acc。连续若干全程开放闸只
    # 贡献一次坐标平移，把它们累加进 acc，直到下一个非平凡闸门在
    # _preimage 里以 offset=acc 一次性换算，避免逐闸重建整份区间集。
    acc = 0

    for i in range(n - 1, -1, -1):
        starts, ends = gate_windows[i]
        if not starts:
            return []  # 该闸永不开放
        if _is_identity_gate(starts, ends):
            # 到达即可入闸：preimage 为恒等映射，仅把入闸域平移到上一闸。
            if i > 0:
                acc += legs[i - 1]
            continue
        lo, hi = _preimage(starts, ends, waits[i], lo, hi, acc)
        if not lo:
            return []
        # 输出现在是本闸真实到达域；上一闸入闸 = 到达 - legs[i-1]，
        # 保持存储不动、把平移累加进 acc。
        acc = legs[i - 1] if i > 0 else 0

    # 存储值 = 真实出发时刻 + acc，换算搜索边界后裁剪、合并。
    slo, shi = search
    slo += acc
    shi += acc
    result: list[list[int]] = []
    last_hi: int | None = None
    for stored_lo, stored_hi in zip(lo, hi):
        a = max(stored_lo, slo) - acc
        b = min(stored_hi, shi) - acc
        if a < b:
            if last_hi is not None and a <= last_hi:
                if b > last_hi:
                    result[-1][1] = b
                    last_hi = b
            else:
                result.append([a, b])
                last_hi = b
    return result


def forward_trace(
    departure: int,
    legs: list[int],
    waits: list[int],
    gate_windows: list[tuple[list[int], list[int]]],
) -> Trace | GateFailure:
    """按最早可入原则逐闸模拟；返回见证 Trace 或首个失败 GateFailure。"""
    arrivals: list[int] = []
    entries: list[int] = []
    arrival = departure

    for i in range(len(waits)):
        starts, ends = gate_windows[i]
        entry = earliest_entry(arrival, starts, ends) if starts else None
        if entry is None:
            return GateFailure(i, arrival, "NO_OPEN_WINDOW")
        if entry - arrival > waits[i]:
            return GateFailure(i, arrival, "WAIT_EXCEEDED")
        arrivals.append(arrival)
        entries.append(entry)
        if i < len(waits) - 1:
            arrival = entry + legs[i]

    return Trace(arrivals, entries)
