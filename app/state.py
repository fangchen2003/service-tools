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
        self._login_attempts: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()
        self._image_blocked_until = 0.0
        # Keep quota check, dispatch and successful accounting in one boundary.
        self.image_budget_lock = asyncio.Lock()
        self.global_waiting = 0
        self.global_active = 0
        self._image_pacing_waiting = 0

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
        interval = max(0.0, self.settings.key_image_min_interval)
        if not interval:
            return
        async with self._lock:
            now = time.monotonic()
            next_at = self._key_image_next_at.get(key_id, 0.0)
            wait = max(0.0, next_at - now)
            self._key_image_next_at[key_id] = max(now, next_at) + interval
        if wait:
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
                "concurrency": self.settings.global_concurrency,
            },
            "image_next_slot_in": round(min(slots, default=0.0), 1),
            "image_cooldown_remaining": self.image_cooldown_remaining(),
            "queue_timeout": self.settings.queue_timeout,
            "image_min_interval": self.settings.image_min_interval,
        }

    def try_tag_request(self, key_id: int) -> bool:
        """Fail-fast autocomplete admission; no awaits, no future image slots."""
        now = time.monotonic()
        if (key_id in self._tag_active
                or len(self._tag_active) >= min(8, max(1, self.settings.global_concurrency))
                or self._tag_next_at.get(key_id, 0) > now):
            return False
        self._tag_active.add(key_id)
        self._tag_next_at[key_id] = now + max(1.0, self.settings.key_image_min_interval)
        return True

    def finish_tag_request(self, key_id: int) -> None:
        self._tag_active.discard(key_id)

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
