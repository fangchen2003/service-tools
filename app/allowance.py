"""V5 subscription cache. Only a real generation job may refresh it."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

SETTING = "v5_alert_threshold"
DEFAULT_THRESHOLD = 20
log = logging.getLogger(__name__)


async def read_alert_threshold(db):
    value = await db.get_setting(SETTING, DEFAULT_THRESHOLD)
    # SQLite settings are stored as TEXT; request validation remains strict.
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 3:
        value = int(value)
    return value if type(value) is int and 1 <= value <= 100 else DEFAULT_THRESHOLD


class AllowanceUnavailable(Exception):
    pass


PERIOD_KEY = "v5_period_samples"      # 每个上游账号最近 48 小时「再恢复 1% 还要多少秒」的读数
PERIOD_WINDOW = 48 * 3600


class AllowanceCache:
    def __init__(self, db):
        self._db = db
        self._rows = {}
        self._locks = {}
        self._periods = None             # {token_id: [[ts, seconds], ...]}，第一次用时从库里读

    async def _note_period(self, token_id, seconds):
        """官方给的是「当前这 1% 还剩多少秒」，不是恢复 1% 要多久：刚好读在快恢复完的时候，
        单次读数算出来的速度会高好几倍（10/10 显示每天 11%，实际约 5%）。
        剩余时间 ≤ 真实周期，所以取 48 小时里最长的那次读数当周期。"""
        if self._periods is None:
            try:
                self._periods = json.loads(await self._db.get_setting(PERIOD_KEY, "{}") or "{}")
            except (TypeError, ValueError):
                self._periods = {}
        now = time.time()
        rows = [r for r in self._periods.get(token_id, []) if now - r[0] < PERIOD_WINDOW]
        rows.append([round(now), int(seconds)])
        self._periods[token_id] = rows[-500:]
        try:
            await self._db.set_setting(PERIOD_KEY, json.dumps(self._periods))
        except Exception:
            pass

    def period(self, token_id):
        now = time.time()
        rows = [r[1] for r in (self._periods or {}).get(token_id, []) if now - r[0] < PERIOD_WINDOW]
        return max(rows) if rows else None

    async def threshold(self):
        return await read_alert_threshold(self._db)

    async def resolve(self, client, host, token_id, token):
        async with self._locks.setdefault(token_id, asyncio.Lock()):
            now = time.monotonic()
            row = self._rows.get(token_id, {})
            if now < row.get("retry_at", 0):
                raise AllowanceUnavailable("V5 额度暂时无法确认，请稍后重试；未发送生图，未扣积分")
            threshold = await self.threshold()
            pct = row.get("percent", 0)
            # 剩 ≤2%（约 30 张）时缓存只用 15 秒（≈ 一次出图间隔）：缓存说「还有」但其实刚用完，
            # 这几十秒里发出去的 V5 会被 NovelAI 按 Anlas 收费、账上却记成免费（2026-10-10 审查 F7）
            ttl = 15 if pct <= 2 else 60 if pct < threshold else 300
            # Never use an old exhausted result to switch a new image to paid.
            if (not row.get("error") and row.get("is_negative") is False
                    and row.get("percent", 0) > 0 and now - row.get("at", -1e9) < ttl):
                return False
            retry_seconds = 60
            response = None
            try:
                async with asyncio.timeout(8):
                    from .nai import default_browser_headers
                    # 和生图同一套浏览器请求头（之前缺 UA，httpx 默认 UA 与 Origin 自相矛盾）
                    headers = default_browser_headers(token)
                    request = client.build_request("GET", host.rstrip('/') + '/user/subscription',
                        headers=headers, timeout=8)
                    response = await client.send(request, stream=True, follow_redirects=False)
                    if response.status_code == 429:
                        retry_seconds = 300
                        value = response.headers.get("retry-after", "")
                        if value.isdecimal() and len(value) < 7:
                            retry_seconds = max(retry_seconds, min(int(value), 86400))
                    if response.status_code != 200:
                        raise ValueError("subscription unavailable")
                    body = bytearray()
                    async for chunk in response.aiter_bytes(8192):
                        if len(body) + len(chunk) > 65536:
                            raise ValueError("subscription too large")
                        body.extend(chunk)
                data = json.loads(body)
                usage = data.get("usage") if isinstance(data, dict) else None
                if (not isinstance(usage, dict) or data.get("active") is not True
                        or type(data.get("tier")) is not int or data["tier"] != 3):
                    raise ValueError("active Opus allowance unavailable")
                percent, negative = usage.get("percent"), usage.get("isNegative")
                if type(percent) is not int or percent < 0 or type(negative) is not bool:
                    raise ValueError("invalid allowance")
                next_pct = usage.get("timeUntilNextPercent")
                self._rows[token_id] = dict(percent=percent, is_negative=negative,
                    checked_at=time.time(), at=time.monotonic(), error=None, retry_at=0,
                    # 官方返回「再恢复 1% 还要多少秒」，据此算出实测恢复速度（%/天）
                    next_percent_seconds=next_pct if type(next_pct) is int and 0 < next_pct < 10 ** 7 else None)
                if self._rows[token_id]["next_percent_seconds"]:
                    await self._note_period(token_id, self._rows[token_id]["next_percent_seconds"])
                if negative or percent < threshold:
                    log.warning("V5 low allowance: account %s; remaining=%s%%; exhausted=%s",
                                token_id, percent, negative)
                    callback = getattr(self, "on_low", None)
                    if callback:
                        callback("v5_low", "V5 官方免费额度" + ("已用尽" if negative else f"仅剩 {percent}%（阈值 {threshold}%）")
                                 + "，之后的 V5 请求会按 Anlas 计费或被拒。", 6 * 3600)
                return negative
            except (httpx.HTTPError, ValueError, TypeError, TimeoutError):
                self._rows[token_id] = {**row, "error": "额度查询失败，当前状态未确认",
                                       "retry_at": time.monotonic() + retry_seconds}
                raise AllowanceUnavailable("V5 额度暂时无法确认，请稍后重试；未发送生图，未扣积分") from None
            finally:
                if response is not None:
                    # Closing the response must finish even when a queued stream is cancelled.
                    from .nai import _wait_cleanup
                    await _wait_cleanup(asyncio.create_task(response.aclose()))

    async def snapshot(self, pool):
        threshold = await self.threshold()
        accounts = []
        for index, token in enumerate(pool):
            row = self._rows.get(token.token_id, {})
            percent = row.get("percent")
            stale = time.monotonic() - row.get("at", -1e9) >= 300
            low = row.get("is_negative") is True or (percent is not None and percent < threshold)
            nps = self.period(token.token_id) or row.get("next_percent_seconds")
            accounts.append(dict(account=index + 1, percent=percent,
                recharge_per_day=round(86400 / nps, 1) if nps else None,
                is_negative=row.get("is_negative"), checked_at=row.get("checked_at"),
                low=low, uncertain=stale or bool(row.get("error")),
                error=row.get("error"), stale=stale))
        return {"threshold": threshold, "accounts": accounts, "mode": "on_demand"}
