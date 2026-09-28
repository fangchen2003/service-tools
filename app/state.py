"""运行期共享状态：限流器、并发闸门、时区工具。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .config import Settings
from .database import Database
from .nai import NaiClient
from .reconciliation import ManualReconciliation


RUNTIME_LIMIT_BOUNDS = {
    "queue_timeout": (15, 300),
    "key_image_min_interval": (15, 120),
    "image_min_interval": (15, 120),
    "image_429_cooldown_seconds": (60, 3600),
}


class GateState:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(str(settings.db_path), settings.tz)
        self.tz = ZoneInfo(settings.tz)
        self.nai = NaiClient(
            tokens=settings.nai_tokens,
            image_host=settings.image_host,
            text_host=settings.text_host,
            legacy_text_host=settings.text_host_legacy,
            db=self.db,
            day_fn=self.day,
            v5_daily_limits=settings.nai_token_v5_daily_limits,
            allow_anlas=settings.nai_token_allow_anlas,
            image_min_interval=settings.image_min_interval,
        )
        self._global_sem = asyncio.Semaphore(max(1, settings.global_concurrency))
        self._key_sems: dict[int, asyncio.Semaphore] = {}
        self._key_image_next_at: dict[int, float] = {}
        self._rpm: dict[int, deque[float]] = {}
        self._tag_active: set[int] = set()
        self._tag_next_at: dict[int, float] = {}
        self._tag_condition = asyncio.Condition()
        self._tag_waiting = 0
        self._login_attempts: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()
        self._image_blocked_until = 0.0
        # Protect only quota reservation mutations; never hold this across HTTP.
        self.image_budget_lock = asyncio.Lock()
        self.image_reservations: dict[int, object] = {}
        self.image_budget_idle = asyncio.Event()
        self.image_budget_idle.set()
        self.reconciliation = ManualReconciliation(
            self.db, self.nai, self.image_budget_lock,
            self.image_reservations, self.image_budget_idle)
        self.global_waiting = 0
        self.global_active = 0
        self._image_pacing_waiting = 0
        self._runtime_limits_lock = asyncio.Lock()

    def runtime_limits_snapshot(self) -> dict[str, int]:
        return {name: int(getattr(self.settings, name)) for name in RUNTIME_LIMIT_BOUNDS}

    async def load_runtime_limits(self) -> None:
        """Persisted panel overrides win over .env after each restart."""
        values = {}
        for name, (minimum, maximum) in RUNTIME_LIMIT_BOUNDS.items():
            raw = await self.db.get_setting("runtime_" + name, None)
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if minimum <= value <= maximum:
                values[name] = value
        await self._apply_runtime_limits(values)

    async def update_runtime_limits(self, values: dict[str, int]) -> dict[str, int]:
        async with self._runtime_limits_lock:
            await self.db.set_settings_bulk({"runtime_" + k: v for k, v in values.items()})
            await self._apply_runtime_limits(values)
            return self.runtime_limits_snapshot()

    async def _apply_runtime_limits(self, values: dict[str, int]) -> None:
        if "key_image_min_interval" in values:
            new = values["key_image_min_interval"]
            old = self.settings.key_image_min_interval
            async with self._lock:
                if new > old:
                    now = time.monotonic()
                    for key_id, next_at in self._key_image_next_at.items():
                        if next_at > now:
                            self._key_image_next_at[key_id] = max(next_at, now + new)
                self.settings.key_image_min_interval = new
        if "image_min_interval" in values:
            await self.nai.set_image_min_interval(values["image_min_interval"])
            self.settings.image_min_interval = values["image_min_interval"]
        for name in ("queue_timeout", "image_429_cooldown_seconds"):
            if name in values:
                setattr(self.settings, name, values[name])

    # ---------- time ----------
    def day(self, ts: Optional[float] = None) -> str:
        return datetime.fromtimestamp(ts or time.time(), self.tz).strftime("%Y-%m-%d")

    def month(self, ts: Optional[float] = None) -> str:
        return datetime.fromtimestamp(ts or time.time(), self.tz).strftime("%Y-%m")

    def week_days(self, n: int = 7) -> list[str]:
        base = time.time()
        return [self.day(base - 86400 * i) for i in range(n - 1, -1, -1)]

    # ---------- concurrency ----------
    def key_sem(self, key_id: int, concurrency: int) -> asyncio.Semaphore:
        sem = self._key_sems.get(key_id)
        if sem is None:
            sem = asyncio.Semaphore(max(1, concurrency))
            self._key_sems[key_id] = sem
        return sem

    @property
    def global_sem(self) -> asyncio.Semaphore:
        return self._global_sem

    async def wait_for_key_image_slot(self, key_id: int) -> None:
        """普通用户 Key 的图片任务独立冷却，不占用全站并发槽。"""
        async with asyncio.timeout(self.settings.queue_timeout):
            while True:
                async with self._lock:
                    now = time.monotonic()
                    wait = max(0.0, self._key_image_next_at.get(key_id, 0.0) - now)
                    if not wait:
                        self._key_image_next_at[key_id] = now + max(
                            0.0, self.settings.key_image_min_interval)
                        return
                self._image_pacing_waiting += 1
                try:
                    await asyncio.sleep(wait)
                finally:
                    self._image_pacing_waiting -= 1

    def queue_snapshot(self) -> dict:
        """Aggregate visibility only; no identities, requests or token values."""
        now = time.monotonic()
        slots = [max(0.0, token.image_next_at - now)
                 for token in self.nai.pool if token.usable]
        return {
            "global": {
                "active": self.global_active,
                "waiting": self.global_waiting + self._image_pacing_waiting,
                "concurrency": sum(t.image_slots.limit for t in self.nai.pool if t.usable),
            },
            "image_next_slot_in": round(min(slots, default=0.0), 1),
            "image_cooldown_remaining": self.image_cooldown_remaining(),
            "queue_timeout": self.settings.queue_timeout,
            "image_min_interval": self.settings.image_min_interval,
        }

    async def wait_for_tag_request(self, key_id: int) -> bool:
        """Queue autocomplete without reserving future image slots or buffering bodies."""
        async with self._tag_condition:
            # Bound idle requests independently of the outer queue timeout.
            if self._tag_waiting >= max(16, self.settings.global_concurrency * 16):
                return False
            self._tag_waiting += 1
            try:
                while True:
                    now = time.monotonic()
                    delay = max(0.0, self._tag_next_at.get(key_id, 0) - now)
                    capacity = min(8, max(1, self.settings.global_concurrency))
                    if key_id not in self._tag_active and len(self._tag_active) < capacity:
                        if not delay:
                            self._tag_active.add(key_id)
                            self._tag_next_at[key_id] = now + max(
                                1.0, self.settings.key_image_min_interval)
                            return True
                        try:
                            await asyncio.wait_for(self._tag_condition.wait(), delay)
                        except TimeoutError:
                            pass
                    else:
                        await self._tag_condition.wait()
            finally:
                self._tag_waiting -= 1

    async def finish_tag_request(self, key_id: int) -> None:
        async with self._tag_condition:
            self._tag_active.discard(key_id)
            self._tag_condition.notify_all()

    # ---------- rpm ----------
    async def hit_rpm(self, key_id: int, rpm: int) -> bool:
        """True = 放行；False = 超出每分钟请求数。"""
        async with self._lock:
            win = self._rpm.setdefault(key_id, deque())
            now = time.time()
            while win and now - win[0] > 60:
                win.popleft()
            if len(win) >= max(1, rpm):
                return False
            win.append(now)
            return True

    async def hit_login(self, client_id: str) -> bool:
        """限制后台口令猜测；True = 放行。"""
        async with self._lock:
            win = self._login_attempts.setdefault(client_id, deque())
            now = time.time()
            window = max(1, self.settings.login_window_seconds)
            while win and now - win[0] > window:
                win.popleft()
            if len(win) >= max(1, self.settings.login_max_attempts):
                return False
            win.append(now)
            return True

    async def delete_inactive_keys(self) -> int:
        """永久回收长期未使用的虚拟 Key；0 天表示关闭。"""
        days = max(0, self.settings.key_inactivity_delete_days)
        if not days:
            return 0
        key_ids = await self.db.inactive_key_ids(time.time() - days * 86400)
        for key_id in key_ids:
            await self.db.delete_key(key_id)
        return len(key_ids)

    # ---------- upstream image cooldown ----------
    async def load_image_cooldown(self) -> None:
        raw = await self.db.get_setting("image_cooldown_until", 0)
        try:
            self._image_blocked_until = max(0.0, float(raw or 0))
        except (TypeError, ValueError):
            self._image_blocked_until = 0.0

    async def block_image_generation(self, retry_after: float) -> int:
        """暂停全站图片请求，并返回当前剩余冷却秒数。"""
        self._image_blocked_until = max(
            self._image_blocked_until, time.time() + max(5.0, retry_after)
        )
        await self.db.set_setting("image_cooldown_until", self._image_blocked_until)
        return max(1, int(self._image_blocked_until - time.time()))

    def image_cooldown_remaining(self) -> int:
        return max(0, int(self._image_blocked_until - time.time()))
