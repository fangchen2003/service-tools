"""首页实时架构图的数据：只给汇总数字，不含任何人的 Key、提示词、IP 或 Discord ID。

/public/live 每秒最多计算一次（结果缓存 1 秒），前端每秒轮询；/v1/live/me 需要 Key，只返回这把 Key 的图在队列里的位置。
"""
from __future__ import annotations

from . import site_flags

import time
from collections import deque
from typing import Any, Optional

WINDOW = 60
_EVENTS: deque = deque(maxlen=5000)          # (ts, status) 最近的请求结果，用于「近 1 分钟」
_cache: dict[str, Any] = {"at": 0.0, "body": None}


def note(status: str) -> None:
    _EVENTS.append((time.time(), status))


def recent(now: Optional[float] = None) -> dict[str, int]:
    now = time.time() if now is None else now
    while _EVENTS and _EVENTS[0][0] < now - WINDOW:
        _EVENTS.popleft()
    total = len(_EVENTS)
    rejected = sum(1 for _, s in _EVENTS if s == "rejected")
    return {"requests": total, "rejected": rejected}


def mask(name: str) -> str:
    name = (name or "").strip()
    return (name[:2] + "**") if name else "**"


async def build(state, registrar, now: Optional[float] = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    if _cache["body"] is not None and now - _cache["at"] < 1.0:
        return _cache["body"]
    guard = getattr(state, "guard", None)
    pool = list(getattr(state.nai, "pool", []))
    day = state.day()
    mono = time.monotonic()
    accounts = []
    images_today = units_today = 0
    for t in pool:
        c = await state.db.get_upstream_counter(t.token_id, day)
        images_today += c["images"]
        units_today += c.get("units", c["images"])     # 和每日上限比较用折算值（节约模式的图算半张）
        accounts.append({
            "usable": t.usable,
            "cooling": max(0, int(t.blocked_until - now)) if not t.disabled and t.admin_enabled else 0,
            "next_in": round(max(0.0, t.image_next_at - mono), 1),
            "hour": guard.hour_count(t.token_id, now) if guard else 0,
        })
    gv = guard.values if guard else {}
    usable = max(1, sum(1 for a in accounts if a["usable"]))
    try:
        v5_cap = await site_flags.get(state.db, site_flags.GLOBAL_DAILY_V5, state.settings)
    except (TypeError, ValueError):
        v5_cap = 0
    body: dict[str, Any] = {
        "t": now,
        "mode": "fifo",                         # 启用轮流出图后为 "drr"
        "auth": recent(now),
        "quota": {"images_today": images_today, "units_today": round(units_today, 1),
                  "images_cap": (gv.get("account_daily_cap") or 0) * usable,
                  "v5_today": await state.db.day_v5_total(day), "v5_cap": v5_cap},
        "queue": guard.queue_view() if guard else {"waiting": 0, "running": 0},
        "guard": {"hour": sum(a["hour"] for a in accounts),
                  "hour_cap": (guard.hourly_cap(now) if guard else 0) * usable,
                  "quiet": guard.in_quiet(now) if guard else False,
                  "next_in": min((a["next_in"] for a in accounts if a["usable"]), default=0),
                  "cooldown": state.image_cooldown_remaining() if hasattr(state, "image_cooldown_remaining") else 0},
        "accounts": [{"usable": a["usable"], "cooling": a["cooling"]} for a in accounts],
        "members": {},
        "interval": round(float(getattr(state.settings, "image_min_interval", 15) or 15) + gv.get("interval_jitter", 0) / 2, 1),
    }
    # 调度规则（首页「点一步看规则」用）：全是站点级参数和汇总，不含任何个人信息
    anlas = {}
    try:
        import json as _json
        raw = await state.db.get_setting("anlas_pool_last", None)
        last = _json.loads(raw) if raw else {}
        anlas = {"per_member": int(last.get("per_member") or 0), "members": len(last.get("members") or []),
                 "enabled": bool(last.get("enabled"))}
    except (TypeError, ValueError):
        pass
    quota = {}
    try:
        q = _json.loads(await state.db.get_setting("quota_algo_last", None) or "{}")
        v5 = q.get("v5") or {}
        quota = {"ceiling": q.get("ceiling"), "v5_each": v5.get("each"), "v5_global": v5.get("global"),
                 "v5_rate": v5.get("rate"), "v5_percent": v5.get("percent")}
    except (TypeError, ValueError):
        pass
    token_ids = [t.token_id for t in pool if t.usable]
    # 和出图检查同一个条件：没人排队 + 按时段放出的份额还剩半小时以上的量（main.quota_image_check）
    borrow_open = False
    if guard is not None and guard.site_idle(-1, token_ids, now):
        try:
            reserve = (gv.get("account_daily_cap") or 0) / 48 * len(token_ids)
            borrow_open = await guard.paced_headroom(state.db, token_ids, day, now) > reserve
        except Exception:
            borrow_open = False
    body["rules"] = {
        "base": gv.get("base_daily_images", 0), "key_queue": gv.get("key_image_queue", 0),
        "queue_cap": (gv.get("queue_per_account") or 0) * usable,
        "hourly_cap": (gv.get("account_hourly_cap") or 0) * usable, "daily_cap": (gv.get("account_daily_cap") or 0) * usable,
        "quiet_start": gv.get("quiet_start", 0), "quiet_end": gv.get("quiet_end", 0),
        "quiet_cap": (gv.get("quiet_hourly_cap") or 0) * usable,
        "min_interval": float(getattr(state.settings, "image_min_interval", 15) or 15), "jitter": gv.get("interval_jitter", 0),
        "borrow_open": borrow_open,
        "anlas": anlas,
        "quota": quota,          # 动态额度的当前结果（quota_algo.py）
    }
    if registrar is not None:
        cfg = await registrar.settings()
        active = await registrar.count_active()
        wl = await registrar.waitlist()
        body["members"] = {"max": cfg["max_users"], "active": active, "open": cfg["open"],
                           "waitlist": [{"name": mask(w["name"]), "invited": bool(w["invited_at"])} for w in wl[:5]],
                           "waitlist_total": len(wl)}
    _cache.update(at=now, body=body)
    return body


def mine(state, key_id: int) -> dict[str, Any]:
    guard = getattr(state, "guard", None)
    view = guard.queue_view(key_id) if guard else {"waiting": 0, "running": 0, "mine": []}
    interval = float(getattr(state.settings, "image_min_interval", 15) or 15)
    for item in view["mine"]:
        if item["state"] == "waiting":
            item["eta"] = int(item["position"] * interval)
    return {"mine": view["mine"], "waiting": view["waiting"], "running": view["running"]}
