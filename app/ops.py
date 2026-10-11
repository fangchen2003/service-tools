"""站长可在运行中调整的开关（后台与 Discord 机器人共用）：注册、功能、生成记录。

所有值保存在 site_settings，优先于环境变量；机器人每次命令都实时读取，所以改了立刻生效。
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

from . import site_flags
from . import features
from .action_log import log_action
from .audit import audit_flags



async def economy_enabled(db) -> bool:
    return await site_flags.get(db, site_flags.ECONOMY)


async def set_economy(db, on: bool, state=None, *, by: str = "站长") -> bool:
    """开 / 关节约模式（全站免费档统一 14 步 + Euler-a，Anlas 约省 40%）。
    发生变化时写操作日志，并在成员公告频道通知（无论算法还是站长触发都公告）。返回是否发生变化。"""
    cur = await site_flags.get(db, site_flags.ECONOMY)
    if cur == bool(on):
        return False
    await site_flags.put(db, site_flags.ECONOMY, bool(on))
    await log_action(db, by, "节约模式", "", "开启" if on else "关闭")
    if state is not None and getattr(state, "nai", None) is not None:
        # V5 额度跟着节约模式放大 / 回落：立刻重算一次，不等 10 分钟一次的定时任务
        try:
            from . import quota_algo
            await quota_algo.run(state)
        except Exception as exc:
            print(f"[warn] quota recalc after economy toggle failed: {type(exc).__name__}", flush=True)
    announcer = getattr(state, "announcer", None) if state is not None else None
    if announcer is not None:
        if on:
            announcer.post("⚙️ **节约模式已开启**：当前使用人较多，为了让更多人都能出到图，暂时统一用 14 步快速出图"
                           "（更快、更省额度）。V5 Full 会自动换成官方 Medium 档，画质接近正常，但自定义负面词暂时不生效"
                           "（可在正面提示词里写 -2::xx:: 排除）。空闲后会恢复正常高质量模式。")
        else:
            announcer.post("✅ **节约模式已关闭**：已恢复正常步数和采样器，高质量出图。")
    return True


def env_audit_defaults() -> SimpleNamespace:
    truth = lambda name: os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")
    try:
        days = int(os.getenv("AUDIT_RETENTION_DAYS", "7") or 7)
    except ValueError:
        days = 7
    return SimpleNamespace(audit_prompts=truth("AUDIT_PROMPTS"), audit_thumbs=truth("AUDIT_THUMBS"),
                           audit_retention_days=days)


async def registration_settings(db, service) -> dict[str, Any]:
    """生效的注册设置：数据库里保存的优先，其次是服务启动时的环境默认值。"""
    async def read(name):
        return await db.get_setting(name, None)

    def to_int(value, default):
        try:
            return max(0, int(float(value)))
        except (TypeError, ValueError):
            return default

    defaults = service
    open_raw = await read("register_open")
    feats_raw = await read("register_features")
    return {
        "configured": service is not None,
        "open": site_flags.parse(site_flags.REGISTER_OPEN, open_raw, False),   # 默认关闭：站长明确开放后才接受注册
        "max_users": to_int(await read("register_max_users"), defaults.max_users if defaults else 0),
        "features": (None if feats_raw == "*" else features.parse_list(feats_raw)) if feats_raw is not None else
                    (features.parse_list(defaults.key_features) if defaults else None),
        "daily_images": to_int(await read("register_daily_images"), defaults.key_daily_images if defaults else 30),
        "daily_v5": to_int(await read("register_daily_v5"), defaults.key_daily_v5 if defaults else 0),
        "image_scope": (await read("register_image_scope")) or (defaults.key_image_scope if defaults else "legacy"),
        "expires_days": to_int(await read("register_expires_days"), defaults.key_expires_days if defaults else 30),
        # 限定身份组：只有在这个 Discord 服务器里拥有这个身份组的人能领 Key（可以是别的社区的服务器）。
        # 都为空时沿用启动配置（DISCORD_ROLE_ID，本服务器）。
        "role_guild": (await read("register_role_guild")) or "",
        "role_id": (await read("register_role_id")) or "",
        "role_note": (await read("register_role_note")) or "",
    }


async def v5_capacity(db, settings, service) -> dict[str, Any]:
    """名额 × 每人每日 V5 是否超过全站 V5 日限；超过时后来的人当天可能用不到 V5。全站日限不随名额自动变化。"""
    reg = await registration_settings(db, service)
    try:
        glob = await site_flags.get(db, site_flags.GLOBAL_DAILY_V5, settings)
    except (TypeError, ValueError):
        glob = 0
    seats, per = reg["max_users"], reg["daily_v5"]
    need = seats * per if reg["image_scope"] == "all" and seats and per else 0
    short = bool(need and glob and need > glob)
    message = (f"名额 {seats} × 每人 V5 {per} = {need} 张/天，超过全站 V5 日限 {glob}："
               f"先用的人用满后，后面的人当天没有 V5。建议把全站 V5 调到 {need}，或降低每人 V5。") if short else ""
    return {"need": need, "global": glob, "short": short, "message": message}


async def _active_count(db) -> int:
    from .registration import count_registered      # 与实际名额检查同一口径
    return await count_registered(db)


async def set_registration(db, body: dict, state=None, service=None) -> None:
    """保存注册设置；传入 state 时，开放/关闭或名额上限变化会在公告频道通知成员（body.notify=false 可关闭）。"""
    before = await registration_settings(db, service) if state is not None else None
    values: dict[str, Any] = {}
    if "open" in body:
        values["register_open"] = "1" if body["open"] else "0"
    for name, low, high in (("max_users", 0, 1000), ("daily_images", 0, 100000), ("daily_v5", 0, 100000),
                            ("expires_days", 0, 3650)):
        if name in body:
            number = int(body[name])
            if not low <= number <= high:
                raise ValueError(f"{name} 超出范围 {low}～{high}")
            values["register_" + name] = number
    if "image_scope" in body:
        values["register_image_scope"] = "all" if body["image_scope"] == "all" else "legacy"
    for name in ("role_guild", "role_id"):
        if name in body:
            v = str(body[name] or "").strip()
            if v and not (v.isdecimal() and 15 <= len(v) <= 21):
                raise ValueError(f"{name} 需要是 Discord 的数字 ID")
            values["register_" + name] = v
    if "role_note" in body:
        values["register_role_note"] = str(body["role_note"] or "").strip()[:60]
    if "features" in body:
        # None = 全部已开放功能，用 "*" 显式保存；空字符串表示“一个功能都不开”。
        values["register_features"] = "*" if body["features"] is None else features.dump(body["features"])
    if values:
        await db.set_settings_bulk(values)
    announcer = getattr(state, "announcer", None) if state is not None else None
    if announcer is not None and body.get("notify", True):
        after = await registration_settings(db, service)
        if (before["open"], before["max_users"]) != (after["open"], after["max_users"]):
            active = await _active_count(db)
            cap = after["max_users"]
            left = f"，当前剩余名额 {max(0, cap - active)}（上限 {cap}）" if cap else "，名额不限"
            if not after["open"]:
                text = "📢 **名额公告**：自助领取已暂停，已领取的成员不受影响。"
            elif not before["open"]:
                text = f"📢 **名额公告**：自助领取已开放{left}。在 🔑｜领取key 输入 `/register` 即可。"
            else:
                text = f"📢 **名额公告**：名额上限已调整{left}。"
            announcer.post(text)


async def set_global_features(db, flags: dict) -> dict[str, bool]:
    import json
    current = await features.global_flags(db)
    for name, value in flags.items():
        if name in features.FEATURES and isinstance(value, bool):
            current[name] = value
    await db.set_setting(features.GLOBAL_KEY, json.dumps(current))
    return current


async def set_audit(state, body: dict) -> dict:
    """保存记录开关与保留天数；记录范围变化且 notify 为真时，在成员公告频道说明（文案与首页同源）。"""
    from . import site_flags
    from .audit import IMAGE_CAP_KEY, audit_image_cap_mb, audit_image_days, audit_image_keep_hours, audit_notice
    prompts, thumbs, days = await audit_flags(state.db, state.settings)
    cap_mb = await audit_image_cap_mb(state.db)
    keep_hours = await audit_image_keep_hours(state.db)
    download = bool(await audit_image_days(state.db))
    before = (prompts, thumbs, days, keep_hours, download)
    if "prompts" in body:
        prompts = bool(body["prompts"])
    if "thumbs" in body:
        thumbs = bool(body["thumbs"])
    if "retention_days" in body:
        days = max(0, min(int(body["retention_days"]), 3650))      # 0 = 长期保留
    if "image_cap_mb" in body:
        cap_mb = max(0, min(int(body["image_cap_mb"]), 500_000))   # 0 = 不限
    if "image_keep_hours" in body:
        keep_hours = max(0, min(int(body["image_keep_hours"]), 8760))   # 0 = 只按容量上限
    if "member_download" in body:
        download = bool(body["member_download"])
    await site_flags.put(state.db, site_flags.IMAGE_KEEP_HOURS, keep_hours)
    await site_flags.put(state.db, site_flags.MEMBER_DOWNLOAD, download)
    image_days = await audit_image_days(state.db)
    await state.db.set_settings_bulk({"audit_prompts": "1" if prompts else "0",
                                      "audit_thumbs": "1" if thumbs else "0",
                                      "audit_retention_days": days,
                                      IMAGE_CAP_KEY: cap_mb})
    if body.get("notify", True) and (prompts, thumbs, days, keep_hours, download) != before:
        announcer = getattr(state, "announcer", None)
        if announcer is not None:
            if prompts or thumbs:
                text = "📢 **数据记录说明**：" + audit_notice(prompts, thumbs, days, image_days, keep_hours)
            else:
                text = "📢 **数据记录说明**：站长已关闭生成记录，不再保存新的提示词和原图（已有记录到期自动删除）。"
            announcer.post(text)
    return {"prompts": prompts, "thumbs": thumbs, "retention_days": days, "image_retention_days": image_days,
            "image_cap_mb": cap_mb, "image_keep_hours": keep_hours, "member_download": download}
