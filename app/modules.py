"""模块登记：容量 / 分配 / 公平调度 / Anlas / 自动驾驶 / 诚信 / 风险检查 / 领 Key / 观测。

每个模块写明回答的问题、关键参数的当前值和交叉校验。框架见 kernel.py；新增模块写一个 Module(...) 并在 build() 里 register。
"""
from __future__ import annotations

import json
import time

from . import site_flags
from .kernel import Check, Kernel, Module, Param
from .params import P


async def _q1(db, sql: str, *args):
    rows = await db._db.execute_fetchall(sql, args)
    return rows[0][0] if rows else None


async def _setting_json(db, key: str) -> dict:
    try:
        return json.loads(await db.get_setting(key, "{}") or "{}")
    except (TypeError, ValueError):
        return {}


# ---------------- ① 容量 ----------------
def capacity(state) -> Module:
    from . import quota_algo
    g = state.guard

    async def tick(k: Kernel):
        now = time.time()
        last = await _q1(state.db, "SELECT MAX(ts) FROM upstream_snapshots")
        if not last or now - float(last) >= 3600:          # 每小时记一次上游真实状态
            fresh = await quota_algo.refresh_allowance(state)    # 主动读一次，夜里没人用 V5 时也是真实值
            pct, rate = await quota_algo._allowance(state)
            anl = (await _setting_json(state.db, "anlas_pool_last")).get("anlas")
            await state.db._db.execute("INSERT INTO upstream_snapshots(ts, v5_percent, v5_rate, anlas, fresh) VALUES (?,?,?,?,?)",
                                       (now, pct, rate, anl, 1 if fresh else 0))
            await state.db._db.commit()
        changed = await g.adapt_daily()
        if changed:
            from .action_log import log_action
            await log_action(state.db, "系统", "自动调整：每小时上限", "", f"一天没有上游限流：{changed[0]} → {changed[1]}")
        return f"每小时上限 {g.values['account_hourly_cap']}" + (f"（{changed[0]} → {changed[1]}）" if changed else "")

    async def checks(k: Kernel):
        now = time.time()
        tokens = [t.token_id for t in getattr(state.nai, "pool", []) if getattr(t, "usable", True)]
        logged = int(await _q1(state.db, "SELECT COALESCE(SUM(images),0) FROM usage_log WHERE ts>? AND status='ok' "
                                         "AND kind LIKE 'image%'", now - 3600) or 0)
        if not tokens:
            return [Check("有可用的上游账号", False, "没有可用的上游账号，无法核对每小时计数")]
        mem = max(g.hour_count(t, now) for t in tokens)
        cap = g.hourly_cap(now) * len(tokens)
        # 独立来源：上游报告的 V5 剩余变化 ↔ 我们日志里数到的 V5 张数（同时校准「每 1% ≈ 14.2 张」）
        snaps = await state.db._db.execute_fetchall(
            "SELECT ts, v5_percent, v5_rate FROM upstream_snapshots WHERE ts>? AND v5_percent IS NOT NULL ORDER BY ts",
            (now - 86400,))
        v5_check = Check("上游 V5 消耗 ↔ 日志 V5 张数", True, "数据不足（需要 ≥ 6 小时、账号剩余 < 95%）")
        if len(snaps) >= 2 and snaps[-1][0] - snaps[0][0] >= 6 * 3600 and snaps[-1][1] < 95:
            hours = (snaps[-1][0] - snaps[0][0]) / 3600
            rate = snaps[-1][2] or quota_algo.V5_FALLBACK_RATE
            used_pct = snaps[0][1] - snaps[-1][1] + rate * hours / 24
            counted = int(await _q1(state.db, "SELECT COALESCE(SUM(images),0) FROM usage_log WHERE ts>? AND ts<=? "
                                              "AND status='ok' AND model LIKE '%diffusion-5%'", snaps[0][0], snaps[-1][0]) or 0)
            if used_pct > 0.5:
                per = counted / used_pct
                v5_check = Check("上游 V5 消耗 ↔ 日志 V5 张数", 7 <= per <= 28,
                                 f"{hours:.0f} 小时内上游用掉 {used_pct:.1f}%，日志 {counted} 张 → 每 1% ≈ {per:.1f} 张（参数 14.2）")
        return [
            v5_check,
            Check("内存每小时计数 ↔ 用量日志", mem >= logged - 2,
                  f"内存 {mem} 张，日志 {logged} 张" + ("" if mem >= logged - 2 else "：内存计数偏少，上限可能被绕过（重启清零？）")),
            Check("实际每小时出图 ≤ 上限", logged <= cap * 1.05 + 2, f"最近 60 分钟 {logged} 张，上限 {cap}"),
        ]

    return Module(
        name="capacity", title="容量", question="每个上游账号一小时、一天能出多少张",
        tick=tick, checks=checks,
        hard_note="关闭后不再自动调每小时上限；各项上限照常生效",
        params=[
            Param("每天", lambda: f"{g.values['account_daily_cap']}（上游 429 / 403 时自动退到 {g.values.get('daily_cap_fallback')}）"),
            Param("按时段放行", lambda: f"0 点先放 {g.values['daily_burst']} 张，之后每小时补 "
                  f"{round(g.values['account_daily_cap'] / 24)} 张" if g.values.get("daily_burst") else "关"),
            Param("每小时", lambda: g.values["account_hourly_cap"], "连续 24 小时没有 429 且顶到过上限时 +10"),
            Param("每 3 小时", lambda: g.values["account_3h_cap"] or "不限"),
            Param("出图间隔（秒）", lambda: state.settings.image_min_interval, "另加随机 0～5 秒"),
            Param("V5 每 1% ≈ 张", lambda: quota_algo.V5_IMAGES_PER_PERCENT),
        ])


