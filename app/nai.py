"""NovelAI 上游客户端：令牌池、429 退避、SSE 透传。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import anyio
import httpx

from .policy import mask_token
from .allowance import AllowanceCache, AllowanceUnavailable
from .image_tools import validate_result


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
        "dispatch_lock",
        "fails", "blocked_until", "disabled", "last_ok", "image_next_at",
    )

    def __init__(self, token: str, index: int, v5_daily_limit: int,
                 allow_anlas: bool):
        self.token = token
        # 永不把原始上游 Token 写入数据库；只存不可逆的短哈希标识。
        self.token_id = "token-" + hashlib.sha256(token.encode()).hexdigest()[:16]
        self.position = index + 1
        self.v5_daily_limit = max(0, v5_daily_limit)
        self.allow_anlas = allow_anlas
        self.admin_enabled = True
        self.dispatch_lock = asyncio.Lock()
        self.pending_v5 = 0
        self.fails = 0
        self.blocked_until = 0.0
        self.disabled = False
        self.last_ok = 0.0
        self.image_next_at = 0.0

    @property
    def usable(self) -> bool:
        return self.admin_enabled and not self.disabled and time.time() >= self.blocked_until


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

    def __init__(self, tokens: list[str], image_host: str, text_host: str,
                 legacy_text_host: str, *, db: Any, day_fn: Callable[[], str],
                 v5_daily_limits: list[int], allow_anlas: list[bool],
                 image_min_interval: float = 15):
        self.image_host = image_host.rstrip("/")
        self.text_host = text_host.rstrip("/")
        self.legacy_text_host = legacy_text_host.rstrip("/")
        self.pool = [
            TokenState(
                token,
                index,
                v5_daily_limits[index] if index < len(v5_daily_limits) else 0,
                allow_anlas[index] if index < len(allow_anlas) else True,
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

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15, read=300, write=120, pool=300),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
            headers={"User-Agent": "nai-gate/1.0"},
            follow_redirects=True,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    # ---------------- token pool ----------------
    async def load_saved_limits(self) -> None:
        saved = await self._db.get_upstream_token_limits()
        enabled = await self._db.get_upstream_token_enabled()
        for token in self.pool:
            if token.token_id in saved:
                token.v5_daily_limit = saved[token.token_id]
            if token.token_id in enabled:
                token.admin_enabled = enabled[token.token_id]

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
        return True

    async def pick_token(self, *, requires_anlas: bool = False,
                         v5_free: bool = False) -> Optional[TokenState]:
        """选取符合该图片费用策略的令牌；V5 限额在这里原子预留。"""
        async with self._lock:
            usable = [t for t in self.pool if t.usable and
                      (not requires_anlas or t.allow_anlas)]
            if not usable:
                return None

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
        """按上游实际成功响应记录生成张数；失败、拒绝和限流不计入。"""
        if image_count > 0:
            await self._db.bump_upstream_image_counter(
                ts.token_id, self._day_fn(), image_count
            )

    async def wait_for_token_image_slot(self, ts: TokenState) -> None:
        """每把上游 Token 各自保持图片请求间隔，不与其他 Token 共享计时。"""
        if not self._image_min_interval:
            return
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, ts.image_next_at - now)
            ts.image_next_at = max(now, ts.image_next_at) + self._image_min_interval
        if wait:
            await asyncio.sleep(wait)

    def mark_rate_limited(self, ts: TokenState, retry_after: float = 20.0) -> None:
        ts.blocked_until = time.time() + max(5.0, retry_after)
        ts.fails += 1

    def mark_unauthorized(self, ts: TokenState) -> None:
        ts.disabled = True

    def mark_ok(self, ts: TokenState) -> None:
        ts.fails = 0
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
                "v5_daily_limit": t.v5_daily_limit,
                "v5_used": counter["v5"],
                "images_today": counter["images"],
            })
        return result

    # ---------------- requests ----------------
    def _headers(self, ts: TokenState, accept: str = "*/*") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {ts.token}",
            "Accept": accept,
            "Content-Type": "application/json",
        }

    def _unavailable(self, requires_anlas: bool, v5_free: bool) -> UpstreamError:
        if requires_anlas:
            return UpstreamError(503, "没有允许使用 Anlas 的上游令牌，无法生成此图片")
        if v5_free:
            return UpstreamError(429, "可用上游令牌的今日 V5 免费图片额度已用完")
        return UpstreamError(503, "上游令牌全部被限流或不可用，请稍后再试")

    async def _settle(self, ts: TokenState, *, succeeded: bool,
                      v5_free: bool, image_count: int) -> None:
        await self.finish_v5_reservation(ts, succeeded=succeeded, v5_free=v5_free)
        if succeeded:
            await self.record_successful_images(ts, image_count)

    async def _rate_limit(self, ts: TokenState, resp: httpx.Response,
                          callback: Optional[Callable[[float], Awaitable[None]]]) -> None:
        try:
            retry_after = float(resp.headers.get("retry-after", "20"))
        except (ValueError, TypeError):
            retry_after = 20.0
        self.mark_rate_limited(ts, retry_after)
        if callback:
            await callback(max(5.0, retry_after))

    async def request(
        self, method: str, url: str, json_body: Any = None,
        accept: str = "*/*",
        on_rate_limited: Optional[Callable[[float], Awaitable[None]]] = None,
        *, requires_anlas: bool = False, v5_free: bool = False,
        image_count: int = 0, image_lane: bool = False, wait_for_image_slot: bool = True,
        resolve_v5_cost: Optional[Callable[[bool], Awaitable[None]]] = None,
        max_response_bytes: int | None = None,
    ) -> httpx.Response:
        """图片请求不自动重试；普通请求只对明确的 429 换 token 一次。"""
        if self._client is None:
            raise RuntimeError("client not started")
        attempts = 0
        while attempts < 2:
            attempts += 1
            ts = await self.pick_token(requires_anlas=requires_anlas, v5_free=v5_free)
            if ts is None:
                raise self._unavailable(requires_anlas, v5_free)
            succeeded = False
            send_started = False
            response_status = None
            try:
                if image_lane:
                    if wait_for_image_slot:
                        await self.wait_for_token_image_slot(ts)
                    else:
                        async with self._lock:
                            now = time.monotonic()
                            if ts.image_next_at > now:
                                raise UpstreamError(503, "上游正在处理图片请求，补全建议暂不可用")
                            ts.image_next_at = now + self._image_min_interval
                if v5_free and resolve_v5_cost is not None:
                    exhausted = await self._resolve_v5_cost(ts, resolve_v5_cost)
                    if exhausted:
                        await self.finish_v5_reservation(ts, succeeded=False, v5_free=True)
                        v5_free = False
                        requires_anlas = True
                async with ts.dispatch_lock:
                    if not ts.admin_enabled:
                        continue  # 停用发生在排队期间；改选其他上游，且不发出此请求。
                    send_started = True
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
                if resp.status_code == 401:
                    self.mark_unauthorized(ts)
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
                await _wait_cleanup(asyncio.create_task(self._settle(
                    ts, succeeded=succeeded, v5_free=v5_free, image_count=image_count
                )))
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
    async def image_stream(
        self, url: str, json_body: Any, *, requires_anlas: bool = False,
        v5_free: bool = False,
        on_rate_limited: Optional[Callable[[float], Awaitable[None]]] = None,
        on_dispatch: Optional[Callable[[], None]] = None,
        resolve_v5_cost: Optional[Callable[[bool], Awaitable[None]]] = None,
    ) -> AsyncIterator[ImageStreamHandle]:
        """图片 SSE 不重试；调用方只在确认完整最终图片后增加 completed_images。"""
        if self._client is None:
            raise RuntimeError("client not started")
        ts = await self.pick_token(requires_anlas=requires_anlas, v5_free=v5_free)
        if ts is None:
            raise self._unavailable(requires_anlas, v5_free)
        resp: Optional[httpx.Response] = None
        handle: Optional[ImageStreamHandle] = None
        send_started = False

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
                                   image_count=count)
                if count > 0:
                    self.mark_ok(ts)
            if close_failed:
                raise UpstreamError(502, "上游图片流连接关闭失败")

        try:
            await self.wait_for_token_image_slot(ts)
            if v5_free and resolve_v5_cost is not None:
                exhausted = await self._resolve_v5_cost(ts, resolve_v5_cost)
                if exhausted:
                    await self.finish_v5_reservation(ts, succeeded=False, v5_free=True)
                    v5_free = False
            req = self._client.build_request(
                "POST", url, json=json_body,
                headers=self._headers(ts, "text/event-stream"),
            )
            async with ts.dispatch_lock:
                if not ts.admin_enabled:
                    raise UpstreamError(503, "上游令牌已停用，未发送生图")
                if on_dispatch is not None:
                    on_dispatch()
                send_started = True
                resp = await self._client.send(req, stream=True)
            if resp.status_code == 429:
                await self._rate_limit(ts, resp, on_rate_limited)
                raise UpstreamError(429, "上游限流(429)，全站图片生成已进入冷却")
            if resp.status_code == 401:
                self.mark_unauthorized(ts)
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
            await _wait_cleanup(asyncio.create_task(cleanup()))

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
            elif resp.status_code == 429:
                self.mark_rate_limited(ts)
            # Error bodies may stall or contain private upstream details.
            # Close before handing the failure back to the route, even on cancel.
            try:
                await _wait_cleanup(asyncio.create_task(resp.aclose()))
            except Exception:
                raise UpstreamError(502, "上游文本错误响应连接关闭失败") from None
            if resp.status_code == 401:
                raise UpstreamError(502, "上游令牌已失效（401），请站长更换 NovelAI Token")
            if resp.status_code == 429:
                raise UpstreamError(429, "上游限流(429)，请降低频率后重试")
            status = resp.status_code if resp.status_code in (400, 422, 503) else 502
            raise UpstreamError(status, f"上游文本请求失败（HTTP {resp.status_code}）")
        self.mark_ok(ts)
        return resp
