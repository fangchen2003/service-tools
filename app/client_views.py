"""Client compatibility views derived only from the authenticated virtual key."""

from __future__ import annotations

import time

from .v5_effort import SCALE, units


async def subscription_payload(state, key, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    key = dict(key)
    day = state.day(now)
    admin = bool(key.get("is_admin"))
    # Ordinary image generation has no paid balance. Vibe encoding has its own
    # daily entitlement and must not masquerade as arbitrary Anlas credit.
    paid = admin
    monthly_limit = 0
    anlas_left = 0
    vibe_remaining = None if admin else await state.db.get_vibe_remaining(key["id"])

    limits: list[int] = []
    remaining: list[int] = []
    if not admin:
        daily_limit = int(key["daily_v5"] or 0)
        if daily_limit > 0:
            counter = await state.db.get_counter(key["id"], day)
            limits.append(daily_limit)
            remaining.append(max(0, units(daily_limit) - units(counter["v5"])) / SCALE)
        if not key.get("exclude_global_v5"):
            global_limit = int(float(await state.db.get_setting(
                "global_daily_v5", state.settings.global_daily_v5) or 0))
            if global_limit > 0:
                limits.append(global_limit)
                remaining.append(max(0, units(global_limit) - units(await state.db.day_v5_total(day))) / SCALE)
    v5_limit = min(limits, default=0)
    v5_left = min(remaining, default=0)

    return {
        "tier": 3,
        "active": True,
        "expiresAt": int(key["expires_at"] or now + 3650 * 86400),
        "trainingStepsLeft": {
            "fixedTrainingStepsLeft": anlas_left,
            "purchasedTrainingSteps": 0,
        },
        "perks": {
            "imageGeneration": True, "unlimitedImageGeneration": False,
            "voiceGeneration": False, "contextTokens": 0,
        },
        # Local daily quotas use naiGate; official battery usage is unavailable.
        "naiGate": {
            "imageModelScope": "all" if admin else key["image_model_scope"],
            "anlasEnabled": paid,
            "freeOnly": not admin,
            "anlasQuotaEnabled": False,
            "vibeRemainingToday": vibe_remaining,
            "vibeDailyLimit": None if admin else 3,
            "anlasLeft": anlas_left,
            "anlasMonthlyLimit": int(monthly_limit),
            "v5LeftToday": v5_left,
            "v5DailyLimit": v5_limit,
            "v5Unlimited": not limits,
            "isAdmin": admin,
            # Older panels accept null and hide shared account details.
            "account": None,
        },
    }