# ---------------- ② 分配 ----------------
def allocation(state) -> Module:
    from . import quota_algo

    async def tick(k: Kernel):
        r = await quota_algo.run(state)
        v5 = (r or {}).get("v5") or {}
        return f"每人 V4.5 {r.get('ceiling')} 张 · V5 {v5.get('each')} 张（全站 V5 {v5.get('global')}）" if r else ""

    async def checks(k: Kernel):
        db = state.db
        last = await _setting_json(db, quota_algo.STATE_KEY)
        if not last or last.get("enabled") is False:
            return []
        v5, a = last.get("v5") or {}, last.get("ceiling")
        each = v5.get("each") or 0
        drift = int(await _q1(db, "SELECT COUNT(*) FROM api_keys WHERE quota_auto=1 AND is_admin=0 AND is_test=0 "
                                  "AND enabled=1 AND (daily_images<>? OR daily_v5<>?)", a, each) or 0)
        return [Check("算法管理的 Key 都已同步", drift == 0, f"{drift} 把 Key 的额度和算法结果不一致")]

    cfg = quota_algo.DEFAULTS
    async def current(key: str):
        return (await _setting_json(state.db, quota_algo.STATE_KEY)).get(key)

    return Module(
        name="allocation", title="分配", question="每人每天能出多少张（所有人一样）",
        tick=tick, checks=checks, hard_note="关闭后额度冻结在当前值",
        params=[
            Param("今天每人 V4.5", lambda: current("ceiling"), "0 点按昨天的用量重算"),
            Param("规则", lambda: "需求接近总量 → 按需求平分；有余量且有人用满 → +"
                  f"{cfg['quota_step']}；否则不变"),
            Param("范围", lambda: f"{cfg['quota_base_min_fair']}～{cfg['quota_ceiling_max']} 张"),
            Param("V5 每人范围", lambda: f"{cfg['quota_v5_min']}～{cfg['quota_v5_max']} 张", "按账号 V5 剩余和恢复速度分"),
        ])


# ---------------- Anlas 自动分配 ----------------
def anlas(state) -> Module:
    from . import anlas_pool

    async def tick(k: Kernel):
        r = await anlas_pool.rebalance(state, notify=k.extra.get("dm"))
        return f"每人 {r.get('per_member')} Anlas · {len(r.get('members') or [])} 人" if isinstance(r, dict) else ""

    async def checks(k: Kernel):
        last = await _setting_json(state.db, anlas_pool.STATE_KEY)
        per = last.get("per_member") or 0
        return [Check("分到的 Anlas 至少够一张续杯", per == 0 or per >= anlas_pool.V5_PAID_PRICE,
                      f"每人 {per}，一张 V5 约 {anlas_pool.V5_PAID_PRICE}")] if last else []

    d = anlas_pool.DEFAULTS
    return Module(
        name="anlas", title="Anlas 续杯", question="V5 用完后，账号的 Anlas 怎么分给常用成员",
        tick=tick, checks=checks, hard_note="关闭后不再重新分配，当天已分配的保持",
        params=[
            Param("一张 V5 约", lambda: anlas_pool.V5_PAID_PRICE, "Anlas"),
            Param("保留不分", lambda: d["anlas_reserve"]),
            Param("每人每天最多", lambda: d["anlas_member_daily_cap"]),
            Param("近 7 天至少出图", lambda: d["anlas_min_images_7d"]),
        ])


