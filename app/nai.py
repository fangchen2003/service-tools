"""NovelAI 上游客户端：令牌池、429 退避、SSE 透传。"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import anyio
import httpx

from . import request_timing, token_store
from .policy import mask_token
from .allowance import AllowanceCache, AllowanceUnavailable
from .concurrency import AdjustableLimiter
from .image_tools import validate_result


# 真实最新的主流桌面浏览器指纹池，完全模拟合法网页端请求
BROWSER_PROFILES: list[dict[str, str]] = [
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
    },
]


def default_browser_headers(token: Optional[str] = None, accept: str = "application/json",
                            profile: Optional[dict[str, str]] = None) -> dict[str, str]:
    p = profile or BROWSER_PROFILES[0]
    headers: dict[str, str] = {
        "Accept": accept,
        "User-Agent": p["user_agent"],
        "Origin": "https://novelai.net",
        "Referer": "https://novelai.net/",
        "Sec-Ch-Ua": p["sec_ch_ua"],
        "Sec-Ch-Ua-Mobile": p["sec_ch_ua_mobile"],
        "Sec-Ch-Ua-Platform": p["sec_ch_ua_platform"],
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Accept-Language": "en-US,en;q=0.9",
        "Priority": "u=1, i",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers



class UpstreamError(Exception):
    def __init__(self, status: int, message: str, *, billing_uncertain: bool = False):
        super().__init__(message)
        self.status = status
        self.message = message
        self.billing_uncertain = billing_uncertain


@dataclass
class ImageStreamHandle:
    response: httpx.Response
    completed_images: int = 0


class TokenState:
    __slots__ = (
        "token", "token_id", "position", "v5_daily_limit", "allow_anlas", "pending_v5",
        "admin_enabled",
        "dispatch_lock", "image_slots",
        "fails", "blocked_until", "disabled", "last_ok", "image_next_at",
        "browser_profile", "forbidden_streak", "forbidden_trips",
    )

    def __init__(self, token: str, index: int, v5_daily_limit: int,
                 allow_anlas: bool, custom_user_agent: Optional[str] = None):
        self.token = token
        # 永不把原始上游 Token 写入数据库；只存不可逆的短哈希标识。
        self.token_id = "token-" + hashlib.sha256(token.encode()).hexdigest()[:16]
        self.position = index + 1
        self.v5_daily_limit = max(0, v5_daily_limit)
        self.allow_anlas = allow_anlas
        self.admin_enabled = True
        self.dispatch_lock = asyncio.Lock()
        self.image_slots = AdjustableLimiter(1)
        self.pending_v5 = 0
        self.fails = 0
        self.blocked_until = 0.0
        self.disabled = False
        self.last_ok = 0.0
        self.image_next_at = 0.0
        self.forbidden_streak = 0          # 连续 403 次数（和 429 分开计）
        self.forbidden_trips = 0           # 冷却触发了几轮（成功一次清零）→ 5 / 15 / 45 / 60 分钟退避
        profile = BROWSER_PROFILES[index % len(BROWSER_PROFILES)].copy()
        if custom_user_agent:
            profile["user_agent"] = custom_user_agent
        self.browser_profile = profile

    @property
    def usable(self) -> bool:
        return self.admin_enabled and not self.disabled and time.time() >= self.blocked_until


RETRY_AFTER_MIN = 5.0
RETRY_AFTER_MAX = 3600.0


def clamp_retry_after(value: Any) -> float:
    """任何来源的冷却秒数都钳制到 5..3600 的有限值（inf/NaN/超大值不能造成永久停摆）。"""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 20.0
    if seconds != seconds:          # NaN
        return 20.0
    return min(RETRY_AFTER_MAX, max(RETRY_AFTER_MIN, seconds))


def parse_retry_after(header: Optional[str]) -> float:
    """只接受十进制秒数；HTTP 日期等其它格式按默认 20 秒处理。"""
    value = (header or "").strip()
    if not value or len(value) > 10 or not value.isascii() or not value.isdigit():
        return 20.0
    return clamp_retry_after(int(value))


async def _wait_cleanup(task: asyncio.Task) -> Any:
    """Finish accounting under ASGI cancel scopes and direct task cancellation."""
    cancelled = False
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        result = task.result()
    if cancelled:
        raise asyncio.CancelledError()
    return result


class NaiClient:
    """持有共享 httpx.AsyncClient 与上游令牌池。"""

    def _event(self, kind: str, message: str, cooldown: float = 900) -> None:
        callback = getattr(self, "on_event", None)
        if callback:
            callback(kind, message, cooldown)

    def __init__(self, tokens: list[str], image_host: str, text_host: str,
                 legacy_text_host: str, *, db: Any, day_fn: Callable[[], str],
                 v5_daily_limits: list[int], allow_anlas: list[bool],
                 image_min_interval: float = 15,
                 proxy: Optional[str] = None,
                 custom_user_agent: Optional[str] = None,
                 http2: bool = True,
                 post_jitter_min: float = 1.0,
                 post_jitter_max: float = 3.0,
                 single_slot_enforced: bool = False):
        self.image_host = image_host.rstrip("/")
        self.text_host = text_host.rstrip("/")
        self.legacy_text_host = legacy_text_host.rstrip("/")
        self._proxy = proxy or None
        self._custom_user_agent = custom_user_agent or None
        self._http2 = bool(http2)
        self._post_jitter_min = max(0.0, post_jitter_min)
        self._post_jitter_max = max(self._post_jitter_min, post_jitter_max)
        self._single_slot_enforced = bool(single_slot_enforced)
        self.pool = [
            TokenState(
                token,
                index,
                v5_daily_limits[index] if index < len(v5_daily_limits) else 0,
                allow_anlas[index] if index < len(allow_anlas) else True,
                custom_user_agent=self._custom_user_agent,
            )
            for index, token in enumerate(tokens)
        ]
        self._db = db
        self._day_fn = day_fn
        self._image_min_interval = max(0.0, image_min_interval)
        self._rr = 0
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()
        self.allowance = AllowanceCache(db)
        self.guard = None          # app.guard.Guard：账号每日 / 每小时上限、安静时段、间隔抖动

    async def start(self) -> None:
        client_kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(connect=15, read=300, write=120, pool=300),
            "limits": httpx.Limits(max_connections=32, max_keepalive_connections=8),
            "follow_redirects": True,
        }
        if self._proxy:
            client_kwargs["proxy"] = self._proxy
        if self._http2:
            try:
                import h2  # noqa: F401
                client_kwargs["http2"] = True
            except ImportError:
                pass
        self._client = httpx.AsyncClient(**client_kwargs)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    # ---------------- token pool ----------------
    # ---------------- 后台增删改令牌（持久化到 token_store 文件，不进数据库）----------------
    def _persist_pool(self) -> None:
        path = getattr(self, "managed_path", None)
        if path is not None:
            token_store.save(path, [{"token": t.token, "allow_anlas": t.allow_anlas} for t in self.pool])

    def _renumber(self, pool: list) -> list:
        for index, token in enumerate(pool):
            token.position = index + 1
        return pool

    async def verify_token(self, token: str) -> dict:
        """向上游查询订阅信息来确认 Token 有效（只读，不消耗额度，不记录 Token）。"""
        if self._client is None:
            return {"ok": False, "error": "服务尚未就绪"}
        try:
            response = await self._client.get(f"{self.image_host}/user/subscription",
                                              headers=default_browser_headers(token, accept="application/json"),
                                              timeout=12)
        except httpx.HTTPError:
            return {"ok": False, "error": "无法连接 NovelAI 验证这把 Token，请稍后再试"}
        if response.status_code in (401, 403):
            return {"ok": False, "error": "NovelAI 拒绝了这把 Token（无效，或已被重置）"}
        if response.status_code != 200:
            return {"ok": False, "error": f"NovelAI 返回 {response.status_code}，暂时无法验证"}
        try:
            info = response.json()
            tier, active = int(info.get("tier", 0)), bool(info.get("active"))
        except (ValueError, TypeError, AttributeError):
            return {"ok": False, "error": "NovelAI 返回了无法识别的订阅信息"}
        if not active:
            return {"ok": False, "error": "这把 Token 对应的账号没有有效订阅"}
        return {"ok": True, "tier": tier}

    async def add_token(self, token: str, allow_anlas: bool = False) -> TokenState:
        async with self._lock:
            new = TokenState(token, len(self.pool), 0, allow_anlas, custom_user_agent=self._custom_user_agent)
            if any(t.token_id == new.token_id for t in self.pool):
                raise ValueError("这把 Token 已经在令牌池里")
            saved = await self._db.get_upstream_token_limits()
            enabled = await self._db.get_upstream_token_enabled()
            if new.token_id in saved:
                new.v5_daily_limit = saved[new.token_id]
            if new.token_id in enabled:
                new.admin_enabled = enabled[new.token_id]
            new.image_slots.resize(1)
            self.pool = self._renumber([*self.pool, new])
            self._persist_pool()
            return new

    async def replace_token(self, token_id: str, token: str) -> TokenState:
        """用新 Token 替换某个槽位（典型场景：在 NovelAI 重置了 Token）。设置与当天计数延续。"""
        async with self._lock:
            index = next((i for i, t in enumerate(self.pool) if t.token_id == token_id), None)
            if index is None:
                raise LookupError("令牌不存在")
            old = self.pool[index]
            new = TokenState(token, index, old.v5_daily_limit, old.allow_anlas, custom_user_agent=self._custom_user_agent)
            if new.token_id != old.token_id and any(t.token_id == new.token_id for t in self.pool):
                raise ValueError("这把 Token 已经在令牌池的另一个位置")
            new.admin_enabled = old.admin_enabled
            new.image_slots.resize(1 if self._single_slot_enforced else old.image_slots.limit)
            await self._db.move_upstream_token(old.token_id, new.token_id)
            pool = list(self.pool)
            pool[index] = new
            self.pool = self._renumber(pool)
            self._persist_pool()
            return new

    async def remove_token(self, token_id: str) -> bool:
        async with self._lock:
            if len(self.pool) <= 1:
                raise ValueError("至少需要保留一把上游 Token")
            remaining = [t for t in self.pool if t.token_id != token_id]
            if len(remaining) == len(self.pool):
                return False
            self.pool = self._renumber(remaining)
            self._persist_pool()
            return True

    async def load_saved_limits(self) -> None:
        saved = await self._db.get_upstream_token_limits()
        enabled = await self._db.get_upstream_token_enabled()
        image_concurrency = (await self._db.get_upstream_token_image_concurrency()
                             if hasattr(self._db, "get_upstream_token_image_concurrency") else {})
        for token in self.pool:
            if token.token_id in saved:
                token.v5_daily_limit = saved[token.token_id]
            if token.token_id in enabled:
                token.admin_enabled = enabled[token.token_id]
            if token.token_id in image_concurrency:
                eff_limit = 1 if self._single_slot_enforced else min(4, max(1, image_concurrency[token.token_id]))
                token.image_slots.resize(eff_limit)

    async def set_v5_daily_limit(self, token_id: str, limit: int) -> bool:
        async with self._lock:
            token = next((item for item in self.pool if item.token_id == token_id), None)
            if token is None:
                return False
            await self._db.set_upstream_token_limit(token_id, limit)
            token.v5_daily_limit = limit
            return True

    async def set_admin_enabled(self, token_id: str, enabled: bool) -> bool:
        token = next((item for item in self.pool if item.token_id == token_id), None)
        if token is None:
            return False
        # The toggle commits after any already-dispatched request. Once the
        # admin API returns, later sends must observe the new state.
        async with token.dispatch_lock:
            async with self._lock:
                await self._db.set_upstream_token_enabled(token_id, enabled)
                token.admin_enabled = enabled
        if not enabled:
            await token.image_slots.wait_idle()
        return True

    async def set_image_concurrency(self, token_id: str, limit: int) -> bool:
        async with self._lock:
            token = next((item for item in self.pool if item.token_id == token_id), None)
            if token is None:
                return False
            eff_limit = 1 if self._single_slot_enforced else min(4, max(1, limit))
            await self._db.set_upstream_token_image_concurrency(token_id, eff_limit)
            token.image_slots.resize(eff_limit)
            return True

    async def pick_token(self, *, requires_anlas: bool = False,
                         v5_free: bool = False, image_job: bool = False) -> Optional[TokenState]:
        """选取符合该图片费用策略的令牌；V5 限额在这里原子预留。

        image_job：出图类任务还要经过账号保护（每日 / 每小时上限、安静时段）；全部账号都被挡住时返回 429 和原因。
        """
        async with self._lock:
            usable = [t for t in self.pool if t.usable and
                      (not requires_anlas or t.allow_anlas)]
            if not usable:
                return None
            if image_job and self.guard is not None:
                reasons = []
                open_tokens = []
                for t in usable:
                    reason = await self.guard.token_block_reason(self._db, t.token_id, self._day_fn())
                    (reasons.append(reason) if reason else open_tokens.append(t))
                if not open_tokens:
                    raise UpstreamError(429, reasons[0])
                usable = open_tokens

            # 保持轮询，同时跳过当日 V5 已用完的特定上游 Token。
            start = (self._rr + 1) % len(usable)
            chosen: Optional[TokenState] = None
            for offset in range(len(usable)):
                candidate = usable[(start + offset) % len(usable)]
                if v5_free and candidate.v5_daily_limit:
                    used = await self._db.get_upstream_v5_counter(
                        candidate.token_id, self._day_fn()
                    )
                    if used + candidate.pending_v5 >= candidate.v5_daily_limit:
                        continue
                chosen = candidate
                break
            if chosen is None:
                return None

            self._rr = usable.index(chosen)
            if v5_free:
                chosen.pending_v5 += 1
            return chosen

    async def finish_v5_reservation(self, ts: TokenState, *, succeeded: bool,
                                    v5_free: bool) -> None:
        """只把成功完成的免费 V5 图计入特定上游令牌的日额度。"""
        if not v5_free:
            return
        async with self._lock:
            try:
                if succeeded:
                    await self._db.bump_upstream_v5_counter(ts.token_id, self._day_fn())
            finally:
                ts.pending_v5 = max(0, ts.pending_v5 - 1)

    async def record_successful_images(self, ts: TokenState, image_count: int) -> None:
        """按上游实际成功响应记录生成张数；失败、拒绝和限流不计入。每张都按 1 张计（10-11 起不再折算）。"""
        if image_count > 0:
            await self._db.bump_upstream_image_counter(ts.token_id, self._day_fn(), image_count)

    async def wait_for_token_image_slot(self, ts: TokenState) -> None:
        """每把上游 Token 各自保持图片请求间隔，不与其他 Token 共享计时。"""
        await self.wait_for_available_token_image_slot(ts)

    async def wait_for_available_token_image_slot(self, ts: TokenState) -> None:
        """补全查询等到当前空档才占位，取消等待不会挤占后续生图。"""
        while True:
            async with self._lock:
                now = time.monotonic()
                wait = max(0.0, ts.image_next_at - now)
                if not wait:
                    jitter = self.guard.jitter() if self.guard is not None else 0.0
                    ts.image_next_at = now + self._image_min_interval + jitter
                    return
            await asyncio.sleep(wait)

    async def set_image_min_interval(self, seconds: float) -> None:
        """Apply a safer increased interval to already cooling tokens too."""
        async with self._lock:
            now = time.monotonic()
            if seconds > self._image_min_interval:
                for token in self.pool:
                    if token.image_next_at > now:
                        token.image_next_at = max(token.image_next_at, now + seconds)
            self._image_min_interval = seconds

    def mark_rate_limited(self, ts: TokenState, retry_after: float = 20.0) -> None:
        ts.blocked_until = time.time() + clamp_retry_after(retry_after)
        ts.fails += 1

    def mark_forbidden(self, ts: TokenState) -> None:
        """403 代表账号受限、Cloudflare 盾拦截或上游 WAF 封禁。连续 3 次（中间没有成功）就冷却并告警。

        不设 disabled：只有一把 Token 时永久停用等于全站停摆，且只能重启恢复。
        冷却按轮次指数退避（5 → 15 → 45 → 60 分钟），被封期间不会每 5 分钟去试一次；403 和 429 分开计数。
        """
        ts.fails += 1
        ts.forbidden_streak += 1
        if ts.forbidden_streak >= 3:
            minutes = min(60, 5 * 3 ** ts.forbidden_trips)
            ts.forbidden_trips += 1
            ts.forbidden_streak = 0
            ts.blocked_until = max(ts.blocked_until, time.time() + minutes * 60.0)
            self._event("upstream_403_cooldown", f"NovelAI 连续返回 403，账号可能被上游封控。该 Token（{ts.token_id}）冷却 {minutes} 分钟后自动重试（第 {ts.forbidden_trips} 轮），请检查上游账号状态。", 1800)
            if self.guard is not None:          # 每日上限试探期：封控信号 → 退回安全档
                import asyncio as _asyncio
                async def _fall():
                    fell = await self.guard.fall_back_daily("上游连续 403（账号可能被封控）")
                    if fell:
                        self._event("daily_cap_fallback", f"⚠️ NovelAI 连续返回 403：每日出图上限自动从 {fell[0]} 退回 {fell[1]} 张，确认安全后需要站长手动再调高。", 0)
                try:
                    task = _asyncio.get_running_loop().create_task(_fall())
                    self._bg = getattr(self, "_bg", set()); self._bg.add(task); task.add_done_callback(self._bg.discard)
                except RuntimeError:
                    pass

    def _warn_account(self, status: int) -> None:
        """402 / 403 状态告警。"""
        if status in (402, 403):
            reason = "需要付费 / 订阅或 Anlas 不足" if status == 402 else "拒绝访问，账号可能受限"
            self._event(f"upstream_{status}", f"NovelAI 返回 {status}（{reason}），请检查上游账号状态。", 1800)

    def mark_unauthorized(self, ts: TokenState) -> None:
        ts.disabled = True

    def mark_ok(self, ts: TokenState) -> None:
        ts.fails = 0
        ts.forbidden_streak = 0
        ts.forbidden_trips = 0
        ts.last_ok = time.time()

    @property
    def configured(self) -> bool:
        return bool(self.pool)

    async def status(self) -> list[dict[str, Any]]:
        now = time.time()
        result = []
        for t in self.pool:
            counter = await self._db.get_upstream_counter(t.token_id, self._day_fn())
            result.append({
                "token_id": t.token_id,
                "position": t.position,
                "token": mask_token(t.token),
                "usable": t.usable,
                "admin_enabled": t.admin_enabled,
                "disabled": t.disabled,
                "fails": t.fails,
                "blocked_for": max(0, int(t.blocked_until - now)),
                "last_ok": t.last_ok,
                "allow_anlas": t.allow_anlas,
                "image_concurrency": t.image_slots.limit,
                "image_active": t.image_slots.active,
                "v5_daily_limit": t.v5_daily_limit,
                "v5_used": counter["v5"],
                "images_today": counter["images"],
            })
        return result

    # ---------------- requests ----------------
    def _headers(self, ts: TokenState, accept: str = "*/*") -> dict[str, str]:
        profile = getattr(ts, "browser_profile", None) or BROWSER_PROFILES[0]
        headers = default_browser_headers(ts.token, accept=accept, profile=profile)
        headers["Content-Type"] = "application/json"
        return headers

    def _unavailable(self, requires_anlas: bool, v5_free: bool) -> UpstreamError:
        # 先区分「账号暂时不可用（冷却 / 停用）」和「额度真的用完」，避免把冷却误报成 V5 额度已用完。
        usable = [t for t in self.pool if t.usable]
        if not usable:
            cooling = [t for t in self.pool if t.admin_enabled and not t.disabled and time.time() < t.blocked_until]
            if cooling:
                wait = max(1, int(min(t.blocked_until for t in cooling) - time.time()))
                return UpstreamError(503, f"上游账号暂时限流冷却中，约 {wait} 秒后恢复（不是额度用完），请稍后再试")
            return UpstreamError(503, "上游账号暂时不可用（不是额度用完），请稍后再试")
        if requires_anlas:
            return UpstreamError(503, "没有允许使用 Anlas 的上游令牌，无法生成此图片")
        if v5_free:
            return UpstreamError(429, "可用上游令牌的今日 V5 免费图片额度已用完")
        return UpstreamError(503, "上游令牌全部被限流或不可用，请稍后再试")

    async def _settle(self, ts: TokenState, *, succeeded: bool,
                      v5_free: bool, image_count: int, send_started: bool = False) -> None:
        await self.finish_v5_reservation(ts, succeeded=succeeded, v5_free=v5_free)
        if succeeded:
            await self.record_successful_images(ts, image_count)
        # 拟人化后置静默冷却：仅对实际发往上游且有正常间隔策略（>0.5s）的请求生效，测试环境（<=0.5s）自动跳过
        if send_started and self._image_min_interval > 0.5 and self._post_jitter_max > 0:
            now = time.monotonic()
            jitter_max = min(self._post_jitter_max, self._image_min_interval)
            jitter_min = min(self._post_jitter_min, jitter_max)
            post_delay = random.uniform(jitter_min, jitter_max) if jitter_max > 0 else 0.0
            if post_delay > 0:
                async with self._lock:
                    ts.image_next_at = max(ts.image_next_at, now + post_delay)

    async def _rate_limit(self, ts: TokenState, resp: httpx.Response,
                          callback: Optional[Callable[[float], Awaitable[None]]]) -> None:
        retry_after = parse_retry_after(resp.headers.get("retry-after"))
        self.mark_rate_limited(ts, retry_after)
        if callback:
            await callback(retry_after)

    async def request(
        self, method: str, url: str, json_body: Any = None,
        accept: str = "*/*",
        on_rate_limited: Optional[Callable[[float], Awaitable[None]]] = None,
        *, requires_anlas: bool = False, v5_free: bool = False,
        image_count: int = 0, image_lane: bool = False, wait_for_image_slot: bool = True,
        resolve_v5_cost: Optional[Callable[[bool], Awaitable[None]]] = None,
        max_response_bytes: int | None = None,
        queue_timeout: float = 90,
        before_dispatch: Optional[Callable[[], None]] = None,
    ) -> httpx.Response:
        """图片请求不自动重试；普通请求只对明确的 429 换 token 一次。"""
        if self._client is None:
            raise RuntimeError("client not started")
        attempts = 0
        while attempts < 2:
            attempts += 1
            ts = await self.pick_token(requires_anlas=requires_anlas, v5_free=v5_free,
                                       image_job=image_lane and wait_for_image_slot)
            if ts is None:
                raise self._unavailable(requires_anlas, v5_free)
            succeeded = False
            send_started = False
            response_status = None
            slot_acquired = False
            try:
                if image_lane:
                    try:
                        async with asyncio.timeout(queue_timeout):
                            await ts.image_slots.acquire()
                            slot_acquired = True
                            if wait_for_image_slot:
                                await self.wait_for_token_image_slot(ts)
                            else:
                                await self.wait_for_available_token_image_slot(ts)
                    except TimeoutError:
                        raise UpstreamError(429, "上游图片任务排队超时，请稍后再试") from None
                if v5_free and resolve_v5_cost is not None:
                    exhausted = await self._resolve_v5_cost(ts, resolve_v5_cost)
                    if exhausted:
                        await self.finish_v5_reservation(ts, succeeded=False, v5_free=True)
                        v5_free = False
                        requires_anlas = True
                async with self._dispatch_guard(ts, image_lane):
                    if not ts.admin_enabled:
                        continue
                    if before_dispatch is not None:
                        before_dispatch()
                    send_started = True
                    request_timing.mark_sent()
                    if image_lane and wait_for_image_slot and self.guard is not None:
                        self.guard.record_start(ts.token_id)
                    if max_response_bytes is None:
                        resp = await self._client.request(
                            method, url, json=json_body, headers=self._headers(ts, accept))
                    else:
                        async with asyncio.timeout(300):
                            async with self._client.stream(
                                method, url, json=json_body, headers=self._headers(ts, accept)
                            ) as stream:
                                response_status = stream.status_code
                                data = bytearray()
                                async for chunk in stream.aiter_bytes():
                                    if len(data) + len(chunk) > max_response_bytes:
                                        raise UpstreamError(
                                            502, "上游图片工具结果过大",
                                            billing_uncertain=image_lane and (
                                                response_status in (200, 201) or response_status >= 500))
                                    data.extend(chunk)
                                headers = {k: v for k, v in stream.headers.items()
                                           if k.lower() not in {"content-encoding", "content-length"}}
                                resp = httpx.Response(stream.status_code, headers=headers,
                                                      content=bytes(data))
                response_status = resp.status_code
                request_timing.mark_status(resp.status_code)
                if resp.status_code in (200, 201) and image_count > 0:
                    try:
                        await anyio.to_thread.run_sync(lambda: validate_result(
                            resp.content, "generate-image", "", expected_images=image_count))
                    except ValueError:
                        raise UpstreamError(502, "上游未返回完整有效的图片结果",
                                            billing_uncertain=True) from None
                succeeded = resp.status_code in (200, 201)
                if resp.status_code == 429:
                    await self._rate_limit(ts, resp, on_rate_limited)
                    if image_lane:
                        raise UpstreamError(429, "上游限流(429)，全站图片生成已进入冷却")
                    continue
                if resp.status_code == 403 and image_lane:
                    self.mark_forbidden(ts)
                    if image_lane:
                        raise UpstreamError(403, "上游拒绝了请求（HTTP 403），该账号已临时隔离保护")
                self._warn_account(resp.status_code)
                if resp.status_code == 401:
                    self.mark_unauthorized(ts)
                    self._event("upstream_401", "NovelAI 返回 401：上游 Token 已失效，生图和文本全部不可用，请尽快更换 Token。", 1800)
                    raise UpstreamError(502, "上游令牌已失效（401），请站长更换 NovelAI Token")
                if succeeded:
                    self.mark_ok(ts)
                return resp
            except (httpx.HTTPError, TimeoutError) as exc:
                if not image_lane:
                    raise
                # 已发送请求的读写故障可能产生扣款；明确的 4xx 除外。
                uncertain = send_started and (
                    response_status is None or response_status in (200, 201) or response_status >= 500
                ) and isinstance(exc, (
                    httpx.ReadError, httpx.ReadTimeout, httpx.WriteError,
                    httpx.WriteTimeout, httpx.RemoteProtocolError, httpx.DecodingError, TimeoutError,
                ))
                raise UpstreamError(502, "上游图片请求连接中断或超时，未记费；请勿自动重试",
                                    billing_uncertain=uncertain) from None
            finally:
                try:
                    await _wait_cleanup(asyncio.create_task(self._settle(
                        ts, succeeded=succeeded, v5_free=v5_free, image_count=image_count, send_started=send_started
                    )))
                finally:
                    if slot_acquired:
                        ts.image_slots.release()
        raise UpstreamError(429, "上游限流(429)，请降低频率后重试")

    async def _resolve_v5_cost(self, ts, callback):
        try:
            async with ts.dispatch_lock:
                if not ts.admin_enabled:
                    raise UpstreamError(503, "上游令牌已停用，未查询额度或发送生图")
                exhausted = await self.allowance.resolve(
                    self._client, self.image_host, ts.token_id, ts.token)
        except AllowanceUnavailable as exc:
            raise UpstreamError(503, str(exc)) from None
        if exhausted and not ts.allow_anlas:
            raise UpstreamError(503, "该上游账号 V5 额度已耗尽，且未允许使用 Anlas；未发送生图")
        await callback(exhausted)
        return exhausted

    @asynccontextmanager
    async def _dispatch_guard(self, ts: TokenState, image_lane: bool):
        # 只在检查 admin_enabled 时持锁；HTTP 请求本身不能持锁，否则一个慢文本请求
        # 会把这把 Token 上的所有图片派发卡住（且不受 queue_timeout 约束）。
        # 停用开关仍会等图片槽空闲后才返回。
        async with ts.dispatch_lock:
            pass                    # 与 set_admin_enabled 串行：之后发出的请求一定能看到新状态
        yield

    @asynccontextmanager
    async def image_stream(
        self, url: str, json_body: Any, *, requires_anlas: bool = False,
        v5_free: bool = False,
        on_rate_limited: Optional[Callable[[float], Awaitable[None]]] = None,
        on_dispatch: Optional[Callable[[], None]] = None,
        resolve_v5_cost: Optional[Callable[[bool], Awaitable[None]]] = None,
        queue_timeout: float = 90,
        before_dispatch: Optional[Callable[[], None]] = None,
    ) -> AsyncIterator[ImageStreamHandle]:
        """图片流不重试；调用方只在确认完整最终图片后增加 completed_images。"""
        if self._client is None:
            raise RuntimeError("client not started")
        ts = await self.pick_token(requires_anlas=requires_anlas, v5_free=v5_free, image_job=True)
        if ts is None:
            raise self._unavailable(requires_anlas, v5_free)
        resp: Optional[httpx.Response] = None
        handle: Optional[ImageStreamHandle] = None
        send_started = False
        slot_acquired = False

        async def cleanup() -> None:
            close_failed = False
            try:
                if resp is not None:
                    try:
                        await resp.aclose()
                    except Exception:
                        close_failed = True
            finally:
                count = max(0, handle.completed_images) if handle is not None else 0
                await self._settle(ts, succeeded=count > 0, v5_free=v5_free,
                                   image_count=count, send_started=send_started)
                if count > 0:
                    self.mark_ok(ts)
            if close_failed:
                raise UpstreamError(502, "上游图片流连接关闭失败")

        try:
            try:
                async with asyncio.timeout(queue_timeout):
                    await ts.image_slots.acquire()
                    slot_acquired = True
                    await self.wait_for_token_image_slot(ts)
            except TimeoutError:
                raise UpstreamError(429, "上游图片任务排队超时，请稍后再试") from None
            if v5_free and resolve_v5_cost is not None:
                exhausted = await self._resolve_v5_cost(ts, resolve_v5_cost)
                if exhausted:
                    await self.finish_v5_reservation(ts, succeeded=False, v5_free=True)
                    v5_free = False
            req = self._client.build_request(
                "POST", url, json=json_body,
                headers=self._headers(ts, "application/x-msgpack" if
                                      json_body.get("parameters", {}).get("stream") == "msgpack"
                                      else "text/event-stream"),
            )
            async with self._dispatch_guard(ts, True):
                if not ts.admin_enabled:
                    raise UpstreamError(503, "上游令牌已停用，未发送生图")
                if before_dispatch is not None:
                    before_dispatch()
                if on_dispatch is not None:
                    on_dispatch()
                send_started = True
                request_timing.mark_sent()
                if self.guard is not None:
                    self.guard.record_start(ts.token_id)
                resp = await self._client.send(req, stream=True)
                request_timing.mark_status(resp.status_code)
            if resp.status_code == 429:
                await self._rate_limit(ts, resp, on_rate_limited)
                raise UpstreamError(429, "上游限流(429)，全站图片生成已进入冷却")
            if resp.status_code == 403:
                self.mark_forbidden(ts)
                raise UpstreamError(403, "上游拒绝了请求（HTTP 403），该账号已临时隔离保护")
            self._warn_account(resp.status_code)
            if resp.status_code == 401:
                self.mark_unauthorized(ts)
                self._event("upstream_401", "NovelAI 返回 401：上游 Token 已失效，生图和文本全部不可用，请尽快更换 Token。", 1800)
                raise UpstreamError(502, "上游令牌已失效（401），请站长更换 NovelAI Token")
            if resp.status_code not in (200, 201):
                status = resp.status_code if 400 <= resp.status_code <= 599 else 502
                raise UpstreamError(status, f"上游图片流请求失败（HTTP {status}）",
                                    billing_uncertain=resp.status_code >= 500)
            handle = ImageStreamHandle(resp)
            yield handle
        except httpx.HTTPError as exc:
            # 读写中断可能发生在上游已开始生成之后。
            uncertain = send_started and isinstance(exc, (
                httpx.ReadError, httpx.ReadTimeout, httpx.WriteError,
                httpx.WriteTimeout, httpx.RemoteProtocolError,
            ))
            raise UpstreamError(502, "上游图片流连接中断，请检查任务结果后再决定是否重试",
                                billing_uncertain=uncertain) from None
        finally:
            try:
                await _wait_cleanup(asyncio.create_task(cleanup()))
            finally:
                if slot_acquired:
                    ts.image_slots.release()

    async def stream(
        self, url: str, json_body: Any,
    ) -> AsyncIterator[httpx.Response]:
        """打开文本流，由调用方持有并关闭响应；错误请求不重试。"""
        if self._client is None:
            raise RuntimeError("client not started")
        ts = await self.pick_token()
        if ts is None:
            raise UpstreamError(503, "上游令牌全部被限流或不可用，请稍后再试")
        req = self._client.build_request(
            "POST", url, json=json_body, headers=self._headers(ts, "text/event-stream")
        )
        async with ts.dispatch_lock:
            if not ts.admin_enabled:
                raise UpstreamError(503, "上游令牌已停用，未发送请求")
            resp = await self._client.send(req, stream=True)
        if resp.status_code not in (200, 201):
            if resp.status_code == 401:
                self.mark_unauthorized(ts)
            # 文本 403 不计入熔断：成员反复请求文本不该让生图停摆。
            # 文本 429 不冻结整把 Token：否则成员刷文本就能让全站生图停摆。
            # Error bodies may stall or contain private upstream details.
            # Close before handing the failure back to the route, even on cancel.
            try:
                await _wait_cleanup(asyncio.create_task(resp.aclose()))
            except Exception:
                raise UpstreamError(502, "上游文本错误响应连接关闭失败") from None
            if resp.status_code == 401:
                self._event("upstream_401", "NovelAI 返回 401：上游 Token 已失效，生图和文本全部不可用，请尽快更换 Token。", 1800)
                raise UpstreamError(502, "上游令牌已失效（401），请站长更换 NovelAI Token")
            if resp.status_code == 403:
                raise UpstreamError(403, "上游拒绝了请求（HTTP 403），该账号已临时隔离保护")
            if resp.status_code == 429:
                raise UpstreamError(429, "上游限流(429)，请降低频率后重试")
            status = resp.status_code if resp.status_code in (400, 422, 503) else 502
            raise UpstreamError(status, f"上游文本请求失败（HTTP {resp.status_code}）")
        self.mark_ok(ts)
        return resp
