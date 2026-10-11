"""运行期共享状态：限流器、并发闸门、时区工具。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from pathlib import Path

from . import alerts, token_store
from .config import Settings
from .database import Database
from .key_sources import SourceTracker
from .action_log import log_action
from .nai import NaiClient, clamp_retry_after
from .reconciliation import ManualReconciliation


RUNTIME_LIMIT_BOUNDS = {
    "queue_timeout": (15, 300),
    "key_image_min_interval": (15, 120),
    "image_min_interval": (15, 120),
    "image_429_cooldown_seconds": (60, 3600),
}


def _mask_ip(value: str) -> str:
    parts = value.split(".")
    return ".".join(["*"] * (len(parts) - 2) + parts[-2:]) if len(parts) == 4 else "…" + value[-6:]


TAG_MIN_INTERVAL = 2.0     # 同一把 Key 两次标签补全至少间隔 2 秒（补全很轻，旧查询会被新查询取代）


class GateState:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(str(settings.db_path), settings.tz)
        self.tz = ZoneInfo(settings.tz)
        # 后台“接管”过上游 Token 后，令牌池以 data/upstream_tokens.json 为准；否则使用 .env 里的 NAI_TOKENS。
        self.token_store_path = Path(settings.data_dir) / "upstream_tokens.json"
        managed = token_store.load(self.token_store_path)
        self.nai = NaiClient(
            tokens=[e["token"] for e in managed] if managed else settings.nai_tokens,
            image_host=settings.image_host,
            text_host=settings.text_host,
            legacy_text_host=settings.text_host_legacy,
            db=self.db,
            day_fn=self.day,
            v5_daily_limits=[] if managed else settings.nai_token_v5_daily_limits,
            allow_anlas=[e["allow_anlas"] for e in managed] if managed else settings.nai_token_allow_anlas,
            image_min_interval=settings.image_min_interval,
            proxy=settings.upstream_proxy,
            custom_user_agent=settings.upstream_user_agent,
            http2=settings.upstream_http2,
            post_jitter_min=settings.post_request_jitter_min,
            post_jitter_max=settings.post_request_jitter_max,
            single_slot_enforced=settings.single_image_slot_enforced,
        )
        self.nai.managed_path = self.token_store_path
        from .guard import Guard
        self.guard = Guard(self.db, settings.tz)
        self.nai.guard = self.guard
        self.upstream_managed = bool(managed)
        self.alerter = alerts.from_settings(settings)
        self.sources = SourceTracker(self.db, self.alerter, threshold=settings.key_share_alert_nets)
        from .share_guard import ShareGuard
        self.share = ShareGuard(self.db)
        from .scheduling import FairScheduler
        self.sched = FairScheduler()
        from .errors import Tracker
        self.bugs = Tracker(self.db, notify=lambda kind, msg, cooldown: self.alerter.notify(kind, msg, cooldown=cooldown))
        self.announcer = alerts.announcer_from_settings(settings)
        self.nai.on_event = lambda kind, msg, cooldown=900: self.alerter.notify(kind, msg, cooldown=cooldown)
        self.nai.allowance.on_low = self.nai.on_event
        self._upstream_events: deque[tuple[float, bool]] = deque(maxlen=40)
        self._upstream_degraded = False
        self._auth_fails: dict[str, deque[float]] = {}
        self._auth_blocked_until: dict[str, float] = {}
        self._global_sem = asyncio.Semaphore(max(1, settings.global_concurrency))
        self._key_sems: dict[int, asyncio.Semaphore] = {}
        self._key_image_next_at: dict[int, float] = {}
        self._key_image_taken: dict[int, tuple[float, float]] = {}
        self._rpm: dict[int, deque[float]] = {}
        self._tag_active: set[int] = set()
        self._tag_next_at: dict[int, float] = {}
        self._tag_condition = asyncio.Condition()
        self._tag_waiting = 0
        self._tag_waiting_by_key: dict[int, int] = {}
        self._tag_latest: dict[int, int] = {}       # 每把 Key 最新一次补全查询的编号：新查询到来时，旧的排队查询直接作废
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
                        prev = self._key_image_next_at.get(key_id, 0.0)
                        new = now + max(0.0, self.settings.key_image_min_interval)
                        self._key_image_next_at[key_id] = new
                        self._key_image_taken[key_id] = (prev, new)
                        if len(self._key_image_next_at) > 1024:
                            for k in [k for k, v in self._key_image_next_at.items() if v <= now]:
                                self._key_image_next_at.pop(k, None)
                                self._key_image_taken.pop(k, None)
                        return
                self._image_pacing_waiting += 1
                try:
                    await asyncio.sleep(wait)
                finally:
                    self._image_pacing_waiting -= 1

    async def refund_key_image_slot(self, key_id: int) -> None:
        """请求在到达上游前被拒绝（如额度不足）时归还冷却占用；已被后续请求覆盖则不动。"""
        async with self._lock:
            taken = self._key_image_taken.pop(key_id, None)
            if taken and self._key_image_next_at.get(key_id) == taken[1]:
                self._key_image_next_at[key_id] = taken[0]

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

    async def wait_for_tag_request(self, key_id: int) -> Optional[bool]:
        """标签补全排队。客户端每输入一个字就会查一次补全，只有最新那次有意义：
        新查询到来时，同一把 Key 还在排队的旧查询作废（返回 None，回空结果，不算拒绝）。
        True = 放行；False = 全站排队已满；None = 被更新的查询取代。"""
        async with self._tag_condition:
            # Bound idle requests independently of the outer queue timeout.
            if self._tag_waiting >= max(16, self.settings.global_concurrency * 16):
                return False
            ticket = self._tag_latest.get(key_id, 0) + 1
            self._tag_latest[key_id] = ticket
            self._tag_condition.notify_all()           # 叫醒同一 Key 的旧查询，让它们发现自己已作废
            self._tag_waiting += 1
            self._tag_waiting_by_key[key_id] = self._tag_waiting_by_key.get(key_id, 0) + 1
            try:
                while True:
                    if self._tag_latest.get(key_id) != ticket:
                        return None
                    now = time.monotonic()
                    delay = max(0.0, self._tag_next_at.get(key_id, 0) - now)
                    capacity = min(8, max(1, self.settings.global_concurrency))
                    if key_id not in self._tag_active and len(self._tag_active) < capacity:
                        if not delay:
                            self._tag_active.add(key_id)
                            self._tag_next_at[key_id] = now + min(TAG_MIN_INTERVAL, self.settings.key_image_min_interval)
                            return True
                        try:
                            await asyncio.wait_for(self._tag_condition.wait(), delay)
                        except TimeoutError:
                            pass
                    else:
                        await self._tag_condition.wait()
            finally:
                self._tag_waiting -= 1
                left = self._tag_waiting_by_key.get(key_id, 1) - 1
                if left > 0:
                    self._tag_waiting_by_key[key_id] = left
                else:
                    self._tag_waiting_by_key.pop(key_id, None)

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
            if len(self._rpm) > 1024:
                for k in [k for k, w in self._rpm.items() if not w or now - w[-1] > 60]:
                    if k != key_id:
                        self._rpm.pop(k, None)
            return True

    def record_upstream(self, ok: bool) -> None:
        """记录一次上游调用结果；近 10 分钟内失败率过高时标记为"不稳定"并告警，恢复后再通知。"""
        now = time.time()
        self._upstream_events.append((now, ok))
        status = self.upstream_health()["status"]
        if status == "degraded" and not self._upstream_degraded:
            self._upstream_degraded = True
            self.alerter.notify("upstream_degraded", "NovelAI 上游近 10 分钟失败率偏高（上游可能在故障），"
                                "成员的生图会受影响。", cooldown=1800)
        elif status == "ok" and self._upstream_degraded:
            self._upstream_degraded = False
            self.alerter.notify("upstream_recovered", "NovelAI 上游已恢复正常。", cooldown=0)

    def upstream_health(self) -> dict:
        cutoff = time.time() - 600
        recent = [ok for ts, ok in self._upstream_events if ts >= cutoff]
        failed = recent.count(False)
        if len(recent) >= 4 and failed / len(recent) >= 0.5:
            status = "degraded"
        elif not recent:
            status = "idle"
        else:
            status = "ok"
        return {"status": status, "recent": len(recent), "failed": failed,
                "image_cooldown_seconds": self.image_cooldown_remaining()}

    def auth_blocked(self, client_id: str) -> int:
        """该 IP 因多次无效 Key 被临时拦截时，返回剩余秒数；否则 0。"""
        until = self._auth_blocked_until.get(client_id, 0.0)
        return max(0, int(until - time.time()))

    def record_auth_failure(self, client_id: str) -> None:
        """记录一次无效 Key；超过阈值则临时拦截该 IP，并告警。"""
        now = time.time()
        window = max(1, self.settings.auth_fail_window)
        win = self._auth_fails.setdefault(client_id, deque())
        win.append(now)
        while win and now - win[0] > window:
            win.popleft()
        if len(win) >= max(1, self.settings.auth_fail_max):
            self._auth_blocked_until[client_id] = now + max(1, self.settings.auth_block_seconds)
            win.clear()
            self.alerter.notify(
                "auth_flood", f"有 IP 在 {window} 秒内用无效 Key 反复请求 {self.settings.auth_fail_max} 次，"
                f"已临时拦截 {self.settings.auth_block_seconds // 60} 分钟（IP 后两段：{_mask_ip(client_id)}）。",
                cooldown=1800)
        if len(self._auth_fails) > 2048:
            for k in [k for k, w in self._auth_fails.items() if not w or now - w[-1] > window]:
                self._auth_fails.pop(k, None)
            for k in [k for k, t in self._auth_blocked_until.items() if t < now]:
                self._auth_blocked_until.pop(k, None)

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
            if len(self._login_attempts) > 2048:
                for k in [k for k, w in self._login_attempts.items() if not w or now - w[-1] > window]:
                    self._login_attempts.pop(k, None)
            return True

    async def remind_idle_keys(self, send_dm, site: str) -> int:
        """闲置回收前 24 小时私信提醒（附配置方法）；同一段闲置只提醒一次，之后有活动会重新计时。"""
        days = max(0, self.settings.key_inactivity_delete_days)
        if days < 1 or send_dm is None:
            return 0
        due = await self.db.keys_due_for_idle_reminder(time.time() - max(days - 1, 0.5) * 86400)
        sent = 0
        for row in due[:3]:      # 每轮（每小时）最多处理 3 个，避免同一批注册的人在同一小时集中收到私信
            if row["ever_used"]:
                text = (f"🦉 猫头鹰公益站提醒：你的 Key 已经有一段时间没有使用了，再过约 24 小时仍没有请求就会自动回收，"
                        f"名额会让给其他人。随便生成一张图即可重新计时；回收后有名额时可以再用 /register 领取。")
            else:
                text = (f"🦉 猫头鹰公益站提醒：你领取的 Key 还没有成功生成过图片。领取后连续 {days} 天没有使用会自动回收，"
                        f"你的 Key 还剩约 24 小时。\n"
                        f"配置方法（以柏宝绘为例）：渠道 → 配置 → 新建接入点，接口地址填 `{site}`，API Key 填私信里 `nai-` 开头的 Key。"
                        f"可以先在 {site} 首页粘贴 Key 点「用 Key 登录」测试。\n"
                        f"遇到问题可在 🛠️｜问题反馈 发截图；Key 丢了用 /resetkey 重新获取。")
            ok = await send_dm(row["discord_id"], text)
            blocked = getattr(getattr(send_dm, "__self__", None), "last_dm_block", "") if not ok else ""
            await self.db.mark_idle_reminded(row["key_id"], row["activity"])
            await log_action(self.db, "系统", "闲置回收前提醒", f"Key #{row['key_id']} {row['name']}",
                             ("从未使用；" if not row["ever_used"] else "")
                             + ("已私信" if ok else f"未私信：{blocked}" if blocked else "私信失败（对方可能关闭了私信）"),
                             ok=ok or bool(blocked))
            sent += int(ok)
        return sent

    async def delete_inactive_keys(self) -> int:
        """永久回收长期未使用的虚拟 Key；0 天表示关闭。"""
        days = max(0, self.settings.key_inactivity_delete_days)
        if not days:
            return 0
        key_ids = await self.db.inactive_key_ids(time.time() - days * 86400)
        for key_id in key_ids:
            key = await self.db.get_key(key_id)
            await self.db.delete_key(key_id)
            await log_action(self.db, "系统", "闲置回收 Key", f"Key #{key_id} {key['name'] if key else ''}",
                             f"连续 {days} 天没有任何请求")
            # 释放对应的 Discord 领取记录，否则用户永远无法重新领取。
            for discord_id in await self.db.forget_registration_for_key(key_id):
                release = getattr(self, "on_registration_released", None)
                if release is not None:
                    await release(discord_id)
        return len(key_ids)

    # ---------- upstream image cooldown ----------
    async def load_image_cooldown(self) -> None:
        raw = await self.db.get_setting("image_cooldown_until", 0)
        try:
            value = float(raw or 0)
            # 历史上可能持久化过 inf / 超大值：最多保留 1 小时冷却，否则视为无效。
            if not (value == value) or value > time.time() + 3600:
                value = 0.0
            self._image_blocked_until = max(0.0, value)
        except (TypeError, ValueError):
            self._image_blocked_until = 0.0

    async def block_image_generation(self, retry_after: float) -> int:
        """暂停全站图片请求，并返回当前剩余冷却秒数。"""
        self._image_blocked_until = max(
            self._image_blocked_until, time.time() + clamp_retry_after(retry_after)
        )
        await self.db.set_setting("image_cooldown_until", self._image_blocked_until)
        remaining = max(1, int(self._image_blocked_until - time.time()))
        guard = getattr(self, "guard", None)
        if guard is not None:
            if guard.note_upstream_429() >= 3:
                fell = await guard.fall_back_daily("上游 1 小时内 ≥3 次 429")
                if fell:
                    self.alerter.notify("daily_cap_fallback", f"⚠️ NovelAI 1 小时内多次返回 429：每日出图上限自动从 {fell[0]} 退回 "
                                        f"{fell[1]} 张。确认安全后需要站长手动再调高。", cooldown=0)
            changed = await guard.on_upstream_429()
            if changed:
                self.alerter.notify("guard_hourly_down", f"上游限流：每小时出图上限自动从 {changed[0]} 降到 {changed[1]}，"
                                    "之后连续一天没有限流会每天 +10。", cooldown=0)
        self.alerter.notify("upstream_429", f"NovelAI 对图片请求返回 429（限流），全站生图已暂停 {remaining} 秒。"
                            "如果频繁出现，请降低成员人数或调大图片间隔。", cooldown=1800)
        return remaining

    def image_cooldown_remaining(self) -> int:
        return max(0, int(self._image_blocked_until - time.time()))
