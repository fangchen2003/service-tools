"""后台周期维护与治理循环任务。

从原 main.py 剥离，包含审计清理、闲置 Key 回收、Anlas 平衡与状态自检。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import shutil
import time

from . import status_stats
from .action_log import RETENTION_DAYS as ADMIN_ACTION_RETENTION_DAYS
from .key_sources import RETENTION_SECONDS as KEY_SOURCE_RETENTION
from .audit import (
    audit_flags,
    audit_image_cap_mb,
    audit_image_keep_hours,
)

ANLAS_REBALANCE_SECONDS = 600


def _main():
    from . import main
    return main


async def check_upstream_perf() -> None:
    """上游变慢 / 限流增多 / 失败率上升 / 账号受限时私信站长；同类提醒 3 小时内只发一次。"""
    from . import perf
    main = _main()
    state = main.STATE
    try:
        report = await perf.collect(state, time.time())
    except Exception as exc:
        main.bug("maintenance:perf", exc)
        return
    for flag in report["flags"]:
        main.notify_owner(f"perf_{flag['family']}_{flag['code']}",
                          "📉 上游表现变化 · " + flag["text"] + " 详情见后台「上游」页。", 3 * 3600)


async def maintenance_loop() -> None:
    """每 5 分钟：清理过期生成记录；磁盘与告警自检。"""
    main = _main()

    async def purge():
        state = main.STATE
        # 0 = 长期保留：已向成员披露保留范围，用于防滥用、回测和优化算法（见 audit.audit_disclosure）
        days = (await audit_flags(state.db, state.settings))[2]
        if days > 0:
            await state.db.purge_audit(time.time() - days * 86400)
        keep = state.settings.usage_log_retention_days
        if keep > 0:
            await state.db.purge_usage_log(time.time() - max(7, keep) * 86400)
        await state.db.purge_key_sources(time.time() - KEY_SOURCE_RETENTION)
        await state.db.purge_admin_actions(time.time() - ADMIN_ACTION_RETENTION_DAYS * 86400)
        await status_stats.flush(state.db)
        await state.db._db.execute("DELETE FROM status_hourly WHERE hour<?", (time.time() - 30 * 86400,))
        await state.db._db.commit()
        if getattr(state, "bugs", None) is not None:
            await state.bugs.purge(time.time() - 30 * 86400)
        # 原图：保存 N 小时后删（10-11 站长定 24 小时），另外总量超过容量上限时删最旧的；站长也可以在后台手动清空
        try:
            keep_h = await audit_image_keep_hours(state.db)
            if keep_h > 0:
                n = await state.db.purge_audit_images(time.time() - keep_h * 3600)
                if n:
                    print(f"[audit] 原图保存超过 {keep_h} 小时，已删 {n} 张", flush=True)
            await state.db.sweep_orphan_images(time.time() - 3600)
            cap_mb = await audit_image_cap_mb(state.db)
            if cap_mb > 0:
                n = await state.db.purge_audit_images_to_cap(cap_mb * 1024 * 1024)
                if n:
                    print(f"[audit] 原图超过容量上限 {cap_mb} MB，已清掉最旧的 {n} 张", flush=True)
        except Exception as exc:
            main.bug("audit_image_purge", exc)

    async def registrar_jobs():
        registrar = getattr(main.app.state, "registrar", None)
        if registrar is not None:
            for name in ("sync_roles", "backfill_profiles", "sweep_departed", "invite_waitlist"):
                try:
                    if name == "invite_waitlist":
                        ann = getattr(main.STATE, "announcer", None)
                        await registrar.invite_waitlist(announce=ann.post if ann is not None else None)
                    else:
                        await getattr(registrar, name)()
                except Exception as exc:
                    main.bug(f"maintenance:{name}", exc)

    async def disk():
        state = main.STATE
        usage = shutil.disk_usage(state.settings.data_dir)
        if usage.free / usage.total < 0.10:
            main.notify_owner("disk_low", f"服务器磁盘剩余不足 10%（剩 {usage.free // 2**20} MB），请清理或扩容。", 6 * 3600)

    while True:
        async def perf():
            fn = getattr(main, "check_upstream_perf", check_upstream_perf)
            await fn()

        async def official():
            from . import status_page
            await status_page.fetch_official(main.STATE.db)
        for name, job in (("purge", purge), ("registrar", registrar_jobs), ("perf", perf), ("disk", disk),
                          ("official", official)):
            try:
                await job()
            except Exception as exc:
                main.bug(f"maintenance:{name}", exc)
        await asyncio.sleep(300)


async def inactive_key_cleanup_loop() -> None:
    """常驻服务每小时回收一次长期闲置 Key。"""
    main = _main()
    while True:
        try:
            registrar = getattr(main.app.state, "registrar", None)
            if registrar is not None:
                await main.STATE.remind_idle_keys(registrar.send_dm, main.SETTINGS.site_url.rstrip("/"))
            removed = await main.STATE.delete_inactive_keys()
            if removed:
                print(f"[info] deleted {removed} inactive API key(s)")
        except Exception as exc:
            main.bug("cleanup", exc)
        await asyncio.sleep(3600)


async def anlas_rebalance_loop() -> None:
    """每 10 分钟：按模块登记顺序运行各模块的周期任务（容量 / 分配 / Anlas / 自动驾驶……），再做交叉校验。
    模块和开关见 modules.py / kernel.py；单个模块出错不影响其他模块。"""
    main = _main()
    while True:
        kernel = getattr(main.STATE, "kernel", None)
        if kernel is not None:
            try:
                await kernel.tick_all()
            except Exception as exc:
                main.bug("kernel", exc)
        await asyncio.sleep(ANLAS_REBALANCE_SECONDS)


async def registration_reset_loop() -> None:
    """可选：每天固定时刻（REGISTER_RESET_AT=HH:MM，按 TZ）清空全部自助注册用户。"""
    main = _main()
    while True:
        service = getattr(main.app.state, "registrar", None)
        at = service.reset_at if service else ""
        try:
            hour, minute = (int(part) for part in at.split(":"))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError
        except ValueError:
            await asyncio.sleep(3600)
            continue
        now = datetime.now(main.STATE.tz)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            removed = await service.reset_all()
            print(f"[info] scheduled registration reset: removed {removed} user(s)")
        except Exception as exc:
            main.bug("registration_reset", exc)


def start_background_tasks():
    """在应用启动（lifespan）时拉起所有常驻维护协程。"""
    asyncio.create_task(maintenance_loop())
    asyncio.create_task(inactive_key_cleanup_loop())
    asyncio.create_task(anlas_rebalance_loop())
    asyncio.create_task(registration_reset_loop())
