"""Discord 机器人（奶妹）的配置与运行状态，存在网关数据库里，后台「Discord」页可改。

机器人每分钟通过桥接接口（/self-register/bot/*，用 REGISTRATION_BRIDGE_SECRET 鉴权）读一次配置、
上报心跳；点赞 / 评论等动作逐条上报，后台能看到最近 50 条。改配置不用重启机器人。
密钥（机器人 Token、AI 评论的 API Key）不进数据库，仍只放在服务器 .env 里。
"""
from __future__ import annotations

import json
import time
from typing import Any

# 名称 → (默认值, 类型, 下限, 上限)；字符串的上下限是长度
FIELDS: dict[str, tuple[Any, type, int, int]] = {
    "gallery_forum": ("跑图分享", str, 1, 50),       # 论坛频道名包含这几个字就算跑图分享
    "gallery_like": (0, int, 0, 1),                 # 新帖自动点 ❤️（默认关：机器人自动互动是 Discord 标记的信号之一）
    "gallery_ai": (0, int, 0, 1),                   # 新帖 AI 看图写评论（默认关）
    "gallery_ai_daily": (30, int, 0, 500),          # AI 评论每天最多几条
    "gallery_ai_model": ("LongCat-2.5-Preview", str, 1, 80),
}
STATUS_KEY = "bot_status"
EVENTS_KEY = "bot_events"
EVENT_KINDS = {"like": "点赞", "comment": "AI 评论", "shy": "捂眼睛（露骨）", "skip": "跳过", "error": "出错"}


def _setting(name: str) -> str:
    return "bot_" + name


def validate(body: dict) -> dict[str, Any]:
    """后台提交的字段 → 合法值；不认识的字段忽略，不合法抛 ValueError（带中文原因）。"""
    out: dict[str, Any] = {}
    for name, (_, kind, lo, hi) in FIELDS.items():
        if name not in body:
            continue
        v = body[name]
        if kind is int:
            if isinstance(v, bool):
                v = int(v)
            if not isinstance(v, int) or not lo <= v <= hi:
                raise ValueError(f"{name} 需要是 {lo}～{hi} 的整数")
        else:
            v = str(v).strip()
            if not lo <= len(v) <= hi:
                raise ValueError(f"{name} 长度需要 {lo}～{hi} 个字")
        out[name] = v
    return out


async def load(db) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, (default, kind, _, _) in FIELDS.items():
        raw = await db.get_setting(_setting(name), None)
        try:
            out[name] = default if raw is None else kind(raw)
        except (TypeError, ValueError):
            out[name] = default
    return out


async def save(db, values: dict[str, Any]) -> None:
    if values:
        await db.set_settings_bulk({_setting(k): v for k, v in values.items()})


async def report(db, status: dict | None, event: dict | None, now: float | None = None) -> None:
    """机器人上报：心跳状态整体覆盖；事件追加到最近 50 条。"""
    now = time.time() if now is None else now
    if status is not None:
        keep = {k: status.get(k) for k in ("user", "guild", "latency_ms", "ready_at", "ai_ready", "ai_today",
                                           "version") if k in status}
        await db.set_setting(STATUS_KEY, json.dumps({**keep, "seen_at": now}, ensure_ascii=False))
    if event is not None and event.get("kind") in EVENT_KINDS:
        events = json.loads(await db.get_setting(EVENTS_KEY, "[]") or "[]")
        events.append({"at": now, "kind": event["kind"],
                       **{k: str(event.get(k) or "").lstrip("# ")[:120] for k in ("title", "author", "detail", "url")}})
        await db.set_setting(EVENTS_KEY, json.dumps(events[-50:], ensure_ascii=False))


async def snapshot(db, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    status = json.loads(await db.get_setting(STATUS_KEY, "{}") or "{}")
    seen = status.get("seen_at")
    status["online"] = bool(seen and now - seen < 180)        # 心跳每分钟一次，3 分钟没收到算离线
    return {"config": await load(db), "status": status,
            "events": list(reversed(json.loads(await db.get_setting(EVENTS_KEY, "[]") or "[]"))),
            "kinds": EVENT_KINDS}
