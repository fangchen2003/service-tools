"""动态额度：V4.5 每日上限、保底、V5 每日额度都由算法管理，新老成员一视同仁。

━━ 为什么要动态 ━━
固定额度有两个问题：人少时额度用不完（V5 额度涨到 100% 后不再累积，多出来的每天白白浪费），
人多时又可能把上游账号用得太猛（fccc 的号就是日均 1000+ 张、全天不停被限制的）。
所以额度跟着「账号能承受多少」和「实际有多少人在用」走，每 10 分钟重算一次，每天再根据前一天的结果微调参数。

━━ 三个量 ━━
  A  每人每天 V4.5 上限（daily_images）。初始 150。
  B  保底（guard_base_daily_images）。2026-10-13 起每天按前一天的实际需求做最大最小公平分配（water_fill）：
     账号日上限的 60% 按人平分，用得少的人用多少算多少，剩下的平分给用得多的人。
     超过保底后只在全站空闲、且按时段放出的份额还剩得多时放行（见 guard.site_idle / paced_headroom）。
  D5 每人每天 V5 额度（daily_v5）；G 全站每天 V5 上限（global_daily_v5）。

━━ V5：按官方恢复速度分配（数据来源：NovelAI 官方 Usage Limit 说明 + 账号接口实测）━━
  · 订阅的第一个月每天恢复约 11%（约 190 张），续订后约 14%/天；空额度补满需要 7～9 天。
  · 官方：100% ≈ 1730 张、每 1% ≈ 17 张，但这是按「23 步、约 100 万像素」估的；额度按每张图的 Anlas 成本扣，
    成本和像素、步数成正比（docs.novelai.net/en/faq #47/#48，journal「Understanding the Opus Usage Limit」）。
    本站成员基本都用 28 步，所以按 17.3 × 23/28 ≈ 14.2 张 / 1% 算，宁可保守一点。
  · 我们直接用接口返回的「再恢复 1% 需要多少秒」算出实际 %/天（拿不到时按 11%/天保守估计）。
    每日可分配 = 恢复 %/天 × 14.2 × 系数 k
  · k 看账号当前剩余：剩得多就多发，把本来会浪费的额度用掉；剩得少就收紧，给账号回血。
        剩余 ≥ 90% → 1.3   70–90% → 1.1   40–70% → 0.9   20–40% → 0.6   < 20% → 0.3
    目标是让剩余长期稳定在 60%～80%：既不浪费（顶到 100% 就不再涨），又留出应对高峰的余量。
  · 每人 D5 = G ÷ 最近 3 天用过 V5 的人数（至少按 10 人算，给新来的人留位置），限制在 3～30 张；每天只定一次。
    全站上限 G 兜底：哪怕每个人都用满，也不会超过当天可分配量。

━━ V4.5：上限 A 与保底 B 每天自动微调（「越用越有效」的部分）━━
  V4.5 的免费规格不消耗额度，真正的限制是「账号每天/每小时出图总量」（guard 里的 1000 张/天、每小时上限 150，AIMD 在 100～160 间自动调）。
  每天 0 点后第一次运行时看前一天：
    · 拥挤（全站用量 ≥ 账号日上限的 85%，或有 ≥ 3 个不同小时出现每小时上限拦截；不计测试号 / 站长号）：A −25（不低于 B），B −10（不低于 50）
      —— 人均少给一点，让更多人能用上，也减少高峰排队。
    · 有余量（用量 < 60%）且有人顶到了上限 A：A +25（不超过 300）
      —— 账号还有空，顶格的人说明需求没满足，放宽。
    · 有余量且有人因为「超过保底、全站不空闲」被拦：B +10（不超过 A）
      —— 保底太低导致常用成员被卡，抬高保底。
    · 其余情况不变。每次调整都写进「操作日志」并保留 30 天历史，站长 / 运维定期审核后可以改初始值或步长。

━━ 范围 ━━
  只管理「由算法管理」的成员 Key（quota_auto = 1，默认）。站长在后台手动改过额度或模型的 Key 变成 -1（手动），
  算法不再覆盖；测试 Key 和站长 Key 不参与。新领取的 Key 直接使用当前算法给出的额度（领 Key 默认值同步更新）。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from .action_log import log_action
from . import quota_policies
from .params import P

V5_IMAGES_PER_PERCENT = 14.2      # 官方 17.3 张/1% 按 23 步估算；成员多用 28 步，按步数折算 17.3×23/28
V5_FALLBACK_RATE = 5.0            # 拿不到读数时用的恢复速度（%/天）。原来的 11 来自一次错误的单次读数（2026-10-10）
# 恢复速度的防护（2026-10-10：单次读数算出每天 11%，实际约 5%，V5 分多了）：
# 1) 合理范围：超出就当读数有问题；2) 和「每天余量变化 + 当天用掉的量」反推的实测值核对；
# 3) 还没核对过（没有实测值）时，不超过保守值。
V5_RATE_RANGE = (1.0, 15.0)
V5_SAFE_RATE = 5.0
V5_RATE_MISMATCH = 0.5            # 接口值和实测值相差超过 50% 算对不上
DEFAULTS = {
    "quota_auto_enabled": 1,
    "quota_target_avg": 150,      # A 初始值
    "quota_base": 100,            # B 初始值
    "quota_ceiling_max": 300,
    "quota_base_min": 50,
    "quota_base_min_fair": 10,    # 按需求平分的保底最低值（人再多也至少保 10 张）
    "quota_step": 25,             # A 每次调整幅度
    "quota_base_step": 10,        # B 每次调整幅度
    "quota_v5_min": 3,
    "quota_v5_max": 30,
}
STATE_KEY = "quota_algo_last"
SHADOW_STATE_KEY = "quota_shadow_state"       # 影子模式：各算法 × 号池规模上一天的结果
SHADOW_HISTORY_KEY = "quota_shadow_history"   # 影子模式：最近 30 天
HISTORY_KEY = "quota_algo_history"
DAY_KEY = "quota_algo_day"
NOTICE_KEY = "algo_notice"
V5_DAY_KEY = "quota_v5_day_plan"   # 当天的 V5 分配（每天只定一次）        # 首页一行提醒        # 最近一次做「每日微调」的日期，保证一天只调一次


# ---- V5 空闲借用（公益版，2026-10-10 站长确认）----
# 额度不归任何人，用不完的不会自动流向用得最多的人。只有账号余量快满（再不用就白白浪费恢复量）时，
# 当天顶到个人上限的人可以多借一点；近 7 天用得最多的三分之一只能借一半——不奖励刷量、开小号。
BORROW_POOL_PCT = 95
BORROW_RATIO = 0.5
BORROW_GLOBAL_RATIO = 0.5          # 借用期间全站上限也放宽这么多
BORROW: dict[str, Any] = {"open": False}      # 每 10 分钟由 run() 更新；出图检查直接读


def borrow_plan(pct: Optional[float], each: int, usage_7d: dict[int, int]) -> dict[str, Any]:
    """纯函数：余量 ≥ 95% 才开放；每人可借 each × 50%，近 7 天用得最多的三分之一只借一半。"""
    if pct is None or pct < BORROW_POOL_PCT or each <= 0:
        return {"open": False, "why": f"账号余量 {pct if pct is not None else '未知'}%，低于 {BORROW_POOL_PCT}% 不借用"}
    bonus = max(1, int(each * BORROW_RATIO + 0.5))
    users = sorted((n, k) for k, n in usage_7d.items() if n > 0)
    heavy = [k for _, k in users[len(users) - len(users) // 3:]] if len(users) >= 3 else []
    return {"open": True, "bonus": bonus, "heavy_bonus": max(1, bonus // 2), "heavy": heavy,
            "why": f"账号余量 {pct:.0f}% 快满：顶到上限的人可以多借 {bonus} 张（近 7 天用得最多的 {len(heavy)} 人借 {max(1, bonus // 2)} 张）"}


def borrow_for(key_id: int) -> int:
    if not BORROW.get("open"):
        return 0
    return BORROW["heavy_bonus"] if key_id in set(BORROW.get("heavy", [])) else BORROW["bonus"]


def choose_rate(api_rate: Optional[float], measured: Optional[float]) -> tuple[float, dict[str, Any]]:
    """决定用哪个恢复速度（纯函数）。返回 (速度, 说明)；说明会显示在后台，并给观测模块做自检。"""
    lo, hi = V5_RATE_RANGE
    notes = []
    if api_rate is not None and not lo <= api_rate <= hi:
        notes.append(f"接口读数 {api_rate:.1f}%/天 超出合理范围 {lo:g}～{hi:g}，不用")
        api_rate = None
    if measured is not None and not lo <= measured <= hi:
        notes.append(f"实测 {measured:.1f}%/天 超出合理范围，不用")
        measured = None
    mismatch = (api_rate is not None and measured is not None
                and abs(api_rate - measured) > V5_RATE_MISMATCH * measured)
    if measured is not None:
        rate = min(measured, api_rate) if api_rate is not None else measured
        status = "对不上" if mismatch else "已核对"
        if mismatch:
            notes.append(f"接口 {api_rate:.1f} 和实测 {measured:.1f} 相差超过一半，取较小的")
    else:
        rate = min(api_rate, V5_SAFE_RATE) if api_rate is not None else V5_SAFE_RATE
        status = "未核对"
        notes.append(f"还没有实测值核对，不超过保守值 {V5_SAFE_RATE:g}%/天")
    rate = max(lo, min(hi, rate))
    return rate, {"api": api_rate, "measured": measured, "used": round(rate, 1), "status": status, "notes": notes}


def v5_factor(percent: Optional[float]) -> float:
    """账号 V5 剩余越多，发得越大方；剩得越少，越保守。未知时按 0.9。"""
    if percent is None:
        return 0.9
    for floor, k in P("allocation.v5_k_table", [[90, 1.3], [70, 1.1], [40, 0.9], [20, 0.6]]):
        if percent >= floor:
            return float(k)
    return P("allocation.v5_k_floor", 0.3)


def v5_plan(percent: Optional[float], rate: Optional[float], active: int, *,
            lo: int = DEFAULTS["quota_v5_min"], hi: int = DEFAULTS["quota_v5_max"]) -> dict[str, Any]:
    """返回全站 V5 上限 G 与每人 D5。rate = 实测恢复 %/天。"""
    rate = rate if rate and rate > 0 else V5_FALLBACK_RATE
    k = v5_factor(percent)
    pool = int(rate * V5_IMAGES_PER_PERCENT * k)
    people = max(P("allocation.min_people", 10), active)
    each = max(lo, min(hi, pool // people))
    return {"rate": round(rate, 1), "percent": percent, "k": k, "global": pool, "people": people, "each": each}


def daily_adjust(a: int, b: int, *, used: int, cap: int, hourly_blocks: int, ceiling_hits: int,
                 base_blocks: int, cfg: dict, paced: bool = False) -> tuple[int, int, list[str]]:
    """前一天的结果 → 新的 A、B 和调整理由（纯函数，方便测试和审核）。
    paced=True（每日上限按时段放行，2026-10-11 起）：拥挤由按时段放行 + 保底平分处理，不再每天下调上限 A；
    保底 B 由 fair_base 按需求平分，这里只保留「有余量且有人顶格 → 放宽 A」。"""
    util = used / cap if cap else 0.0
    reasons: list[str] = []
    if paced:
        a2 = a
        if util < P("allocation.slack_util", 0.6) and ceiling_hits > 0:
            a2 = min(cfg["quota_ceiling_max"], a + cfg["quota_step"])
        reasons.append(f"有余量（用量 {util:.0%}），{ceiling_hits} 人顶到上限：上限 {a}→{a2}" if a2 != a
                       else f"用量 {util:.0%}：按时段放行，上限不变")
        return a2, min(b, a2), reasons
    # 拥挤看两样：全天用量 ≥ 85%，或有 ≥ 3 个不同的小时出现过「每小时上限」拦截。
    # 只按小时数算：一次几分钟的扎堆（2026-10-09 23:04 一次 10 连拦）不算拥挤，那是每小时上限本身在起作用。
    if util >= P("allocation.congested_util", 0.85) or hourly_blocks >= P("allocation.congested_hours", 3):
        a2 = max(b, a - cfg["quota_step"])
        b2 = max(cfg["quota_base_min"], b - cfg["quota_base_step"])
        reasons.append(f"拥挤（用量 {util:.0%}，{hourly_blocks} 个小时出现每小时上限拦截）：上限 {a}→{a2}，保底 {b}→{b2}")
        return a2, min(b2, a2), reasons
    a2, b2 = a, b
    if util < P("allocation.slack_util", 0.6) and ceiling_hits > 0:
        a2 = min(cfg["quota_ceiling_max"], a + cfg["quota_step"])
        if a2 != a:
            reasons.append(f"有余量（用量 {util:.0%}），{ceiling_hits} 人顶到上限：上限 {a}→{a2}")
    if util < P("allocation.slack_util", 0.6) and base_blocks > 0:
        b2 = min(a2, b + cfg["quota_base_step"])
        if b2 != b:
            reasons.append(f"有余量，{base_blocks} 次因「超过保底且全站不空闲」被拦：保底 {b}→{b2}")
    if not reasons:
        reasons.append(f"用量 {util:.0%}，无需调整")
    return a2, b2, reasons


FAIR_SHARE = 0.6        # 账号日上限里拿多少按人平分做保底；其余 40% 留给超出保底的人在空闲时段用


from .quota_policies import water_fill  # noqa: E402  影子模式和线上共用同一份实现


async def member_usage(db, day: str) -> list[int]:
    """前一天每个算法管理成员的 V4.5 用量（不含测试 / 站长 / 手动 Key）。"""
    rows = await db._db.execute_fetchall(
        "SELECT c.legacy_free_images FROM counters c JOIN api_keys k ON k.id=c.key_id "
        "WHERE c.day=? AND k.is_test=0 AND k.is_admin=0 AND k.quota_auto=1 AND c.legacy_free_images>0", (day,))
    return [int(r[0]) for r in rows]


async def fair_base(db, day: str, cap: int, *, lo: int, hi: int, share: float = FAIR_SHARE) -> Optional[int]:
    """按前一天每个成员的 V4.5 用量算保底（只看算法管理的成员，不含测试 / 站长 Key）。"""
    rows = await db._db.execute_fetchall(
        "SELECT c.legacy_free_images FROM counters c JOIN api_keys k ON k.id=c.key_id "
        "WHERE c.day=? AND k.is_test=0 AND k.is_admin=0 AND k.quota_auto=1 AND c.legacy_free_images>0", (day,))
    usage = [int(r[0]) for r in rows]
    if not usage:
        return None
    return max(lo, min(hi, water_fill(usage, cap * share)))


async def _cfg(db) -> dict[str, int]:
    out = {}
    for name, default in DEFAULTS.items():
        try:
            out[name] = int(float(await db.get_setting(name, default)))
        except (TypeError, ValueError):
            out[name] = default
    return out


async def day_coverage(db, day: str) -> float:
    """这一天有日志覆盖的小时数（从系统开始记录算起）。覆盖不足的「残缺天」不能拿来调参（统计审查 P6：
    10-09 只有 18:18 之后的数据，却被当成「全天用量 20%」去放宽额度和名额）。"""
    start = time.mktime(time.strptime(day, "%Y-%m-%d"))
    first = (await db._db.execute_fetchall("SELECT MIN(ts) FROM usage_log"))[0][0]
    if first is None:
        return 0.0
    return max(0.0, (start + 86400 - max(start, float(first))) / 3600)


async def _yesterday(db, day: str, members: list[int]) -> dict[str, int]:
    """前一天的统计：成员出图总数、每小时 / 每日总量拦截次数、顶到上限的人数、被保底规则拦下的次数。"""
    start = time.mktime(time.strptime(day, "%Y-%m-%d"))
    end = start + 86400
    rows = await db._db.execute_fetchall(
        "SELECT c.key_id, c.images, k.daily_images, c.legacy_free_images, k.quota_auto FROM counters c "
        "JOIN api_keys k ON k.id=c.key_id WHERE c.day=? AND k.is_test=0 AND k.is_admin=0", (day,))
    used = sum(r[1] for r in rows)                      # 账号总量：全部出图（和账号日上限比）
    # 账号日上限按算力折算（节约模式 14 步算半张）：按当天账号层面「折算值 / 实际张数」的比例换算，
    # 否则节约模式的日子会被误判成拥挤、把每个人的上限砍掉（2026-10-10）
    (imgs, units), = await db._db.execute_fetchall(
        "SELECT COALESCE(SUM(images),0), COALESCE(SUM(COALESCE(units, images)),0) FROM upstream_token_counters WHERE day=?", (day,))
    if imgs and units < imgs:
        used = int(round(used * units / imgs))
    # 顶格：V4.5 上限 A 管的是 V4.5 免费图（legacy_free_images），不能拿含 V5 的总张数去比；手动额度的 Key 不参与
    hits = sum(1 for r in rows if r[2] and r[4] == 1 and (r[3] or 0) >= r[2])

    # 只统计成员（不含测试号、站长号）被拦的记录
    member_rejects = ("FROM usage_log u JOIN api_keys k ON k.id=u.key_id WHERE u.ts>=? AND u.ts<? "
                      "AND u.status='rejected' AND k.is_test=0 AND k.is_admin=0 AND u.detail LIKE ?")

    async def count(pattern: str) -> int:
        r = await db._db.execute_fetchall("SELECT COUNT(*) " + member_rejects, (start, end, pattern))
        return int(r[0][0])

    async def hours(*patterns: str) -> int:
        """出现过拦截的不同小时数。"""
        seen: set[int] = set()
        for p in patterns:
            for (h,) in await db._db.execute_fetchall("SELECT DISTINCT CAST(u.ts/3600 AS INT) " + member_rejects,
                                                     (start, end, p)):
                seen.add(int(h))
        return len(seen)
    return {"used": used, "ceiling_hits": hits,
            # 只数「每小时上限」：按时段放行 / 每日上限是总量本来就这么多，不说明每人给多了（否则上限 A 每天 −25，几天后 A=保底）
            "hourly_blocks": await hours("%本小时出图量已达上限%"),
            "base_blocks": await count("%保底%其他人在排队%")}


async def _v5_used_equiv(db, day: str) -> float:
    """这一天全站用掉的 V5，折成 28 步的张数：按每张 V5 图自己的步数（日志 detail 里的「/NNstep」）折算。
    原来用全站所有图片（大部分是 28 步的 V4.5）的平均折算比例，节约模式那天 V5 多是 14 步，
    V5 用量被高估约 20%，实测恢复速度算成 15.5%（超出合理范围被丢弃），退回保守值 5%（2026-10-10 演练发现）。"""
    start = time.mktime(time.strptime(day, "%Y-%m-%d"))
    rows = await db._db.execute_fetchall(
        "SELECT detail, images FROM usage_log WHERE ts>=? AND ts<? AND status='ok' AND model LIKE '%diffusion-5%'",
        (start, start + 86400))
    total = 0.0
    for detail, images in rows:
        m = re.search(r"/(\d+)step", detail or "")
        steps = int(m.group(1)) if m else 28
        total += (images or 1) * min(max(steps, 1), 28) / 28
    return total


async def _members(db) -> list[int]:
    rows = await db._db.execute_fetchall(
        "SELECT id FROM api_keys WHERE enabled=1 AND is_admin=0 AND is_test=0 AND quota_auto=1")
    return [r[0] for r in rows]


async def _active_v5(db, days: int, now: float) -> int:
    """最近 days 天用过 V5 的成员数（不含测试 / 站长 Key）。"""
    since = [(datetime.fromtimestamp(now) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    r = await db._db.execute_fetchall(
        f"SELECT COUNT(DISTINCT c.key_id) FROM counters c JOIN api_keys k ON k.id=c.key_id "
        f"WHERE c.v5>0 AND k.is_test=0 AND k.is_admin=0 AND c.day IN ({','.join('?' * len(since))})", since)
    return int(r[0][0])


async def refresh_allowance(state) -> bool:
    """主动读一次账号 V5 剩余（只读订阅信息，不生成图片；缓存 5 分钟内不会重复读）。成功返回 True。
    以前只有成员出 V5 时才刷新：夜里没人用 V5，每小时快照记的都是旧缓存，算不出恢复速度（2026-10-10）。"""
    nai = getattr(state, "nai", None)
    allowance = getattr(nai, "allowance", None)
    if allowance is None or getattr(nai, "_client", None) is None:
        return False
    ok = False
    for t in getattr(nai, "pool", []):
        if getattr(t, "usable", False):
            try:
                await allowance.resolve(nai._client, nai.image_host, t.token_id, t.token)
                ok = True
            except Exception:
                pass
    return ok


async def quiet_recovery(db, now: float, window: float = 72 * 3600) -> Optional[dict[str, Any]]:
    """独立测量恢复速度：相邻两次「主动读」的快照之间一张 V5 都没出 → 剩余的上涨就是纯恢复。
    累计 ≥ 4 个安静小时才给结果（剩余只精确到 1%，时间太短看不出来）。"""
    snaps = await db._db.execute_fetchall(
        "SELECT ts, v5_percent FROM upstream_snapshots WHERE ts>? AND fresh=1 AND v5_percent IS NOT NULL ORDER BY ts",
        (now - window,))
    hours = gain = 0.0
    for (t0, p0), (t1, p1) in zip(snaps, snaps[1:]):
        if t1 - t0 > 3 * 3600:
            continue
        (n,), = await db._db.execute_fetchall(
            "SELECT COUNT(*) FROM usage_log WHERE ts>? AND ts<=? AND status='ok' AND model LIKE '%diffusion-5%'", (t0, t1))
        if n == 0:
            hours += (t1 - t0) / 3600
            gain += p1 - p0
    if hours < 4:
        return None
    return {"rate": round(gain / hours * 24, 2), "hours": round(hours, 1), "gain": gain}


async def _allowance(state) -> tuple[Optional[float], Optional[float]]:
    """账号 V5 剩余 % 与实测恢复 %/天（多个账号时取平均）。"""
    nai = getattr(state, "nai", None)
    allowance = getattr(nai, "allowance", None)
    if allowance is None:
        return None, None
    pool = getattr(nai, "pool", [])
    snap = await allowance.snapshot(pool)
    rows = [a for a in snap["accounts"] if a.get("percent") is not None]
    if not rows and getattr(nai, "_client", None) is not None:
        # 刚重启时还没人用过 V5，缓存里没有剩余比例：主动查一次（只是读订阅信息，不生成图片）
        for t in pool:
            if getattr(t, "usable", False):
                try:
                    await allowance.resolve(nai._client, nai.image_host, t.token_id, t.token)
                except Exception:
                    pass
        snap = await allowance.snapshot(pool)
        rows = [a for a in snap["accounts"] if a.get("percent") is not None]
    if not rows:
        return None, None
    pct = sum(a["percent"] for a in rows) / len(rows)
    rates = [a["recharge_per_day"] for a in rows if a.get("recharge_per_day")]
    return pct, (sum(rates) / len(rates) if rates else None)


async def run(state, now: Optional[float] = None) -> dict[str, Any]:
    """重算并应用。每 10 分钟调用一次；每天第一次调用时先做「每日微调」。"""
    db = state.db
    now = time.time() if now is None else now
    cfg = await _cfg(db)
    if not cfg["quota_auto_enabled"]:
        return {"enabled": False}
    guard = getattr(state, "guard", None)
    # 按账号总数算（不看此刻是否可用）：0 点恰好有账号在冷却时，不能把一整天的额度减半（Sonnet 审查 P2）
    accounts = max(1, len(getattr(getattr(state, "nai", None), "pool", []) or [])) if getattr(state, "nai", None) else 1
    cap = (guard.values["account_daily_cap"] if guard else 1000) * accounts
    a = int(float(await db.get_setting("quota_ceiling", cfg["quota_target_avg"])))
    b = int(float(await db.get_setting("quota_base_now", cfg["quota_base"])))
    members = await _members(db)

    # ---- 每日微调（一天一次，看前一天）----
    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    review = None
    if await db.get_setting(DAY_KEY, None) != today:
        yday = (datetime.fromtimestamp(now) - timedelta(days=1)).strftime("%Y-%m-%d")
        stats = await _yesterday(db, yday, members)
        covered = await day_coverage(db, yday)
        # 极简模式（2026-10-11 站长）：每人每天同一个数 N，没有保底 / 上限两层。
        # N = 把「账号日上限 − 全站 V5」按昨天的需求做最大最小公平分配：用得少的人用多少算多少，剩下的平分给用得多的人
        from . import site_flags
        v5_global = int(await site_flags.get(db, site_flags.GLOBAL_DAILY_V5, getattr(state, "settings", None)) or 0)
        budget = max(0, cap - v5_global)
        if covered < P("allocation.min_coverage_hours", 20):
            n, reasons = a, [f"昨天只有 {covered:.1f} 小时数据（不足 20 小时），每人额度不变"]
        else:
            usage = await member_usage(db, yday)
            new, why = quota_policies.simple({"a": a}, usage=usage, budget=budget, cfg=cfg)     # 线上 = 影子里的 LIVE
            n = new["a"]
            reasons = [f"{why}（可分 = 账号日上限 {cap} − 全站 V5 {v5_global}）"]
        a2 = b2 = n
        stats["coverage_hours"] = round(covered, 1)
        review = {"day": yday, **stats, "cap": cap, "from": [a, b], "to": [a2, b2], "reasons": reasons}
        history = json.loads(await db.get_setting(HISTORY_KEY, "[]") or "[]")[-29:] + [review]
        await db.set_settings_bulk({"quota_ceiling": a2, "quota_base_now": b2, DAY_KEY: today,
                                    HISTORY_KEY: json.dumps(history, ensure_ascii=False)})
        if (a2, b2) != (a, b):
            await log_action(db, "系统", "动态额度调整", f"上限 {a}→{a2} · 保底 {b}→{b2}", "；".join(reasons))
        a, b = a2, b2

    # ---- V5 ----
    # 每人 V5 额度一天只定一次（每日重置时），当天不再随人数变化（统计审查：10-10 一天内 15→13→11→9→8，
    # 先用的人用到 11 张后额度被降到 9 而被拦）。分母按最近 3 天真正用过 V5 的人数（原来按出过任何图的人，
    # 23 人里只有 10 人用 V5，额度长期浪费在 97%）。只有账号剩余跌破 40% 才在当天收紧（安全优先）。
    pct, rate = await _allowance(state)
    hist = json.loads(await db.get_setting(HISTORY_KEY, "[]") or "[]")
    if review is not None and pct is not None:
        # 每天记一笔账号 V5 剩余和接口给的恢复速度，并用「余量变化 + 前一天用掉的量」反推实测恢复速度
        review.update(v5_pct=round(pct, 1), v5_rate=round(rate, 1) if rate else None)
        prev = next((h for h in reversed(hist[:-1]) if h.get("v5_pct") is not None), None) \
            if hist and hist[-1].get("day") == review["day"] else None
        if prev is not None:
            used = await _v5_used_equiv(db, review["day"])
            review["v5_used_pct"] = round(used / V5_IMAGES_PER_PERCENT, 2)
            review["v5_rate_measured"] = round(pct - prev["v5_pct"] + review["v5_used_pct"], 2)
        if hist and hist[-1].get("day") == review["day"]:
            hist[-1] = review
            await db.set_setting(HISTORY_KEY, json.dumps(hist, ensure_ascii=False))
    measured = next((h["v5_rate_measured"] for h in reversed(hist) if h.get("v5_rate_measured") is not None), None)
    quiet = await quiet_recovery(db, now)
    if quiet is not None:              # 安静时段直接量到的恢复速度最可靠：优先用它
        measured = quiet["rate"]
    rate, rate_info = choose_rate(rate, measured)
    if quiet is not None:
        rate_info["quiet"] = quiet
    stored = json.loads(await db.get_setting(V5_DAY_KEY, "{}") or "{}")
    fresh = v5_plan(pct, rate, await _active_v5(db, 3, now), lo=cfg["quota_v5_min"], hi=cfg["quota_v5_max"])
    fresh["rate_info"] = rate_info
    if stored.get("day") == today and stored.get("plan"):
        v5 = stored["plan"]
        if pct is not None and pct < P("allocation.v5_tighten_below", 40) and fresh["each"] < v5["each"]:
            v5 = fresh
            await db.set_setting(V5_DAY_KEY, json.dumps({"day": today, "plan": v5}, ensure_ascii=False))
    else:
        v5 = fresh
        await db.set_setting(V5_DAY_KEY, json.dumps({"day": today, "plan": v5}, ensure_ascii=False))

    # ---- 节约模式：免费档统一 14 步，每张图只扣约一半额度 → 同样的额度能出约 2 倍的图 ----
    # 当天的基础方案（按 28 步计价）不变，只在下发时放大；关掉节约模式后下一次重算自动回到基础值。
    # 账号剩余跌破收紧线时不放大（安全优先）。
    v5 = dict(v5)
    # 当天的方案（每人额度）一天只定一次，但显示用的剩余要用实时值：原来一直显示早上定方案时的 97%，
    # 实际下午已经是 92%（2026-10-10）。决策（收紧、节约放大）本来就用的实时 pct。
    if pct is not None:
        v5["percent"] = pct
    v5["rate_check"] = rate_info          # 实时的核对结果（方案一天定一次，核对状态每 10 分钟更新）

    # ---- 空闲借用（账号快满时才开放）----
    since7 = time.mktime(time.strptime(today, "%Y-%m-%d")) - 6 * 86400
    usage = {int(k): int(n) for k, n in await db._db.execute_fetchall(
        "SELECT key_id, SUM(v5) FROM counters WHERE day>=? GROUP BY key_id",
        (datetime.fromtimestamp(since7).strftime("%Y-%m-%d"),)) if k is not None}
    BORROW.clear()
    BORROW.update({"open": False, "why": "极简模式：每人固定张数，不借用"})       # 2026-10-11 站长：去掉借用这一层
    v5["borrow"] = {k: BORROW[k] for k in ("open", "why") if k in BORROW}

    # ---- 应用：所有由算法管理的成员同一套额度 ----
    cur = await db._db.execute_fetchall(
        "SELECT COUNT(*) FROM api_keys WHERE quota_auto=1 AND enabled=1 AND is_admin=0 AND is_test=0 "
        "AND (daily_images<>? OR daily_v5<>? OR image_model_scope<>'all')", (a, v5["each"]))
    changed = int(cur[0][0])
    if changed:
        await db._db.execute(
            "UPDATE api_keys SET daily_images=?, daily_v5=?, image_model_scope='all' "
            "WHERE quota_auto=1 AND is_admin=0 AND is_test=0", (a, v5["each"]))
        await db._db.commit()
    # 手动定了 V5 基础值的 Key：实际 = max(基础值 × 节约倍数, 普通成员的值)——手动的人不会比大家少，节约模式一样放大。
    # 测试 Key 也照样应用：测试标记只影响统计，站长明确定的数字必须生效（2026-10-10 站长自己的 Key 是测试 Key，定了 50 没生效）
    # 手动 Key 填多少就是多少（2026-10-11）：不再按 v5_pinned 把 daily_v5 抬到「不低于普通成员」。旧数据启动时清掉 v5_pinned。
    settings = {"global_daily_v5": v5["global"], "register_daily_images": a, "register_daily_v5": v5["each"],
                "register_image_scope": "all"}
    if guard is not None and guard.values.get("base_daily_images") != b:
        await guard.save({"base_daily_images": b})
    await db.set_settings_bulk(settings)
    # 首页提醒（站长要求：算法调整只在网站首页提示，不发 Discord）
    note = f"今日额度：V4.5 免费图蓄水池 {a} 张（每小时自动恢复 4 张，全天上限 96 张）· V5 每人 {v5['each']} 张"
    if review and review["from"] != review["to"]:
        note = f"{today[5:].replace('-', '月')}日 算法调整：" + "；".join(review["reasons"]) + "。" + note
    await db.set_setting(NOTICE_KEY, note)
    result = {"enabled": True, "ceiling": a, "base": b, "v5": v5, "members": len(members), "cap": cap,
              "updated_at": now, "changed_keys": changed, "review": review}
    await db.set_setting(STATE_KEY, json.dumps(result, ensure_ascii=False))
    if changed:
        await log_action(db, "系统", "动态额度应用", f"{changed} 把 Key",
                         f"V4.5 上限 {a}（保底 {b}）· V5 每人 {v5['each']}，全站 {v5['global']}（剩余 {pct}%，恢复 {v5['rate']}%/天）")
    return result
