"""Anonymous, local-only aggregates for the public homepage.

Actual successful V5 images include independent-quota and deleted keys. They
are not the shared pool's weighted quota usage; Medium images cost a fraction
of a High quota unit. No key identities or upstream state are exposed.
"""
from datetime import datetime, time, timedelta
import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .v5_effort import SCALE, settings as effort_settings, units

router = APIRouter()


@router.get("/api/public/stats")
async def public_stats(request: Request):
    state = getattr(request.app.state, "gate", None)
    headers = {"Cache-Control": "no-store"}
    if state is None or state.db._db is None:
        return JSONResponse({"detail": "Statistics temporarily unavailable"},
                            status_code=503, headers=headers)
    try:
        payload = await _stats_payload(state)
    except (sqlite3.Error, ValueError):
        # Fail closed during startup/shutdown or DB outages; never echo paths,
        # settings values, or an upstream exception through the global handler.
        return JSONResponse({"detail": "Statistics temporarily unavailable"},
                            status_code=503, headers=headers)
    return JSONResponse(payload, headers=headers)


async def _stats_payload(state):
    now = datetime.now(state.tz)
    day = state.day(now.timestamp())
    start = datetime.combine(now.date(), time.min, state.tz)
    reset = datetime.combine(now.date() + timedelta(days=1), time.min, state.tz)
    # Range filtering uses idx_log_ts and local calendar boundaries (including
    # 23/25-hour DST days). No join: deleted and independent keys still count.
    async with state.db._db.execute(
        """SELECT COALESCE(SUM(images), 0) FROM usage_log
           WHERE ts>=? AND ts<? AND status='ok' AND images>0
             AND kind IN ('image', 'image_stream')
             AND (lower(trim(model)) IN ('nai-diffusion-5', 'nai-v5')
                  OR lower(trim(model)) GLOB 'nai-diffusion-5-*'
                  OR lower(trim(model)) GLOB 'nai-v5-*')""",
        (start.timestamp(), reset.timestamp()),
    ) as cursor:
        images = int((await cursor.fetchone())[0])
    used = await state.db.day_v5_total(day)
    limit = int(float(await state.db.get_setting(
        "global_daily_v5", state.settings.global_daily_v5) or 0))
    effort = await effort_settings(state.db)
    return {
        "day": day,
        "timezone": str(state.tz),
        "v5_images_today": images,
        "v5_images_scope": "all_keys_including_independent",
        "shared_v5_used": used,
        "shared_v5_limit": limit,
        "shared_v5_remaining": (
            max(0, units(limit) - units(used)) / SCALE if limit > 0 else None),
        "shared_v5_unit": "weighted_quota",
        "medium_cost": effort["v5_medium_multiplier"],
        # RegistrationService.finish creates 30/100 keys, independently of the
        # configurable defaults used when an administrator creates a key.
        "personal_v5_limit": 30,
        "personal_legacy_limit": 100,
        "refreshed_at": now.timestamp(),
        "reset_at": reset.timestamp(),
    }