# ---------------- 自动驾驶 ----------------
def autopilot_module(state) -> Module:
    from . import autopilot

    async def tick(k: Kernel):
        r = await autopilot.run(state, k.extra.get("registrar"))
        rules = r.get("rules") or {}
        guarded = len((rules.get("key_guard") or {}).get("value") or [])
        return f"全站熔断：{'触发' if (rules.get('breaker') or {}).get('value') else '没有'} · 被限流的 Key：{guarded} 把"

    return Module(
        name="autopilot", title="自动驾驶", question="名额、全站熔断、单个 Key 限流",
        tick=tick, hard_note="关闭后这些设置保持当前值",
        params=[Param("全站熔断", lambda: "15 分钟上游失败 ≥ 5 次且 ≥ 30% → 暂停出图 5 分钟"),
                Param("单个 Key 限流", lambda: "1 小时被拒 ≥ 60 次 → 暂停 1 小时"),
                Param("名额", lambda: f"快满且有余量时 +{autopilot.SLOTS_STEP}，最多 {autopilot.SLOTS_MAX}（名额不限时不动）")])


# ---------------- ④ 诚信（防分享）----------------
def integrity(state) -> Module:
    from . import share_guard as sg

    async def get_enabled(k: Kernel):
        return await state.share.mode() != "off"

    async def set_enabled(k: Kernel, on: bool):
        # 重新打开只回到 observe（校准前不处罚）；要真的处罚需在后台单独切 enforce
        await site_flags.put(state.db, site_flags.SHARE_MODE, "observe" if on else "off")

    async def checks(k: Kernel):
        now = time.time()
        db = state.db
        rows = await db._db.execute_fetchall(
            # 自动驾驶的 1 小时限流（pause_key）不写 paused_ts、也不算违规，不属于防分享处罚，不要求强证据
            "SELECT key_id FROM share_state WHERE (paused_until>? AND paused_ts>0) OR strikes>0", (now,))
        unsupported = []
        for (kid,) in rows:
            strong = await _q1(db, f"SELECT COUNT(*) FROM share_evidence WHERE key_id=? AND kind IN "
                                   f"({','.join('?' * len(sg.STRONG))}) AND points>0", kid, *sg.STRONG)
            if not strong:
                unsupported.append(kid)
        return [Check("每个处罚都有强证据", not unsupported,
                      f"{len(rows)} 把 Key 在处罚中" + (f"，其中 {unsupported} 没有强证据" if unsupported else ""))]

    return Module(
        name="integrity", title="诚信（防分享）", question="有没有人共用或转卖 Key",
        get_enabled=get_enabled, set_enabled=set_enabled, checks=checks,
        hard_note="关闭后不再收集证据和处罚；已暂停的 Key 到期自动恢复",
        params=[
            Param("模式", lambda: state.share.mode(), "observe = 只记录不处罚"),
            Param("提醒 / 暂停 / 重置", lambda: f"{P('share.warn', sg.WARN)} / {P('share.pause', sg.PAUSE)} / {P('share.reset', sg.RESET)} 分"),
            Param("强证据 / 辅助证据", lambda: f"{sg._pts('overlap')} / {sg.POINTS['multi_device']} 分", "辅助证据要 72 小时内有强证据才计分"),
            Param("分数半衰期", lambda: f"{P('share.half_life', sg.HALF_LIFE) // 3600} 小时"),
            Param("停用前违规次数", lambda: sg.STRIKES_BAN),
        ])


# ---------------- 领 Key ----------------
def registration(state) -> Module:
    async def get_enabled(k: Kernel):
        return await site_flags.get(state.db, site_flags.REGISTER_OPEN)

    async def set_enabled(k: Kernel, on: bool):
        await site_flags.put(state.db, site_flags.REGISTER_OPEN, on)

    async def checks(k: Kernel):
        reg = k.extra.get("registrar")
        if reg is None:
            return []
        cfg = await reg.settings()
        active = await reg.count_active()
        cap = cfg.get("max_users") or 0
        return [Check("已领人数 ≤ 名额", not cap or active <= cap, f"{active} / {cap}")]

    return Module(
        name="registration", title="领 Key", question="新成员能不能领 Key",
        get_enabled=get_enabled, set_enabled=set_enabled, checks=checks,
        hard_note="关闭 = 暂停领取；已领的成员和邀请 Key 绑定不受影响",
        params=[Param("Key 有效天数", lambda: getattr(state.settings, "default_key_expires_days", 30)),
                Param("闲置回收", lambda: f"{state.settings.key_inactivity_delete_days} 天" if state.settings.key_inactivity_delete_days else "关")])


