"""额度算法的影子模式：历代「每人每天给多少张」的算法并排独立计算，只记录、不生效。

为什么要留着：以后号池加账号（2 个、3 个……），可以直接对比每一代算法在不同账号数下会怎么分，
再配合 app/lab 的日志回放做回测，而不是凭感觉挑。线上只执行 LIVE 这一个。

每个算法是一个纯函数：输入「昨天的统计 + 自己上一天的状态 + 可分的总量」，输出新的 (上限 A, 保底 B) 和理由。
各算法各自保存状态（A、B 是各自算出来的，不是线上值），所以几天以后会走出不同的轨迹。

注意：昨天的用量本身受线上规则限制（被拦下的需求看不到），账号越多的影子结果越可能低估需求，只能当参考；
要更准，用 lab 回放（按真实到达时间重放，能模拟被拦下的人重试）。
"""
from __future__ import annotations

from typing import Any, Callable

ACCOUNTS = (1, 2, 3)           # 影子模式同时算这几种号池规模
LIVE = "simple_v2_18"          # 线上实际执行的算法


def water_fill(usage: list[int], budget: float) -> int:
    """最大最小公平（water-filling）：找最小的 b，使 Σ min(用量ᵢ, b) ≥ budget。
    用得少的人用多少算多少，剩下的平分给用得多的人；需求不到 budget 时返回最大用量（人人都满足）。"""
    us = sorted(u for u in usage if u > 0)
    if not us:
        return 0
    left, n = float(budget), len(us)
    for i, u in enumerate(us):
        share = left / (n - i)
        if u >= share:
            return max(1, int(share))
        left -= u
    return us[-1]


def simple(prev: dict, *, usage: list[int], budget: int, cfg: dict, **_) -> tuple[dict, str]:
    """v2.18 极简模式（线上）：每人每天同一个数 N；需求顶满时按需求平分，有余量且有人用满时 +step，否则不变。"""
    a = int(prev.get("a") or cfg["quota_target_avg"])
    lo, hi = cfg["quota_base_min_fair"], cfg["quota_ceiling_max"]
    if usage and sum(usage) >= budget * 0.95:
        n = max(lo, min(hi, water_fill(usage, budget)))
        why = f"昨天总需求 {sum(usage)} 张已接近可分的 {budget} 张，按需求平分"
    elif usage and max(usage) >= a:
        n = min(hi, a + cfg["quota_step"])
        why = f"还有余量（昨天共 {sum(usage)} / {budget} 张），{sum(1 for u in usage if u >= a)} 人用满 {a} 张：放宽"
    else:
        n = max(lo, min(hi, a))
        why = f"还有余量（昨天共 {sum(usage)} / {budget} 张），没人用满：不变"
    return {"a": n, "b": n}, f"每人每天 {n} 张。{why}"


def fair60(prev: dict, *, usage: list[int], budget: int, stats: dict, cfg: dict, **_) -> tuple[dict, str]:
    """v2.17 公平分配：保底 B = 可分总量 60% 的最大最小公平；上限 A 只在有余量且有人顶格时 +step。"""
    a = int(prev.get("a") or cfg["quota_target_avg"])
    util = stats["used"] / budget if budget else 0.0
    if util < 0.6 and stats.get("ceiling_hits", 0) > 0:
        a = min(cfg["quota_ceiling_max"], a + cfg["quota_step"])
    b = max(cfg["quota_base_min_fair"], min(a, water_fill(usage, budget * 0.6))) if usage else int(prev.get("b") or a)
    return {"a": a, "b": b}, f"上限 {a} · 保底 {b}（60% 按需求平分，用量 {util:.0%}）"


def aimd(prev: dict, *, budget: int, stats: dict, cfg: dict, **_) -> tuple[dict, str]:
    """v2.16 每日微调（AIMD）：拥挤（用量 ≥85% 或 ≥3 个小时撞每小时上限）A−25、B−10；有余量且有人顶格 A+25、被保底拦 B+10。"""
    a = int(prev.get("a") or cfg["quota_target_avg"])
    b = int(prev.get("b") or cfg["quota_base"])
    util = stats["used"] / budget if budget else 0.0
    if util >= 0.85 or stats.get("hourly_blocks", 0) >= 3:
        a2 = max(b, a - cfg["quota_step"])
        b2 = max(cfg["quota_base_min"], b - cfg["quota_base_step"])
        return {"a": a2, "b": min(b2, a2)}, f"拥挤（用量 {util:.0%}）：上限 {a}→{a2}，保底 {b}→{min(b2, a2)}"
    a2, b2 = a, b
    if util < 0.6 and stats.get("ceiling_hits", 0) > 0:
        a2 = min(cfg["quota_ceiling_max"], a + cfg["quota_step"])
    if util < 0.6 and stats.get("base_blocks", 0) > 0:
        b2 = min(a2, b + cfg["quota_base_step"])
    return {"a": a2, "b": b2}, f"用量 {util:.0%}：上限 {a2} · 保底 {b2}"


POLICIES: dict[str, tuple[str, Callable[..., tuple[dict, str]]]] = {
    "simple_v2_18": ("极简：每人每天 N 张（v2.18，线上）", simple),
    "fair60_v2_17": ("保底 60% 平分 + 上限只升（v2.17）", fair60),
    "aimd_v2_16": ("每日微调 AIMD（v2.16）", aimd),
}


def run_all(state: dict, *, day: str, usage: list[int], stats: dict, cap_per_account: int, v5_global: int,
            cfg: dict) -> dict[str, Any]:
    """每个算法 × 每种号池规模各算一次。state = 上一天各自的结果（{policy: {accounts: {a, b}}}）。"""
    out: dict[str, Any] = {"day": day, "results": {}}
    for name, (_label, fn) in POLICIES.items():
        for n in ACCOUNTS:
            budget = max(0, (cap_per_account - v5_global) * n)      # 每个账号各有自己的 V5 额度：按账号数一起放大
            prev = (state.get(name) or {}).get(str(n)) or {}
            new, why = fn(prev, usage=usage, budget=budget, stats=stats, cfg=cfg)
            out["results"].setdefault(name, {})[str(n)] = {**new, "budget": budget, "why": why}
    return out
