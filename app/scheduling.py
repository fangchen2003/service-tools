"""③ 公平调度（DRR，亏损轮询）：决定账号空出槽位时，下一张图轮到谁。

━━ 问题 ━━
账号同时只出 1 张图，是全站唯一的瓶颈。现在是 FIFO（先到先得），谁手快、谁的客户端被拒后狂重试，
谁就更容易抢到槽位——哈哈哈大王 65 次重试就是这么把队列搅乱的。公平调度要让「一把 Key 占的份额」有上限，
而不是比谁点得快。

━━ 原理（极简）━━
每张图成本相同（≈1 张），所以 DRR 退化成「加权轮流」：在等待的 Key 里，挑最近服务得最少的那把先出。
参数只有一个——quantum（一把 Key 连续出几张后必须让队）。quantum=1 最公平，越大越偏吞吐。

━━ 现状：只有 observe，enforce 还没接上 ━━
真实派发顺序仍是 FIFO（main.py 只调 observe_pick / on_serve；drr_pick 只在 observe_pick 内部用来比较）。
把 module_scheduling_mode 设成 enforce 目前**没有任何效果**——要生效需在派发点用 drr_pick 重排（算法计划里的待办）。
observe：只记录「此刻 FIFO 服务的人，是不是 DRR 本该挑的人」，算出不公平比例和出图差距，不改真实顺序。
enforce（设计，未实现）：空槽时真的按 DRR 挑人——但只在「≥ engage_min_waiters 个不同 Key 同时在等」时才重排，否则天然等于 FIFO；
         任何一张图等满 starvation_seconds 秒强制插到最前（防饥饿）。所以它闲时零影响、挤时才公平，自动切换，无需手动。
         冻结期保持 observe，避免污染测量数据。
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any, Optional

from .params import P

MODE_SETTING = "module_scheduling_mode"     # observe（默认）/ off；enforce 尚未接入派发路径，设了也等于 observe
WINDOW = 600.0                              # 公平性统计窗口：最近 10 分钟
DEFAULT_QUANTUM = 1


def drr_pick(waiters: list[int], served: dict[int, int], quantum: int = 1,
             ages: Optional[dict[int, float]] = None, starvation: float = 60.0) -> Optional[int]:
    """从等待的 Key（waiters，按到达先后，可重复出现表示排了多张）里，挑 DRR 下一个该服务的 key_id。

    served：最近窗口里每把 Key 已服务的张数。挑「已服务 // quantum 最小」的 Key；并列时按到达顺序（waiters 里更靠前）。
    这就是等成本 DRR 的等价形式：服务少的先补，补到一个 quantum 再轮下一个。
    """
    if not waiters:
        return None
    # 防饥饿：任何一张图等太久（≥ starvation 秒）就强制优先，不管公平份额，绝不让人被无限期压后
    if ages:
        starving = [(ages.get(k, 0), i, k) for i, k in enumerate(waiters) if ages.get(k, 0) >= starvation]
        if starving:
            return max(starving)[2]
    q = max(1, quantum)
    best = None
    best_rank = None
    for order, kid in enumerate(waiters):
        rank = (served.get(kid, 0) // q, order)      # 服务轮数少者优先；并列看到达顺序
        if best_rank is None or rank < best_rank:
            best_rank, best = rank, kid
    return best


class FairScheduler:
    """记录每把 Key 最近的服务时刻，提供 DRR 选人与影子对比统计。只在内存里。"""

    def __init__(self) -> None:
        self._served: dict[int, deque] = {}       # key_id -> 最近服务时刻
        self._shadow_total = 0                     # 影子：有 ≥2 把 Key 在等时的决策次数
        self._shadow_unfair = 0                    #        其中 FIFO 挑的人 ≠ DRR 该挑的人
        self._shadow_since = time.time()

    def _served_counts(self, now: float, keys: Optional[set] = None) -> dict[int, int]:
        out: dict[int, int] = {}
        for kid, dq in self._served.items():
            while dq and dq[0] < now - WINDOW:
                dq.popleft()
            if dq and (keys is None or kid in keys):
                out[kid] = len(dq)
        return out

    def on_serve(self, key_id: int, now: Optional[float] = None) -> None:
        """一张图开始发往上游时调用：记一次服务。"""
        now = time.time() if now is None else now
        self._served.setdefault(key_id, deque()).append(now)

    def observe_pick(self, fifo_key: int, waiting_keys: list[int], now: Optional[float] = None) -> None:
        """影子对比：FIFO 此刻服务 fifo_key，而等待中的不同 Key 有 waiting_keys；
        记录 DRR 会不会挑别人（= 这次 FIFO 不公平）。不改真实顺序。"""
        now = time.time() if now is None else now
        distinct = list(dict.fromkeys(waiting_keys))
        if len(distinct) < P("scheduling.engage_min_waiters", 2):   # 少于这么多人同时在等，等于 FIFO，不算决策
            return
        self._shadow_total += 1
        served = self._served_counts(now, set(distinct))
        pick = drr_pick(distinct, served, P("scheduling.quantum", DEFAULT_QUANTUM))
        if pick != fifo_key:
            self._shadow_unfair += 1

    def report(self, now: Optional[float] = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        counts = self._served_counts(now)
        active = len(counts)
        served_vals = sorted(counts.values())
        top = served_vals[-1] if served_vals else 0
        total = sum(served_vals)
        skew = (top - served_vals[0]) if served_vals else 0        # 最爽 - 最饿
        monopoly = (top / total) if total else 0.0                 # 近 10 分钟最高一把 Key 的占比
        unfair_rate = (self._shadow_unfair / self._shadow_total) if self._shadow_total else 0.0
        return {"active_keys": active, "window_served": total, "skew": skew,
                "monopoly": round(monopoly, 3), "unfair_rate": round(unfair_rate, 3),
                "shadow_decisions": self._shadow_total,
                "since": self._shadow_since, "quantum": P("scheduling.quantum", DEFAULT_QUANTUM)}