# ---------------- ⑥ 观测 ----------------
def observation(state) -> Module:
    async def checks(k: Kernel):
        # 生成记录（audit.py）和用量日志是两条独立的写入路径；「每日计数 ↔ 日志」在同一个事务里写，是恒等式
        db = state.db
        day = state.day()
        start = time.mktime(time.strptime(day, "%Y-%m-%d"))
        logged = int(await _q1(db, "SELECT COALESCE(SUM(images),0) FROM usage_log WHERE ts>=? AND status='ok' "
                                   "AND kind LIKE 'image%'", start) or 0)
        audited = int(await _q1(db, "SELECT COUNT(*) FROM generation_audit WHERE ts>=? AND status='ok'", start) or 0)
        if not audited:
            return [Check("生成记录 ↔ 用量日志", True, "生成记录未开启或今天还没有记录")]
        tol = max(2, logged * 0.005)
        return [Check("生成记录 ↔ 用量日志", abs(audited - logged) <= tol, f"生成记录 {audited} 条，日志 {logged} 张"),
                await _v5_rate_check()]

    async def _v5_rate_check() -> Check:
        """V5 恢复速度：接口读数和「每天余量变化 + 用掉的量」反推的实测值核对（2026-10-10 单次读数错了一倍）。"""
        try:
            last = json.loads(await state.db.get_setting("quota_algo_last", "{}") or "{}")
        except (TypeError, ValueError):
            last = {}
        rc = ((last.get("v5") or {}).get("rate_check")) or {}
        if not rc:
            return Check("V5 恢复速度：接口 ↔ 实测", True, "还没有数据")
        fmt = lambda v: "—" if v is None else f"{v:.1f}%/天"
        detail = f"接口 {fmt(rc.get('api'))} · 实测 {fmt(rc.get('measured'))} · 采用 {fmt(rc.get('used'))}（{rc.get('status')}）"
        return Check("V5 恢复速度：接口 ↔ 实测", rc.get("status") != "对不上", detail)

    return Module(
        name="observation", title="观测", question="各处记录的数字对不对得上",
        checks=checks, hard_note="关闭后不再核对；日志照常记录")


# ---------------- ③ 公平调度（DRR）----------------
def scheduling(state) -> Module:
    from . import scheduling as sch

    async def get_enabled(k: Kernel):
        return (await state.db.get_setting(sch.MODE_SETTING, "observe")) != "off"

    async def set_enabled(k: Kernel, on: bool):
        await state.db.set_setting(sch.MODE_SETTING, "observe" if on else "off")

    async def tick(k: Kernel):
        r = getattr(state, "sched").report()
        return f"近 10 分钟 {r['active_keys']} 把 Key · 不公平决策 {r['unfair_rate']:.0%} · 最大差距 {r['skew']}"

    async def checks(k: Kernel):
        r = getattr(state, "sched").report()
        if r["shadow_decisions"] < 20:
            return [Check("公平性（影子）", True, f"样本不足（{r['shadow_decisions']} 次多人排队）")]
        if r["active_keys"] < 2:          # 只有一个人在用时他占 100% 是必然的，谈不上垄断（以前每晚都误报）
            return [Check("没有 Key 垄断队列", True, f"近 10 分钟只有 {r['active_keys']} 把 Key 在用")]
        # 影子观察：没有人被明显垄断、也没人被饿着时算健康；FIFO 下不公平率高 = DRR 值得开
        return [Check("没有 Key 垄断队列", r["monopoly"] <= 0.6,
                      f"近 10 分钟最高一把 Key 占 {r['monopoly']:.0%}，出图差距 {r['skew']}，"
                      f"FIFO 不公平决策 {r['unfair_rate']:.0%}（DRR 可消除）")]

    return Module(
        name="scheduling", title="排队顺序", question="多人排队时，下一张轮到谁",
        get_enabled=get_enabled, set_enabled=set_enabled, tick=tick, checks=checks,
        hard_note="现在按先来后到出图；这里只统计「如果轮流出图会怎样」，不改顺序",
        params=[Param("实际顺序", lambda: "先来后到"),
                Param("对照：轮流出图", lambda: f"每把 Key 连出 {P('scheduling.quantum', sch.DEFAULT_QUANTUM)} 张后让队，"
                      f"等满 {P('scheduling.starvation_seconds', 60)} 秒插最前")])


# ---------------- 风险检查 ----------------
def risk(state) -> Module:
    """出站请求的样子和滥用线索（risk_check.py），每 10 分钟体检一次；只展示，不处罚、不记 Bug。"""
    from . import risk_check

    async def checks(k: Kernel):
        data = await risk_check.collect(state, time.time())
        return [Check(it["title"], True, it["detail"], level=it["status"], evidence=it["evidence"]) for it in data["items"]]

    return Module(name="risk", title="风险检查", question="出站请求像不像正常浏览器，有没有滥用迹象",
                  checks=checks, report_bugs=False, hard_note="关闭后不再体检；不影响任何处罚")


def build(state, bug) -> Kernel:
    k = Kernel(state, bug)
    for make in (observation, capacity, allocation, scheduling, anlas, autopilot_module, integrity, risk, registration):
        k.register(make(state))
    return k
