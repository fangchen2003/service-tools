"""Manual comparison of official balances and Gate accounting."""
from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time
from email.utils import parsedate_to_datetime

import httpx

COOLDOWN_SETTING = "anlas_reconciliation_retry_at"


class ReconciliationError(Exception):
    def __init__(self, message: str, status: int = 503, retry_after: int = 0):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after


def amount(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1e12:
        raise ValueError("invalid balance")
    return value


def balance(data):
    if not isinstance(data, dict) or data.get("active") is not True:
        raise ValueError("subscription unavailable")
    steps = data["trainingStepsLeft"]
    result = {"fixed": amount(steps["fixedTrainingStepsLeft"]),
              "purchased": amount(steps["purchasedTrainingSteps"])}
    # Use subscription expiry to detect a new billing cycle.
    expiry = data.get("expiresAt")
    result["expires_at"] = amount(expiry) if expiry is not None else None
    return result


def comparison(previous, current):
    if previous is None:
        return {"reason": "first"}
    old, new = previous["accounts"], current["accounts"]
    if old.keys() != new.keys():
        return {"reason": "accounts_changed"}
    if any(old[k]["expires_at"] != new[k]["expires_at"] for k in old):
        return {"reason": "cycle_changed"}
    if any(new[k][part] > old[k][part] for k in old for part in ("fixed", "purchased")):
        return {"reason": "balance_increased"}
    before, after = previous["ledger"], current["ledger"]
    if any(after[k] < before[k] for k in before):
        return {"reason": "ledger_reset"}
    decrease = round(previous["balance"] - current["balance"], 4)
    recorded = round(after["anlas"] - before["anlas"], 4)
    return {"reason": None, "since": previous["checked_at"],
            "balance_decrease": decrease, "recorded": recorded,
            "difference": round(decrease - recorded, 4),
            "unconfirmed_anlas": round(after["unconfirmed_anlas"] - before["unconfirmed_anlas"], 4),
            "unconfirmed_requests": after["unconfirmed_requests"] - before["unconfirmed_requests"]}


class ManualReconciliation:
    wait_timeout = 30
    query_timeout = 30

    def __init__(self, db, nai, image_lock):
        self.db, self.nai, self.image_lock = db, nai, image_lock
        self.lock = asyncio.Lock()

    async def retry_after(self):
        value = float(await self.db.get_setting(COOLDOWN_SETTING, 0))
        return max(0, math.ceil(value - time.time()))

    async def status(self):
        # Read cached history and cooldown state.
        return {"history": await self.db.reconciliation_history(),
                "retry_after": await self.retry_after(), "running": self.lock.locked()}

    def accounts(self):
        # Include disabled accounts whose usage is still in the ledger.
        return {token.token_id: token for token in self.nai.pool}

    async def query(self, token):
        response = None
        try:
            async with asyncio.timeout(8):
                request = self.nai._client.build_request(
                    "GET", self.nai.image_host + "/user/subscription",
                    headers={"Authorization": "Bearer " + token.token, "Accept": "application/json"},
                    timeout=8)
                response = await self.nai._client.send(request, stream=True, follow_redirects=False)
                if response.status_code == 429:
                    delay = 300
                    value = response.headers.get("retry-after", "")
                    try:
                        seconds = int(value) if value.isdecimal() else parsedate_to_datetime(value).timestamp() - time.time()
                        delay = max(delay, min(86400, math.ceil(seconds)))
                    except (ValueError, TypeError, OverflowError):
                        pass
                    await self.db.set_setting(COOLDOWN_SETTING, time.time() + delay)
                    raise ReconciliationError("官方查询限流，请稍后重试；上次结果已保留", 429, delay)
                if response.status_code != 200:
                    raise ValueError("subscription unavailable")
                body = bytearray()
                async for chunk in response.aiter_bytes(8192):
                    if len(body) + len(chunk) > 65536:
                        raise ValueError("subscription too large")
                    body.extend(chunk)
                return balance(json.loads(body))
        except (httpx.HTTPError, ValueError, KeyError, TypeError, TimeoutError, OverflowError, RecursionError):
            # Official responses/exceptions may contain account data or credentials.
            raise ReconciliationError("官方余额未能完整读取；上次结果已保留") from None
        finally:
            if response is not None:
                from .nai import _wait_cleanup
                await _wait_cleanup(asyncio.create_task(response.aclose()))

    async def run(self):
        if self.lock.locked():
            raise ReconciliationError("已有核对正在进行，请稍后查看结果", 409)
        async with self.lock:
            retry = await self.retry_after()
            if retry:
                raise ReconciliationError(f"请在 {retry} 秒后重试", 429, retry)
            # Wait for dispatched image work and its accounting to finish.
            try:
                await asyncio.wait_for(self.image_lock.acquire(), self.wait_timeout)
            except TimeoutError:
                raise ReconciliationError("图片任务仍在处理，请稍后核对；尚未查询官方", 409) from None
            try:
                accounts = self.accounts()
                if not accounts or self.nai._client is None:
                    raise ReconciliationError("尚未配置可查询的上游账号")
                if any(t.disabled for t in accounts.values()):
                    raise ReconciliationError("有上游账号已失效，请先检查令牌池；尚未查询官方")
                blocked = max(t.blocked_until for t in accounts.values()) - time.time()
                if blocked > 0:
                    raise ReconciliationError("上游账号正在冷却，请稍后核对", 429, math.ceil(blocked))
                # Persist the cooldown before querying, including failed attempts.
                await self.db.set_setting(COOLDOWN_SETTING, time.time() + 60)
                async with asyncio.timeout(self.query_timeout):
                    rows = {identity: await self.query(token) for identity, token in accounts.items()}
                if accounts.keys() != self.accounts().keys():
                    raise ReconciliationError("查询期间账号列表有变化，请重新查询")
                ledger = await self.db.reconciliation_totals()
                current = {"checked_at": time.time(), "accounts": rows, "ledger": ledger,
                           "balance": sum(row["fixed"] + row["purchased"] for row in rows.values())}
                previous = await self.db.reconciliation_history(limit=1)
                current["comparison"] = comparison(previous[0] if previous else None, current)
                # Finish saving the validated snapshot even if the caller disconnects.
                from .nai import _wait_cleanup
                try:
                    await _wait_cleanup(asyncio.create_task(self.db.save_reconciliation(current)))
                except sqlite3.Error:
                    raise ReconciliationError("核对结果未能保存；上次结果已保留") from None
            except TimeoutError:
                raise ReconciliationError("本次核对超时；上次结果已保留") from None
            finally:
                self.image_lock.release()
        return await self.status()
