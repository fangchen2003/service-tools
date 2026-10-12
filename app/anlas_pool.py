"""Anlas 自动分配：「用完即作废」的资源按天均分给活跃成员，站长不用手动干预。

Opus 的固定 Anlas 每个账单周期补满到 10000、不累积，到期没用掉就浪费。所以每天重算一次：

    每日池 = (账号剩余 Anlas − 保留) ÷ 距续费天数        （越临近续费，每天能分的越多）
    每日池 ≤ (全站月预算 − 本月已用) ÷ 本月剩余天数       （站长设的月预算仍是硬上限）
    每人每天 = min(每人上限, 每日池 ÷ 合格人数)，低于一张付费 V5 的价格就不分

合格成员：通过 Discord 领取、启用中、未过期、不是测试 Key、能用 V5、近 7 天出图 ≥ N 张、领取满 M 天。
用途只有一个：「V5 续杯」—— 当天个人 V5 用完后，用 Anlas 继续生成同规格的 V5 图。
自动分配的 Key 仍然保留免费档钳制（尺寸、步数），不会因为客户端设置偏大而误扣 Anlas。
站长手动管理的 Key（anlas_auto=-1：手动关闭或手动额度）不参与自动分配，也不会被覆盖。
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx

from . import site_flags
from .action_log import log_action

V5_PAID_PRICE = 30          # 一张免费规格 V5（832×1216 / 1024²，28 步）在没有额度时扣的 Anlas：policy.py 实测系数 20 × V5 1.5 倍；低于它的分配没有意义
DEFAULTS = {
    "anlas_auto_enabled": 1,
    "anlas_reserve": 1000,             # 留给账号本身（V5 额度耗尽时的兜底等）
    "anlas_member_daily_cap": 90,      # 每人每天最多约 3 张续杯 V5（3 × 30）
    "anlas_min_images_7d": 10,
    "anlas_min_key_age_days": 3,
}
STATE_KEY = "anlas_pool_last"


async def _setting(db, name: str) -> int:
    try:
        return int(float(await db.get_setting(name, DEFAULTS[name])))
    except (TypeError, ValueError):
        return DEFAULTS[name]


async def fetch_account(host: str, token: str) -> Optional[dict]:
    """读上游账号的剩余 Anlas 和订阅到期（= 下次补满）时间；失败返回 None。"""
    from .nai import default_browser_headers  # 和生图同一套浏览器请求头
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(host.rstrip("/") + "/user/subscription",
                                 headers=default_browser_headers(token))
        data = r.json() if r.status_code == 200 else None
        steps = data["trainingStepsLeft"]
        return {"anlas": int(steps["fixedTrainingStepsLeft"]) + int(steps.get("purchasedTrainingSteps") or 0),
                "refill_at": float(data["expiresAt"])}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


def plan(anlas: int, refill_at: float, now: float, eligible: int, *, reserve: int, cap: int,
         budget_left: Optional[float], month_days_left: int) -> dict[str, Any]:
    days = max(1.0, (refill_at - now) / 86400)
    pool = max(0.0, anlas - reserve) / days
    if budget_left is not None:
        pool = min(pool, max(0.0, budget_left) / max(1, month_days_left))
    per = min(cap, math.floor(pool / eligible)) if eligible else 0
    if per < V5_PAID_PRICE:
        per = 0
    return {"days_to_refill": round(days, 1), "daily_pool": int(pool), "per_member": per}


async def eligible_keys(db, now: float, min_images: int, min_age_days: int, day_fn) -> list[dict]:
    week = [(datetime.fromtimestamp(now) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    rows = await db._db.execute_fetchall(
        f"""SELECT k.id, k.name, k.created_at, COALESCE(SUM(c.images),0) AS imgs
            FROM api_keys k JOIN discord_registrations r ON r.key_id=k.id
            LEFT JOIN counters c ON c.key_id=k.id AND c.day IN ({",".join("?" * 7)})
            WHERE k.enabled=1 AND k.is_admin=0 AND k.is_test=0 AND k.image_model_scope='all' AND k.anlas_auto>=0
              AND (k.expires_at IS NULL OR k.expires_at>?)
            GROUP BY k.id""", (*week, now))
    return [{"id": r[0], "name": r[1]} for r in rows
            if r[3] >= min_images and now - r[2] >= min_age_days * 86400]


async def rebalance(state, now: Optional[float] = None, account: Optional[dict] = None,
                    notify=None) -> dict[str, Any]:
    """重算并应用。account 可注入（测试用）；否则查询第一把可用上游 Token。"""
    db = state.db
    now = time.time() if now is None else now
    chosen_ids: list[int] = []
    if not await _setting(db, "anlas_auto_enabled"):
        result = {"enabled": False, "per_member": 0, "members": []}
    else:
        if account is None:
            pool = [t for t in getattr(state.nai, "pool", []) if t.usable and t.allow_anlas]
            account = await fetch_account(state.nai.image_host, pool[0].token) if pool else None
        if account is None:
            prev = await db.get_setting(STATE_KEY, None)
            return json.loads(prev) if prev else {"enabled": True, "error": "无法读取上游 Anlas"}
        members = await eligible_keys(db, now, await _setting(db, "anlas_min_images_7d"),
                                      await _setting(db, "anlas_min_key_age_days"), state.day)
        try:
            budget = await site_flags.get(db, site_flags.GLOBAL_MONTHLY_ANLAS, state.settings)
        except (TypeError, ValueError):
            budget = 0.0
        budget_left = budget - await db.month_anlas_all(state.month()) if budget > 0 else None
        today = datetime.fromtimestamp(now)
        nxt = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        p = plan(account["anlas"], account["refill_at"], now, len(members),
                 reserve=await _setting(db, "anlas_reserve"), cap=await _setting(db, "anlas_member_daily_cap"),
                 budget_left=budget_left, month_days_left=max(1, (nxt - today).days))
        result = {"enabled": True, "anlas": account["anlas"], "refill_at": account["refill_at"],
                  **p, "members": [m["name"] for m in members]}
        chosen_ids = [m["id"] for m in members]
    per = result.get("per_member", 0)
    chosen = set(chosen_ids) if per and result.get("enabled") else set()
    current = {r[0]: r[1] for r in await db._db.execute_fetchall(
        "SELECT id, daily_anlas FROM api_keys WHERE anlas_auto=1")}
    for key_id in chosen:
        if current.get(key_id) != per:
            await db._db.execute("UPDATE api_keys SET allow_anlas=1, anlas_auto=1, daily_anlas=?, monthly_anlas=0 WHERE id=?",
                                 (per, key_id))
    if notify is not None:
        for key_id in chosen - set(current):          # 新获得续杯资格的成员，私信说明一次
            try:
                await notify(key_id, f"你近 7 天比较活跃，获得了每天约 {per} Anlas 的自动额度：可以用 Vibe / 角色参照、放大、导演工具，"
                                     "或者超过免费规格的尺寸 / 步数（这些本来就要花 Anlas，不占大家的 V5 免费额度）。"
                                     "额度按全站剩余 Anlas 每天自动重算，不用做任何设置。")
            except Exception:
                pass
    for key_id in set(current) - chosen:
        await db._db.execute("UPDATE api_keys SET allow_anlas=0, anlas_auto=0, daily_anlas=0 WHERE id=?", (key_id,))
    await db._db.commit()
    changed = chosen != set(current) or any(current.get(k) != per for k in chosen)
    result["updated_at"] = now
    await db.set_setting(STATE_KEY, json.dumps(result, ensure_ascii=False))
    if changed:
        await log_action(db, "系统", "Anlas 自动分配",
                         f"{len(chosen)} 人 · 每人每天 {per}",
                         f"剩余 {result.get('anlas')} · 距续费 {result.get('days_to_refill')} 天 · 每日池 {result.get('daily_pool')}")
    return result
