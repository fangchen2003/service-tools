"""NAI Gate —— NovelAI 公益分发网关。

别人拿到的是本站签发的虚拟 Key（nai-xxx），真实 NovelAI Token 只保存在服务端。
网关负责：鉴权 -> 限流(RPM/并发/排队) -> 配额检查 -> 参数钳制(防爆费) -> 透传 -> 记账。
"""

from __future__ import annotations

import asyncio
import functools
from contextvars import ContextVar
import shutil
from datetime import datetime, timedelta
import json
import logging
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlencode, urlsplit

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from . import site_flags
from . import admin, live, registration_routes, request_timing
from .registration import configured_service
from .body import read_json_body
from .config import load_settings
from .client_views import subscription_payload
from .image_events import ImageEventTracker, ImageStreamProtocolError, STREAM_MEDIA_TYPES
from . import reasons
from .image_streaming import ImageStreamResponse
from .image_tools import prepare_tool, validate_result, MAX_RESPONSE_BYTES
from .image_payload import read_image_body
from .nai import UpstreamError, _wait_cleanup
from .policy import (
    normalize_image_request,
    upstream_parameter_problem,
    fix_name_digit_weights,
    clamp_image_params,
    clamp_text_params,
    estimate_image_cost,
    image_model_tier,
    is_v5_medium,
    fill_v4_prompt,
    medium_normalize,
    legacy_normal_free_eligible,
    validate_image_references,
    validate_vibe_encoding,
    VIBE_ENCODING_ANLAS,
    estimate_tokens,
    gen_key,
    text_model_host,
)
from .state import GateState
from . import features
from .policy import REFERENCE_FIELDS
from . import status_stats, quota_algo
from .key_sources import RETENTION_SECONDS as KEY_SOURCE_RETENTION
from .action_log import RETENTION_DAYS as ADMIN_ACTION_RETENTION_DAYS, log_action
from .audit import audit_flags, audit_disclosure, audit_image_cap_mb, audit_image_keep_hours, capture_prompts, full_image
from .upstream_errors import upstream_error_message, text_stream_events
from .sse import encode_sse

SETTINGS = load_settings()
STATE: Optional[GateState] = None



class GateError(Exception):
    def __init__(self, status: int, message: str, *, billing_uncertain: bool = False, code: str = ""):
        super().__init__(message)
        self.status = status
        self.message = str(message)
        self.billing_uncertain = billing_uncertain
        self.code = code or reasons.code_of(message)      # 拒绝原因码：guard 返回的 Reason 自带


def err(status: int, message: str, code: str = "") -> GateError:
    return GateError(status, message, code=code)


class QuerylessAccessFilter(logging.Filter):
    """Keep Uvicorn diagnostics without recording private URL query values."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if (record.msg != '%s - "%s %s HTTP/%s" %d'
                or not isinstance(args, tuple) or len(args) != 5
                or not isinstance(args[2], str)):
            return False  # Unknown access format: fail closed, never echo raw text.
        record.args = (*args[:2], args[2].partition("?")[0], *args[3:])
        return True


def install_access_log_filter() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, QuerylessAccessFilter) for item in logger.filters):
        logger.addFilter(QuerylessAccessFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    global STATE
    install_access_log_filter()
    STATE = GateState(SETTINGS)
    await STATE.db.connect()
    from . import status_page
    await status_page.ensure_schema(STATE.db)
    from . import database as _database
    _database.RULE_VERSION = __version__
    await STATE.load_runtime_limits()
    await STATE.db.migrate_upstream_token_ids([token.token_id for token in STATE.nai.pool])
    await STATE.nai.load_saved_limits()
    await STATE.guard.load()
    try:
        await STATE.guard.seed_hour(STATE.db, [t.token_id for t in getattr(STATE.nai, "pool", [])])
    except Exception as exc:
        bug("guard_seed", exc)
    if getattr(STATE, "share", None) is not None:
        await STATE.share.load()
    await STATE.load_image_cooldown()
    # 闲置回收不在启动时跑（交给每小时的循环，那时身份组回收回调已接好）。
    # 停机检测：最后一条请求日志距今超过 1 小时 → 网关停过机，从现在重新起算闲置天数，
    # 否则停机 ≥ 回收天数后一重启就会把所有成员的 Key 当成闲置一次删光（2026-10-10 审查 F2）。
    last = (await STATE.db._db.execute_fetchall("SELECT MAX(ts) FROM usage_log"))[0][0]
    if last and time.time() - float(last) > 3600:
        await STATE.db.set_setting("key_inactivity_grace_started_at", time.time())
        await log_action(STATE.db, "系统", "闲置计时重置", "", f"网关停机约 {(time.time() - float(last)) / 3600:.1f} 小时，闲置天数从现在重新计算")
    await STATE.nai.start()
    STATE.admin_pw_hash = await STATE.db.get_setting("admin_password_hash", None)   # 后台改过的密码（哈希）
    if SETTINGS.seed_demo_key:
        if not await STATE.db.get_key_by_token("nai-demo-key"):
            row = await STATE.db.create_key({
                "name": "演示钥匙", "token": gen_key("nai"),
                "daily_images": SETTINGS.default_daily_images,
                "monthly_anlas": SETTINGS.default_monthly_anlas,
                "daily_text_tokens": SETTINGS.default_daily_text_tokens,
                "rpm": SETTINGS.default_rpm, "expires_at": None,
            })
            print(f"[seed] 演示 Key: {row['token']}")
    if not SETTINGS.admin_password:
        print("[warn] 未设置 ADMIN_PASSWORD，/admin 管理端将无法登录！")
    if not SETTINGS.nai_tokens:
        print("[warn] 未设置 NAI_TOKENS，所有生成请求将返回 503")
    app.state.gate = STATE
    registration_http = httpx.AsyncClient()
    app.state.registrar = configured_service(STATE.db, registration_http)
    if app.state.registrar is not None:
        STATE.on_registration_released = app.state.registrar.release_role
    from . import modules as _modules
    STATE.kernel = _modules.build(STATE, bug)
    await STATE.kernel.load()
    STATE.kernel.extra["registrar"] = app.state.registrar

    async def _dm(key_id, text, reg=app.state.registrar):
        did = await reg.registration_for_key(key_id) if reg is not None else None
        if did is not None:
            await reg.send_dm(did, "🦉 猫头鹰公益站通知：" + text)
    STATE.kernel.extra["dm"] = _dm
    cleanup_task = asyncio.create_task(inactive_key_cleanup_loop())
    reset_task = asyncio.create_task(registration_reset_loop())
    maintenance_task = asyncio.create_task(maintenance_loop())
    anlas_task = asyncio.create_task(anlas_rebalance_loop())
    notify_owner("startup", "服务已启动（重启或更新部署后会收到这条）。", 60)
    try:
        yield
    finally:
        for task in (cleanup_task, reset_task, maintenance_task, anlas_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await registration_http.aclose()
        await STATE.nai.close()
        # 关停时还在跑的审计任务先写完再关库（10/10 22:55 部署时一条审计写在关库之后丢失）
        if _AUDIT_TASKS:
            await asyncio.wait(list(_AUDIT_TASKS), timeout=10)
        await STATE.db.close()


__version__ = "2.21.0"

app = FastAPI(title="猫头鹰公益站", version=__version__, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


class AdminNoStoreMiddleware:
    """后台 JSON 含完整成员 Key 与提示词：禁止任何缓存。

    用纯 ASGI 中间件而不是 BaseHTTPMiddleware，后者会改变取消语义，影响流式与计费清理。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/admin/api/"):
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() not in (b"cache-control", b"x-content-type-options")]
                headers += [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(AdminNoStoreMiddleware)


class NaiPathAliasMiddleware:
    """成员常把 OpenAI 文本用的 Base URL（…/v1）填进 NovelAI 客户端的接口地址，
    导致请求变成 /v1/ai/…、/v1/user/… 而 404。这里把多出来的 /v1 去掉，两种填法都能用。"""

    _PREFIXES = ("/v1/ai/", "/v1/user/")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith(self._PREFIXES):
            scope = dict(scope)
            scope["path"] = scope["path"][3:]
            raw = scope.get("raw_path")
            if isinstance(raw, (bytes, bytearray)) and raw.startswith(b"/v1/"):
                scope["raw_path"] = bytes(raw[3:])
        await self.app(scope, receive, send)


app.add_middleware(NaiPathAliasMiddleware)


class RequestContextMiddleware:
    """每个请求开始时清空“当前 Key / 是否已记日志”，结束后还原，避免跨请求串用。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        key_token, logged_token = _REQUEST_KEY.set(None), _REQUEST_LOGGED.set(False)
        timing_token = request_timing.begin(scope)
        rid = request_timing.rid().encode()

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), (b"x-request-id", rid)]}
            await send(message)
        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:
            # 500 处理器在这个中间件外面执行，那时上下文已还原：先把请求编号 / Key 挂到异常上
            exc._gate_ctx = (rid.decode(), _REQUEST_KEY.get(), _REQUEST_LOGGED.get() or request_timing.logged())
            raise
        finally:
            _REQUEST_KEY.reset(key_token)
            _REQUEST_LOGGED.reset(logged_token)
            request_timing.end(timing_token)


app.add_middleware(RequestContextMiddleware)


class StatusStatsMiddleware:
    """成员接口的状态码计数（后台「状态码分布」）。没发响应就断开 = 499，抛异常 = 500。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not status_stats.tracked(scope.get("path", "")):
            return await self.app(scope, receive, send)
        seen: dict[str, int] = {}

        async def send_status(message):
            if message["type"] == "http.response.start":
                seen["code"] = int(message.get("status", 0))
            await send(message)
        try:
            await self.app(scope, receive, send_status)
        except BaseException:
            status_stats.record(seen.get("code") or 500)
            raise
        status_stats.record(seen.get("code") or 499)


app.add_middleware(StatusStatsMiddleware)
if SETTINGS.cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=SETTINGS.cors_origins,
                       allow_methods=["*"], allow_headers=["*"], allow_credentials=False)
app.include_router(admin.router)
from . import status_page as _status_page  # noqa: E402
app.include_router(_status_page.router)
app.include_router(_status_page.admin_router)
app.include_router(registration_routes.router)
app.include_router(registration_routes.member_router)


# 当前请求已认出的 Key，以及本请求是否已写过用量日志；用于在统一错误处理里补记“被拒绝”。
_REQUEST_KEY: ContextVar = ContextVar("gate_request_key", default=None)
_REQUEST_LOGGED: ContextVar = ContextVar("gate_request_logged", default=False)
_PATH_KIND = (("generate-image-stream", "image_stream"), ("suggest-tags", "tags"), ("generate-image", "image"),
              ("encode-vibe", "vibe_encode"), ("upscale", "upscale"), ("augment-image", "augment-image"),
              ("generate-voice", "voice"), ("generate-stream", "text"), ("/ai/generate", "text"),
              ("/v1/chat/completions", "chat"))


def _kind_for_path(path: str) -> str:
    return next((kind for needle, kind in _PATH_KIND if needle in path), "account")


NOTICE_PREFIX = "猫头鹰公益站提醒："     # 成员在客户端里看到的每条报错都带上来源，免得以为是 NovelAI 官方出错


@app.exception_handler(GateError)
async def gate_error_handler(request: Request, exc: GateError):
    key = _REQUEST_KEY.get()
    if key is not None and 400 <= exc.status < 500 and not (_REQUEST_LOGGED.get() or request_timing.logged()):
        # 鉴权之后被拒（Key 停用 / 过期、功能未开通、额度用完、限流、排队超时……）统一记一条，方便排查成员问题
        try:
            record(key, _kind_for_path(request.url.path), request_timing.model(), "rejected",
                   detail=f"{exc.status} {exc.message}"[:160], reason=exc.code)
        except Exception as log_exc:
            bug("log:rejected", log_exc, path=request.url.path)
    if exc.status >= 500:          # 上游失败 / 网关故障：按消息归并，方便看出哪类问题在变多
        bug("upstream" if exc.status in (502, 503, 504) else "gate", title=f"{exc.status} {exc.message}"[:200],
            path=request.url.path, level="warn")
    message = exc.message if exc.message.startswith(NOTICE_PREFIX) else NOTICE_PREFIX + exc.message
    # 额度用完一律 402（不可重试）：客户端遇到 429 会自动重试，并把原因替换成自己的「请求过于频繁」。
    # 顶层 statusCode / message 与 NovelAI 官方错误格式一致，客户端直接显示这句话。
    return JSONResponse({"statusCode": exc.status, "message": message,
                         "error": {"message": message, "status": exc.status, "request_id": request_timing.rid()}},
                        status_code=exc.status)


@app.exception_handler(Exception)
async def fallback_handler(request: Request, exc: Exception):
    """没有预料到的异常 = bug：记录堆栈、私信站长，给成员一个可以报给站长的请求编号。"""
    rid, key, logged = getattr(exc, "_gate_ctx", (request_timing.rid(), _REQUEST_KEY.get(),
                                                 _REQUEST_LOGGED.get() or request_timing.logged()))
    bug("request", exc, path=request.url.path, rid=rid, key_id=key["id"] if key is not None else None)
    if key is not None and not logged:
        try:
            await STATE.db.add_log(key["id"], key["name"], _kind_for_path(request.url.path), "", "error",
                                   detail=f"500 {type(exc).__name__}"[:160], rid=rid)
        except Exception:
            pass
    return JSONResponse({"error": {"message": f"{NOTICE_PREFIX}服务器内部错误，已自动记录。反馈时请附上错误编号 {rid}",
                                   "status": 500, "request_id": rid}}, status_code=500,
                        headers={"X-Request-Id": rid} if rid else None)


# ================================================================ helpers ====

async def authenticate(request: Request, *, passive: bool = False):
    """校验虚拟 Key。"""
    client_id = request.client.host if request.client else "unknown"
    wait = getattr(STATE, "auth_blocked", lambda _ip: 0)(client_id)
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        if wait:
            raise GateError(429, f"无效请求过多，请 {wait // 60 + 1} 分钟后再试")
        raise err(401, "缺少 API Key")
    row = await STATE.db.get_key_by_token(token)
    if row:
        _REQUEST_KEY.set(row)
    if not row:
        # 被拦截的 IP 只拦“无效 Key”；持有有效 Key 的成员（如同一出口 IP 的其他人）不受影响。
        if wait:
            raise GateError(429, f"无效请求过多，请 {wait // 60 + 1} 分钟后再试")
        getattr(STATE, "record_auth_failure", lambda _ip: None)(client_id)
        raise err(401, "无效的 API Key")
    if not row["enabled"]:
        raise err(403, "该 Key 已被禁用")
    if row["expires_at"] and row["expires_at"] < time.time():
        raise err(403, "该 Key 已过期，请联系站长续期")
    if passive:          # 首页每秒查排队位置：只读，不算使用，不参与防分享统计
        return row
    share = getattr(STATE, "share", None)
    if share is not None and share.paused_until(row["id"]):
        until = time.strftime("%m-%d %H:%M", time.localtime(share.paused_until(row["id"])))
        reason = getattr(share, "pause_reasons", {}).get(row["id"])
        if reason:
            raise err(403, f"{reason}，{until} 自动恢复。如有疑问请联系站长。", reasons.KEY_PAUSED)
        raise err(403, f"你的 Key 因检测到多人共用已暂停，{until} 自动恢复。Key 仅限本人使用；如果是误判，请联系站长。",
                  reasons.KEY_PAUSED)
    # 只要 Key 实际通过鉴权即视为使用，避免 Launcher 登录/上游暂时失败时被误删。
    # 记「最后使用时间」只是记账：写失败（如 database is locked）不能让成员的请求 500（10/11 02:01 实际发生一次）。
    try:
        await STATE.db.touch_key(row["id"])
    except Exception as exc:
        bug("touch_key", exc)
    sources = getattr(STATE, "sources", None)
    if sources is not None:
        try:                            # 来源网段统计（防 Key 分享）；失败不能影响请求
            await sources.observe(row, client_id, client=request.headers.get("user-agent", ""))
        except Exception as exc:
            bug("key_sources", exc)
    from .key_sources import network_of
    found = network_of(client_id)
    request_timing.set_source(found[1] if found else "")
    if share is not None and found:
        try:                            # 防分享：证据 + 风险分 + 自动处罚（share_guard.py）；失败不能影响请求
            guard = getattr(STATE, "guard", None)
            busy = bool(guard and guard.image_inflight.get(row["id"], 0) > 0)
            await share.observe(row, found[1], 6 if ":" in found[1] else 4, request.headers.get("user-agent", ""),
                                busy=busy, **_share_callbacks(request))
        except Exception as exc:
            bug("share_guard", exc)
    return row


def _share_callbacks(request: Request) -> dict:
    reg = getattr(request.app.state, "registrar", None)

    async def member(key_id: int, text: str) -> None:
        did = await reg.registration_for_key(key_id) if reg is not None else None
        if did is not None:
            await reg.send_dm(did, NOTICE_PREFIX + text)

    async def reset(key_id: int):
        token = gen_key("nai")
        return token if await STATE.db.rotate_key_token(key_id, token) else None

    async def ban(key_id: int) -> None:
        did = await reg.registration_for_key(key_id) if reg is not None else None
        if did is not None:
            await reg.ban(did)
        else:
            await STATE.db.update_key(key_id, {"enabled": 0})

    def admin(message: str) -> None:
        notify_owner(f"share_guard_{time.time():.0f}", message, cooldown=0)
    return {"member": member, "reset": reset, "ban": ban, "admin": admin}


async def require_feature(key, name: str) -> None:
    reason = await features.check(STATE.db, key, name)
    if reason:
        raise err(403, reason)


def upstream_outcome(ok: bool) -> None:
    tracker = getattr(STATE, "record_upstream", None)
    if tracker is not None:
        tracker(ok)


def bug(source: str, exc: BaseException | None = None, **kw) -> str:
    """记一条 bug（见 errors.py）；自动带上当前请求的编号和 Key。"""
    tracker = getattr(STATE, "bugs", None)
    if tracker is None:
        return ""
    key = _REQUEST_KEY.get()
    kw.setdefault("rid", request_timing.rid())
    kw.setdefault("key_id", key["id"] if key is not None else None)
    return tracker.capture(source, exc, **kw)


def notify_owner(kind: str, message: str, cooldown: float = 900) -> None:
    alerter = getattr(STATE, "alerter", None)
    if alerter is not None:
        alerter.notify(kind, message, cooldown=cooldown)


_AUDIT_TASKS: set = set()


def schedule_audit(*args) -> None:
    """缩略图编码较慢：放到后台任务里做，不占用图片并发槽和预算锁，也不拖慢返回。"""
    task = asyncio.get_running_loop().create_task(audit_generation(*args))
    _AUDIT_TASKS.add(task)
    task.add_done_callback(_AUDIT_TASKS.discard)


async def audit_generation(key, kind: str, model: str, status: str, body: dict, content: bytes | None = None) -> None:
    """按配置记录提示词和原图（不再另存缩略图，后台需要小图时从原图现场缩小）；任何失败都不得影响生图结果。"""
    cfg = STATE.settings
    try:
        want_prompts, want_thumbs, _days = await audit_flags(STATE.db, cfg)
    except Exception:
        return
    if not (want_prompts or want_thumbs):
        return
    try:
        prompt, negative, extra = capture_prompts(body) if want_prompts else ("", "", "")
        thumb, image, image_type = None, None, ""
        if want_thumbs and content and status == "ok":           # 「记录生成结果」开关就是存原图的开关
            image, image_type = await anyio.to_thread.run_sync(full_image, content)
        await STATE.db.add_audit(key["id"], key["name"], kind, model, status, prompt, negative, thumb,
                                 extra=extra, image=image, image_type=image_type)
    except Exception as exc:
        bug("audit", exc)


async def check_upstream_perf() -> None:
    """上游变慢 / 限流增多 / 失败率上升 / 账号受限时私信站长；同类提醒 3 小时内只发一次。"""
    from . import perf
    try:
        report = await perf.collect(STATE, time.time())
    except Exception as exc:
        bug("maintenance:perf", exc)
        return
    for flag in report["flags"]:
        notify_owner(f"perf_{flag['family']}_{flag['code']}",
                     "📉 上游表现变化 · " + flag["text"] + " 详情见后台「上游」页。", 3 * 3600)


async def maintenance_loop() -> None:
    """每 5 分钟：清理过期生成记录；磁盘与告警自检。"""
    async def purge():
        # 0 = 长期保留：已向成员披露保留范围，用于防滥用、回测和优化算法（见 audit.audit_disclosure）
        days = (await audit_flags(STATE.db, STATE.settings))[2]
        if days > 0:
            await STATE.db.purge_audit(time.time() - days * 86400)
        keep = STATE.settings.usage_log_retention_days
        if keep > 0:
            await STATE.db.purge_usage_log(time.time() - max(7, keep) * 86400)
        await STATE.db.purge_key_sources(time.time() - KEY_SOURCE_RETENTION)
        await STATE.db.purge_admin_actions(time.time() - ADMIN_ACTION_RETENTION_DAYS * 86400)
        await status_stats.flush(STATE.db)
        await STATE.db._db.execute("DELETE FROM status_hourly WHERE hour<?", (time.time() - 30 * 86400,))
        await STATE.db._db.commit()
        if getattr(STATE, "bugs", None) is not None:
            await STATE.bugs.purge(time.time() - 30 * 86400)
        # 原图：保存 N 小时后删（10-11 站长定 24 小时），另外总量超过容量上限时删最旧的；站长也可以在后台手动清空
        try:
            keep_h = await audit_image_keep_hours(STATE.db)
            if keep_h > 0:
                n = await STATE.db.purge_audit_images(time.time() - keep_h * 3600)
                if n:
                    print(f"[audit] 原图保存超过 {keep_h} 小时，已删 {n} 张", flush=True)
            await STATE.db.sweep_orphan_images(time.time() - 3600)
            cap_mb = await audit_image_cap_mb(STATE.db)
            if cap_mb > 0:
                n = await STATE.db.purge_audit_images_to_cap(cap_mb * 1024 * 1024)
                if n:
                    print(f"[audit] 原图超过容量上限 {cap_mb} MB，已清掉最旧的 {n} 张", flush=True)
        except Exception as exc:
            bug("audit_image_purge", exc)

    async def registrar_jobs():
        registrar = getattr(app.state, "registrar", None)
        if registrar is not None:
            for name in ("sync_roles", "backfill_profiles", "sweep_departed", "invite_waitlist"):
                try:
                    if name == "invite_waitlist":
                        ann = getattr(STATE, "announcer", None)
                        await registrar.invite_waitlist(announce=ann.post if ann is not None else None)
                    else:
                        await getattr(registrar, name)()
                except Exception as exc:
                    bug(f"maintenance:{name}", exc)

    async def disk():
        usage = shutil.disk_usage(STATE.settings.data_dir)
        if usage.free / usage.total < 0.10:
            notify_owner("disk_low", f"服务器磁盘剩余不足 10%（剩 {usage.free // 2**20} MB），请清理或扩容。", 6 * 3600)

    while True:
        # 每一步单独兜底：一步失败不影响后面的步骤，并且每种失败都进 Bug 追踪
        async def official():
            from . import status_page
            await status_page.fetch_official(STATE.db)
        for name, job in (("purge", purge), ("registrar", registrar_jobs), ("perf", check_upstream_perf), ("disk", disk),
                          ("official", official)):
            try:
                await job()
            except Exception as exc:
                bug(f"maintenance:{name}", exc)
        await asyncio.sleep(300)


async def inactive_key_cleanup_loop() -> None:
    """常驻服务每小时回收一次长期闲置 Key。"""
    while True:
        try:
            registrar = getattr(app.state, "registrar", None)
            if registrar is not None:
                await STATE.remind_idle_keys(registrar.send_dm, SETTINGS.site_url.rstrip("/"))
            removed = await STATE.delete_inactive_keys()
            if removed:
                print(f"[info] deleted {removed} inactive API key(s)")
        except Exception as exc:
            bug("cleanup", exc)
        await asyncio.sleep(3600)


ANLAS_REBALANCE_SECONDS = 600


async def anlas_rebalance_loop() -> None:
    """每 10 分钟：按模块登记顺序运行各模块的周期任务（容量 / 分配 / Anlas / 自动驾驶……），再做交叉校验。
    模块和开关见 modules.py / kernel.py；单个模块出错不影响其他模块。"""
    while True:
        kernel = getattr(STATE, "kernel", None)
        if kernel is not None:
            try:
                await kernel.tick_all()
            except Exception as exc:
                bug("kernel", exc)
        await asyncio.sleep(ANLAS_REBALANCE_SECONDS)


async def registration_reset_loop() -> None:
    """可选：每天固定时刻（REGISTER_RESET_AT=HH:MM，按 TZ）清空全部自助注册用户。"""
    while True:
        service = getattr(app.state, "registrar", None)
        at = service.reset_at if service else ""
        try:
            hour, minute = (int(part) for part in at.split(":"))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError
        except ValueError:
            await asyncio.sleep(3600)   # 未配置或格式错误：保持关闭，但允许改配置后重启生效
            continue
        now = datetime.now(STATE.tz)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            removed = await service.reset_all()
            print(f"[info] scheduled registration reset: removed {removed} user(s)")
        except Exception as exc:
            bug("registration_reset", exc)


async def check_rpm(key) -> None:
    if key["is_admin"]:
        return
    ok = await STATE.hit_rpm(key["id"], key["rpm"])
    if not ok:
        raise err(429, f"请求过于频繁（上限 {key['rpm']} 次/分钟），请稍后再试")


def check_image_cooldown() -> None:
    remaining = STATE.image_cooldown_remaining()
    if remaining:
        raise err(429, f"上游图片服务限流保护中，所有图片生成暂停约 {remaining} 秒", reasons.COOLDOWN)


MAX_INFLIGHT_PER_KEY = 4
_INFLIGHT: dict[str, int] = {}
TEXT_BODY_MB = 1          # 文本输入上限 24000 字符，1 MB 足够任何合法请求


def limit_inflight(handler):
    """读请求体之前按 Authorization 限制同时进行中的请求数。

    25 MB 的生图请求体解析后会占用数倍内存；不限制时单个成员并发堆积即可拖垮 2 GB 机器。
    """
    @functools.wraps(handler)
    async def wrapper(request: Request):
        raw = request.headers.get("authorization", "")[:512].strip()
        scheme, _, rest = raw.partition(" ")
        # 和鉴权一样归一化：「Bearer K」「bearer K」「Bearer  K」是同一把 Key，不能各算一份并发（审查 P2）
        ident = rest.strip() if scheme.lower() == "bearer" else raw
        if not ident:
            return await handler(request)
        count = _INFLIGHT.get(ident, 0)
        if count >= MAX_INFLIGHT_PER_KEY:
            raise err(429, f"同时进行中的请求过多（上限 {MAX_INFLIGHT_PER_KEY} 个），请等前面的请求完成")
        _INFLIGHT[ident] = count + 1
        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            left = _INFLIGHT.get(ident, 1) - 1
            if left > 0:
                _INFLIGHT[ident] = left
            else:
                _INFLIGHT.pop(ident, None)
        try:
            resp = await handler(request)
        except BaseException:
            release()
            raise
        if isinstance(resp, (StreamingResponse, ImageStreamResponse)):
            return _ReleaseAfterSend(resp, release)      # 流式：处理函数一返回流才刚开始，名额要占到推流结束
        release()
        return resp
    return wrapper


class _ReleaseAfterSend(Response):
    """包住流式响应：整个推流（含客户端断开后的上游结算）结束才归还 limit_inflight 名额。"""

    def __init__(self, inner: Response, release) -> None:
        self.inner, self.release = inner, release
        self.status_code = inner.status_code
        self.background = None
        self.raw_headers = inner.raw_headers          # 同一个列表：FastAPI 往外层追加的头也进到真正发出的响应

    async def __call__(self, scope, receive, send) -> None:
        try:
            await self.inner(scope, receive, send)
            if self.background is not None:
                await self.background()
        finally:
            self.release()


async def read_json(request: Request, limit_mb: float = 25) -> dict:
    try:
        return await read_json_body(request, int(limit_mb * 1024 * 1024))
    except HTTPException as exc:
        raise err(exc.status_code, exc.detail) from None


async def read_image_payload(request: Request, limit_mb: float = 25) -> dict:
    """JSON 与 multipart 附件统一进入后续权限、参数和费用检查。"""
    try:
        return await read_image_body(request, int(limit_mb * 1024 * 1024))
    except HTTPException as exc:
        raise err(exc.status_code, exc.detail) from None


def _anlas_auto(key) -> bool:
    """anlas_auto：1 = 自动分配管理；0 = 交给算法（暂未分到）；-1 = 站长手动管理（关闭或手动额度）。"""
    try:
        return key["anlas_auto"] == 1
    except (KeyError, IndexError, TypeError):
        return False


def _flag(key, name: str) -> bool:
    """读 Key 上可能不存在的新列（老测试数据 / 假对象）。"""
    try:
        return bool(key[name])
    except (KeyError, IndexError, TypeError):
        return False


def _manual(key) -> bool:
    """手动额度的 Key：填多少就是多少，不受保底 / 借用规则影响。"""
    try:
        return key["quota_auto"] == -1
    except (KeyError, IndexError, TypeError):
        return False


def daily_base(key) -> int:
    """这把 Key 的每日保底张数；0 表示没有保底 / 借用之分（整天都按每日上限）。"""
    guard = getattr(STATE, "guard", None)
    base = guard.values["base_daily_images"] if guard is not None else 0
    if key["is_admin"] or not base or not key["daily_images"] or base >= key["daily_images"] or _manual(key):
        return 0
    return base


async def guard_precheck() -> None:
    """出图前先看账号保护：所有可用账号都到了每日 / 每小时上限或安静时段上限时，直接 429 并说明原因。"""
    guard = getattr(STATE, "guard", None)
    pool = [t for t in getattr(STATE.nai, "pool", []) if t.usable]
    if guard is None or not pool:
        return
    reasons = [await guard.token_block_reason(STATE.db, t.token_id, STATE.day()) for t in pool]
    if all(reasons):
        raise err(429, reasons[0])


@asynccontextmanager
async def image_admission(key):
    """P1：每把 Key 同时生成 1 张、最多再排 N 张；全站排队总数有上限。超出直接 429，不进入排队。"""
    guard = getattr(STATE, "guard", None)
    if guard is None or key["is_admin"]:
        yield
        return
    await guard_precheck()
    accounts = sum(1 for t in getattr(STATE.nai, "pool", []) if t.usable)
    reason = guard.admit_image(key["id"], accounts)
    if reason:
        raise err(429, reason)
    entry_id = guard.entries[-1]["id"]           # admit_image 刚追加的那一条（中间没有 await，不会被别人插队）
    def _on_sent():
        waiting = guard.waiting_keys()           # 本张开始发往上游时，还在排队的其他 Key
        guard.mark_running(key["id"], entry_id)
        sched = getattr(STATE, "sched", None)
        if sched is not None:
            try:
                sched.observe_pick(key["id"], waiting)
                sched.on_serve(key["id"])
            except Exception as exc:
                bug("scheduling", exc)
    request_timing.on_sent(_on_sent)
    try:
        yield
    finally:
        guard.release_image(key["id"], entry_id)


@asynccontextmanager
async def acquire_concurrency(key, *, image: bool = False):
    """Per-user admission; text also uses the legacy global request limit."""
    if image:
        async with image_admission(key), _acquire_slots(key, image=True):
            yield
    else:
        async with _acquire_slots(key, image=False):
            yield


@asynccontextmanager
async def _acquire_slots(key, *, image: bool):
    t = STATE.settings.queue_timeout
    ksem = None if key["is_admin"] else STATE.key_sem(key["id"], STATE.settings.key_concurrency)
    async with AsyncExitStack() as resources:
        STATE.global_waiting += 1
        try:
            async with asyncio.timeout(t):
                if ksem is not None:
                    await resources.enter_async_context(ksem)
                if not image:
                    await resources.enter_async_context(STATE.global_sem)
        except TimeoutError:
            raise err(429, "当前排队人数过多，请稍后再试")
        finally:
            STATE.global_waiting -= 1
        STATE.global_active += 1
        try:
            yield
        finally:
            STATE.global_active -= 1


async def quota_image_check(key, est: dict, *, legacy_free_images: int = 0,
                            exclude_reservation=None) -> None:
    if key["is_admin"]:
        return
    c = await STATE.db.get_counter(key["id"], STATE.day())
    pending = [r for r in getattr(STATE, "image_reservations", {}).values()
               if r is not exclude_reservation]
    own = [r for r in pending if r.key_id == key["id"]]
    # A request started before midnight can settle after midnight; count every
    # in-flight reservation conservatively against the current period.
    own_day = own_month = own
    global_day = global_month = pending
    if legacy_free_images and key["daily_images"] > 0:
        used_legacy = c["legacy_free_images"] + sum(r.legacy for r in own_day)
        if used_legacy + legacy_free_images > key["daily_images"]:
            raise err(402, f"今日 V4.5 及以下免费图额度已用完（{key['daily_images']} 张/天），明日恢复")
        # 保底与借用：超过每日保底后，只在全站空闲（没人排队、本小时用量不高）时放行，直到 Key 的每日上限。
        guard = getattr(STATE, "guard", None)
        base = guard.values["base_daily_images"] if guard is not None else 0
        if base and base < key["daily_images"] and used_legacy + legacy_free_images > base and not _manual(key):
            tokens = [t.token_id for t in getattr(STATE.nai, "pool", []) if t.usable]
            # 按时段放行时，超出保底的部分只能用「这个时段放出来、还剩得多」的份额：至少留半小时的量给保底内的人
            reserve = guard.values["account_daily_cap"] / 48 * len(tokens)
            if not guard.site_idle(key["id"], tokens):
                raise err(429, reasons.Reason(f"今天的保底 {base} 张已用完。全站空闲时可以继续用到 {key['daily_images']} 张，"
                                      "现在有其他人在排队或用量较高，请过几分钟再试", reasons.OVER_BASE))
            if await guard.paced_headroom(STATE.db, tokens, STATE.day()) <= reserve:
                # 文案不能含「其他人在排队」：每日复盘按它数 base_blocks（「有余量还被拦」），这里是份额紧（Sonnet 审查 P1）
                raise err(429, reasons.Reason(f"今天的保底 {base} 张已用完。这个时段放出的出图份额所剩不多，先留给还没用到保底的成员；"
                                      f"份额充裕时可以继续用到 {key['daily_images']} 张，请稍后再试", reasons.OVER_BASE))
    if est["v5"] > 0:
        # V5 周额度是账户级共享资源，用全站日计数镜像（恢复量 ~190 张/天）
        try:
            manual = key["quota_auto"] == -1
        except (KeyError, IndexError, TypeError):
            manual = False
        borrow = quota_algo.borrow_for(key["id"]) if key["daily_v5"] > 0 and not manual else 0    # 手动 Key 填多少就是多少，不借
        if key["daily_v5"] > 0 and c["v5"] + sum(r.v5 for r in own_day) + est["v5"] > key["daily_v5"] + borrow:
            raise err(402, f"已达今日 V5 额度（{key['daily_v5']} 张/天" + (f"，含空闲借用 {borrow} 张" if borrow else "")
                      + "），明天恢复后再用")
        g = await site_flags.get(STATE.db, site_flags.GLOBAL_DAILY_V5, STATE.settings)
        if g > 0 and quota_algo.BORROW.get("open"):
            g = int(g * (1 + quota_algo.BORROW_GLOBAL_RATIO))
        if g > 0 and not key["exclude_global_v5"]:
            total = await STATE.db.day_v5_total(STATE.day())
            if total + sum(r.v5 for r in global_day if not r.exclude_global_v5) + est["v5"] > g:
                raise err(402, f"全站今日 V5 额度已用完（{int(g)} 张/天），明天再来")
    if est["anlas"] > 0:
        if not key["allow_anlas"]:
            raise err(402, "该请求会消耗 Anlas，此 Key 未开通付费额度权限")
        # 2026-10-10 站长：放开 Anlas。只用于 NovelAI 一定按 Anlas 收费的操作（超规格尺寸 / 步数、一次多张、
        # Vibe、放大、导演工具）：这些不占 V5 免费额度（官方 FAQ #38），也不影响 V4.5（Opus 不限）。每人仍受每日 Anlas 上限约束。
        if key["daily_anlas"] > 0 and c["anlas"] + sum(r.anlas for r in own_day) + est["anlas"] > key["daily_anlas"]:
            raise err(402, f"今日 Anlas 额度不足（已用 {c['anlas']:.0f} / 上限 "
                           f"{key['daily_anlas']:.0f}），明日恢复")
        used = await STATE.db.month_anlas(key["id"], STATE.month())
        if key["monthly_anlas"] > 0 and used + sum(r.anlas for r in own_month) + est["anlas"] > key["monthly_anlas"]:
            raise err(402, f"本月 Anlas 额度不足（已用 {used:.0f}/{key['monthly_anlas']:.0f}）")
        budget = await site_flags.get(STATE.db, site_flags.GLOBAL_MONTHLY_ANLAS, STATE.settings)
        if budget > 0:
            all_used = await STATE.db.month_anlas_all(STATE.month())
            if all_used + sum(r.anlas for r in global_month) + est["anlas"] > budget:
                raise err(402, f"全站本月 Anlas 预算已耗尽（{budget:.0f}），请联系站长")


class ImageReservation:
    def __init__(self, key, est, legacy):
        self.key_id = key["id"]
        self.exclude_global_v5 = bool(key["exclude_global_v5"])
        self.anlas = est["anlas"]
        self.v5 = est["v5"]
        self.legacy = legacy

    async def update(self, key, est, legacy=0):
        async with STATE.image_budget_lock:
            await quota_image_check(key, est, legacy_free_images=legacy,
                                    exclude_reservation=self)
            self.anlas, self.v5, self.legacy = est["anlas"], est["v5"], legacy


@asynccontextmanager
async def reserve_image_budget(key, est, *, legacy_free_images=0):
    reservation = ImageReservation(key, est, legacy_free_images)
    try:
        async with asyncio.timeout(STATE.settings.queue_timeout):
            async with STATE.image_budget_lock:
                await quota_image_check(key, est, legacy_free_images=legacy_free_images)
                reservations = getattr(STATE, "image_reservations", None)
                if reservations is None:
                    reservations = STATE.image_reservations = {}
                reservations[id(reservation)] = reservation
                if hasattr(STATE, "image_budget_idle"):
                    STATE.image_budget_idle.clear()
    except (TimeoutError, GateError) as exc:
        # 尚未到达上游：归还用户 Key 的图片冷却占用，别让被拒的请求白占 15 秒。
        refund = getattr(STATE, "refund_key_image_slot", None)
        if refund is not None and not key["is_admin"]:
            await refund(key["id"])
        if isinstance(exc, TimeoutError):
            raise err(429, "图片预算核对排队超时，请稍后再试") from None
        raise
    try:
        yield reservation
    finally:
        with anyio.CancelScope(shield=True):
            async with STATE.image_budget_lock:
                STATE.image_reservations.pop(id(reservation), None)
                if not STATE.image_reservations and hasattr(STATE, "image_budget_idle"):
                    STATE.image_budget_idle.set()


def record(key, kind: str, model: str, status: str, *, images: int = 0,
           anlas: float = 0.0, tokens: int = 0, v5: int = 0,
           legacy_free_images: int = 0, detail: str = "",
           unconfirmed_anlas: float = 0.0, reason: str = "") -> asyncio.Task:
    """写日志；成功请求额外计入每日配额。"""
    _REQUEST_LOGGED.set(True); request_timing.mark_logged()
    live.note(status)
    timing = request_timing.snapshot()
    async def _go():
        if status == "ok":
            await STATE.db.record_success(
                key["id"], key["name"], kind, model, STATE.day(),
                images=images, anlas=anlas, tokens=tokens, v5=v5,
                legacy_free_images=legacy_free_images, detail=detail,
                unconfirmed_anlas=unconfirmed_anlas, **timing,
            )
        else:
            await STATE.db.add_log(key["id"], key["name"], kind, model, status,
                                   images=images, anlas=anlas, tokens=tokens, detail=detail,
                                   unconfirmed_anlas=unconfirmed_anlas, reason=reason, **timing)
    task = asyncio.create_task(_go())
    task.add_done_callback(_log_task_failure)
    return task


def _log_task_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        print("[error] request accounting failed")
        bug("accounting", task.exception())


async def settle_record(*args, **kwargs) -> None:
    """Finish the successful ledger write before its budget/concurrency locks release."""
    with anyio.CancelScope(shield=True):
        task = record(*args, **kwargs)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise


async def complete_image_operation(operation, *, can_cancel=None, delivered=None):
    """Once sent, finish upstream response handling and its ledger even on disconnect.

    The caller retains both the budget and concurrency locks. A caller cancelled
    while upstream-token accounting is in progress must not lose the user charge.
    Queue waits and quota prechecks remain cancellable outside this boundary.
    """
    cancelled = False
    with anyio.CancelScope(shield=True):
        task = asyncio.create_task(operation)
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
                # Streaming reserves a Token before its pacing wait. That wait
                # can still be cancelled safely until HTTP dispatch begins.
                if can_cancel is not None and can_cancel():
                    task.cancel()
        result = task.result()
    if cancelled and not (delivered is not None and delivered()):
        # 图已经生成（也已计数），但客户端先断开了：通常是客户端超时设得太短，成员会以为失败而重试。
        # 流式出图收到最终图后客户端自己关连接是正常的（2026-10-10 jhx666 连续误报），不记
        bug("disconnect", title="客户端在图片返回前断开连接（图已生成并计数）", level="warn")
    if cancelled:
        raise asyncio.CancelledError()
    return result


async def upstream_call(url: str, payload: dict, accept: str = "*/*", *,
                        on_rate_limited=None, requires_anlas: bool = False,
                        v5_free: bool = False, image_count: int = 0,
                        image_lane: bool = False, resolve_v5_cost=None,
                        max_response_bytes: int | None = None) -> httpx.Response:
    try:
        return await STATE.nai.request(
            "POST", url, payload, accept=accept, on_rate_limited=on_rate_limited,
            requires_anlas=requires_anlas, v5_free=v5_free, image_count=image_count,
            image_lane=image_lane,
            **({"queue_timeout": STATE.settings.queue_timeout,
                "before_dispatch": check_image_cooldown} if image_lane else {}),
            **({"max_response_bytes": max_response_bytes} if max_response_bytes else {}),
            **({"resolve_v5_cost": resolve_v5_cost} if resolve_v5_cost is not None else {}),
        )
    except UpstreamError as e:
        raise GateError(e.status if e.status in (429, 503) else 502, e.message,
                        billing_uncertain=e.billing_uncertain) from None


def _text_url(model: str, stream: bool = True) -> str:
    host = text_model_host(model, STATE.nai.text_host, STATE.nai.legacy_text_host)
    return f"{host}/ai/generate{'-stream' if stream else ''}"


def _require_text_model(key, model: str, kind: str) -> None:
    """原生文本入口只放行白名单模型，避免任意 model 字符串打到上游。"""
    if key["is_admin"] or model in TEXT_MODELS:
        return
    record(key, kind, model, "rejected", detail="model not allowed")
    raise err(400, "不支持的文本模型")


async def _text_quota_check(key, payload: dict) -> None:
    if key["is_admin"]:
        return
    c = await STATE.db.get_counter(key["id"], STATE.day())
    if key["daily_text_tokens"] >= 0 and c["text_tokens"] >= key["daily_text_tokens"]:
        raise err(402, f"已达今日文本额度（{key['daily_text_tokens']} tokens/天），明日再来吧")


# ============================================================== 图片生成 =====

async def wait_for_user_image_slot(key) -> None:
    if key["is_admin"]:
        return
    try:
        await STATE.wait_for_key_image_slot(key["id"])
    except TimeoutError:
        raise err(429, "图片任务排队超时，请稍后再试") from None


async def image_tool(request: Request, operation: str):
    key = await authenticate(request)
    check_image_cooldown()
    await check_rpm(key)
    await require_feature(key, "upscale" if operation == "upscale" else "augment")
    if not key["is_admin"] and not (key["allow_img2img"] and STATE.settings.allow_img2img):
        raise err(403, "此 Key 或本站未开放图片处理权限（img2img）")
    body = await read_image_payload(request)
    await wait_for_user_image_slot(key)
    async with acquire_concurrency(key, image=True):
        check_image_cooldown()
        try:
            payload, cost = await anyio.to_thread.run_sync(prepare_tool, body, operation)
        except ValueError as exc:
            raise err(400, str(exc)) from None
        model = payload.get("model", payload.get("req_type"))
        estimate = {"anlas": cost, "v5": 0}

        async def limited(retry_after):
            await STATE.block_image_generation(max(
                STATE.settings.image_429_cooldown_seconds, retry_after))

        async def perform():
            billing_uncertain = False
            try:
                resp = await upstream_call(
                    f"{STATE.nai.image_host}/ai/{operation}", payload,
                    on_rate_limited=limited, requires_anlas=cost > 0,
                    image_lane=True, max_response_bytes=MAX_RESPONSE_BYTES,
                )
                billing_uncertain = resp.status_code in (200, 201) or resp.status_code >= 500
                if resp.status_code not in (200, 201):
                    raise err(resp.status_code if 400 <= resp.status_code < 500 else 502,
                              f"图片工具请求失败（上游状态 {resp.status_code}），未记费")
                media, count = await anyio.to_thread.run_sync(
                    validate_result, resp.content, operation, payload.get("req_type", ""))
            except (GateError, httpx.HTTPError, ValueError, TimeoutError) as exc:
                billing_uncertain |= isinstance(exc, GateError) and exc.billing_uncertain
                await record(key, operation, model, "error",
                             detail=exc.message if isinstance(exc, GateError) else "图片工具连接失败或返回无效结果，未记费",
                             unconfirmed_anlas=cost if billing_uncertain else 0)
                if isinstance(exc, GateError):
                    raise
                raise err(502, "图片工具连接失败或返回无效结果，未记费；请勿自动重试") from None
            await settle_record(key, operation, model, "ok", images=count, anlas=cost,
                                detail=f"图片工具 {cost} Anlas；返回 {count} 张")
            return Response(resp.content, media_type=media, headers={"Cache-Control": "no-store"})

        async with reserve_image_budget(key, estimate):
            return await complete_image_operation(perform())


async def upscale_image(request: Request):
    return await image_tool(request, "upscale")


async def augment_image(request: Request):
    return await image_tool(request, "augment-image")

async def encode_vibe(request: Request):
    """Encode a V4/V4.5 reference; only a successful binary result costs 2 Anlas."""
    key = await authenticate(request)
    check_image_cooldown()
    await check_rpm(key)
    await require_feature(key, "vibe")
    body = await read_image_payload(request)
    # Accept Launcher's spelling as well as the existing Gate client field.
    if "information_extracted" in body:
        value = body.pop("information_extracted")
        if "informationExtracted" in body and body["informationExtracted"] != value:
            raise err(400, "两种 informationExtracted 字段的值不一致")
        body["informationExtracted"] = value
    problem = validate_vibe_encoding(body)
    if problem:
        raise err(400, problem)
    model = body["model"]
    payload = {"image": body["image"], "model": model,
               "information_extracted": body["informationExtracted"]}
    estimate = {"anlas": VIBE_ENCODING_ANLAS, "v5": 0}
    await quota_image_check(key, estimate)

    async def record_encoding_429(retry_after: float) -> None:
        # 回调更新冷却，日志由请求收尾时统一记录。
        await STATE.block_image_generation(max(
            STATE.settings.image_429_cooldown_seconds, retry_after
        ))

    async def perform_encoding():
        try:
            resp = await upstream_call(
                f"{STATE.nai.image_host}/ai/encode-vibe", payload,
                accept="application/octet-stream", on_rate_limited=record_encoding_429,
                requires_anlas=True, image_lane=True, max_response_bytes=MAX_RESPONSE_BYTES,
            )
        except (GateError, httpx.HTTPError, TimeoutError) as exc:
            await record(key, "vibe_encode", model, "error",
                         detail=exc.message if isinstance(exc, GateError) else "上游编码请求失败，未记费",
                         unconfirmed_anlas=VIBE_ENCODING_ANLAS
                         if isinstance(exc, GateError) and exc.billing_uncertain else 0)
            if isinstance(exc, GateError):
                raise
            raise err(502, "Vibe 编码连接失败，未记费；请勿自动重试") from None
        if resp.status_code not in (200, 201):
            await record(key, "vibe_encode", model, "error", detail=f"upstream {resp.status_code}",
                         unconfirmed_anlas=VIBE_ENCODING_ANLAS if resp.status_code >= 500 else 0)
            raise err(resp.status_code if 400 <= resp.status_code < 500 else 502,
                      f"Vibe 编码失败（上游状态 {resp.status_code}），未记费")
        content_type = resp.headers.get("content-type", "application/octet-stream").lower()
        if not resp.content or "json" in content_type or content_type.startswith("text/"):
            await record(key, "vibe_encode", model, "error", detail="上游未返回有效二进制编码，未记费",
                         unconfirmed_anlas=VIBE_ENCODING_ANLAS)
            raise err(502, "Vibe 编码未返回有效数据，未记费")
        await settle_record(key, "vibe_encode", model, "ok", anlas=VIBE_ENCODING_ANLAS,
                            detail=f"Vibe 编码 {VIBE_ENCODING_ANLAS} Anlas")
        return Response(resp.content, media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store"})

    await wait_for_user_image_slot(key)
    async with acquire_concurrency(key, image=True):
        check_image_cooldown()
        async with reserve_image_budget(key, estimate):
            return await complete_image_operation(perform_encoding())


async def generate_image(request: Request):
    return await _generate_image(request, streaming=False)


async def generate_image_stream(request: Request):
    return await _generate_image(request, streaming=True)


async def _generate_image(request: Request, *, streaming: bool):
    key = await authenticate(request)
    check_image_cooldown()
    await check_rpm(key)
    await require_feature(key, "image")
    body = await read_image_payload(request)
    request_timing.set_model(str(body.get("model") or ""))
    if not isinstance(body.get("parameters"), dict):
        raise err(400, "缺少 parameters 对象")
    try:
        normalize_image_request(body)
    except ValueError as exc:
        record(key, "image", str(body.get("model", "?"))[:80], "rejected", detail=str(exc))
        raise err(400, f"图片参数无效：{exc}") from None
    model = body["model"]
    fix_name_digit_weights(body)               # bm94199:: → bm94199 ::（否则上游当成权重 94199 → NaN）
    problem = upstream_parameter_problem(body)
    if problem:
        record(key, "image", model, "rejected", detail=problem[:120])
        raise err(400, problem)
    if any(body["parameters"].get(name) for name in REFERENCE_FIELDS):
        await require_feature(key, "vibe")      # 参考图 / Vibe 经由 generate-image 传入时同样受功能开关约束
    model_tier = image_model_tier(model)
    if model_tier is None:
        record(key, "image", model, "rejected", detail="未列入本站图片模型白名单")
        raise err(400, "不支持或尚未开放的图片模型")
    share = getattr(STATE, "share", None)
    if share is not None:
        try:                            # 防分享：出图习惯指纹（只在内存里保留哈希，见 share_guard.py）
            busy = bool(STATE.guard.image_inflight.get(key["id"], 0) > 0)
            await share.observe_habit(key, body, request_timing.snapshot().get("src", ""),
                                      request.headers.get("user-agent", ""), busy=busy, **_share_callbacks(request))
        except Exception as exc:
            bug("share_guard", exc)
    if model_tier == "v5" and not key["is_admin"] and key["image_model_scope"] != "all":
        record(key, "image", model, "rejected", detail="模型权限：仅允许 V4.5 及更低")
        raise err(403, "你的 Key 目前只能用 V4.5 及更低模型（V5 是全站共享的有限额度，暂时只开放给早期成员）。"
                       "请在客户端把模型换成 NAI Diffusion V4.5 再生成")

    # 图生图功能权限与费用分开判断；免费规格也沿用 Anlas 权限要求。
    if body.get("image") or body.get("mask"):
        raise err(400, "生图请求的 image 和 mask 请放在 parameters 中")
    problem = validate_image_references(body)
    if problem:
        raise err(400, problem)
    p0 = body.get("parameters", {}) or {}
    if (p0.get("image") or p0.get("mask")) and not key["is_admin"]:
        if not (key["allow_img2img"] and STATE.settings.allow_img2img):
            record(key, "image", model, "rejected", detail="img2img 未开放")
            raise err(400, "本站不支持图生图（img2img）/ 局部重绘（会消耗 Anlas）。请在客户端里移除参考图（原图）后再生成")
        if not key["allow_anlas"]:
            record(key, "image", model, "rejected", detail="img2img 未开通 Anlas 权限")
            raise err(402, "该 Key 未开通 Anlas 权限，无法使用图生图 / 局部重绘")

    # 免费档钳制：只对没有 Anlas 权限的 Key 生效；有 Anlas（含自动分配）的 Key 可以用超规格参数，按 Anlas 扣
    if STATE.settings.safe_clamp and not key["is_admin"] and not key["allow_anlas"]:
        try:
            body, notes, problem = clamp_image_params(
                body,
                max_pixels=STATE.settings.max_pixels,
                max_steps=STATE.settings.max_steps,
                allow_img2img=True,  # 权限已在上面预检
            )
        except (TypeError, ValueError, OverflowError):
            raise err(400, "图片参数无效") from None
        if problem:
            record(key, "image", model, "rejected", detail=problem)
            raise err(400, problem)
    else:
        notes = []
    body, more = fill_v4_prompt(body)          # V4 / V4.5 缺结构化提示词时上游回 500：照官方网页端补上
    notes = list(notes) + more
    if model_tier == "v5":
        if is_v5_medium(str(body.get("model", ""))):
            body, more = medium_normalize(body)     # 成员自己选 Medium 时也按官方规则整理，免得上游拒绝
            notes = list(notes) + more
        model = body["model"]

    p = body.get("parameters", {})
    try:
        image_count = int(p.get("n_samples", 1) or 1)
    except (TypeError, ValueError):
        raise err(400, "n_samples 必须是正整数")
    if image_count < 1:
        raise err(400, "n_samples 必须是正整数")

    try:
        est = estimate_image_cost(body, is_opus=True)
    except (TypeError, ValueError, OverflowError) as exc:
        raise err(400, "图片参数无效，无法估算费用") from exc
    # V5 续杯：自动分配了 Anlas 的成员，当天个人 V5 用完后改用 Anlas 生成同规格 V5（受每日 Anlas 限额约束）
    # 注意：账号 V5 额度还有剩余时，NovelAI 对免费规格 V5 扣的是 V5 额度而不是 Anlas。所以「续杯」只在账号 V5
    # 额度真的用完（≤ 1%）时才走 Anlas，否则会悄悄多用全站共享的 V5 额度、影响其他人。
    v5_left = (json.loads(await STATE.db.get_setting("quota_algo_last", "{}") or "{}").get("v5") or {}).get("percent")
    if (est["v5"] and _anlas_auto(key) and key["allow_anlas"] and key["daily_v5"] > 0
            and v5_left is not None and v5_left <= 1):
        if (await STATE.db.get_counter(key["id"], STATE.day()))["v5"] >= key["daily_v5"]:
            est = {**estimate_image_cost(body, is_opus=True, v5_allowance_available=False), "topup": True}
            notes = list(notes) + ["今日 V5 已用完，使用自动分配的 Anlas 续杯"]
    legacy_free_images = (
        1 if model_tier == "legacy" and legacy_normal_free_eligible(body) else 0
    )
    if not est["v5"]:
        await quota_image_check(key, est, legacy_free_images=legacy_free_images)

    cost = "; ".join(part for part in (
        f"est={est['anlas']}A" if est["anlas"] else "",
        f"V5额度+{est['v5']}" if est["v5"] else "",
    ) if part) or "免费"
    # 尺寸 / 步数 / 费用始终在前：钳制说明只追加在后面，不能顶掉记账信息
    detail = "; ".join([f"{p.get('width')}x{p.get('height')}/{p.get('steps')}step {cost}", *notes])

    reservation = None

    async def resolve_v5_cost(exhausted: bool):
        nonlocal est, detail
        if exhausted and not key["allow_anlas"]:
            # 账号的 V5 免费额度刚用完：直说原因，不要报成「你的 Key 没开通付费权限」（审查 P2）
            raise err(402, "账号今天的 V5 免费额度刚用完（不是你的个人额度）。可以先改用 V4.5，额度恢复后再用 V5", reasons.V5_EXHAUSTED)
        updated = estimate_image_cost(body, is_opus=True, v5_allowance_available=not exhausted)
        await reservation.update(key, updated, legacy_free_images)
        est = updated
        if exhausted:
            detail = "; ".join(notes + [
                f"{p.get('width')}x{p.get('height')}/{p.get('steps')}step est={est['anlas']}A",
                "官方确认 V5 额度不可用，按 Anlas 估算记账"])

    async def record_image_429(retry_after: float) -> None:
        # 两种响应均在收尾时记日志，回调只更新冷却。
        await STATE.block_image_generation(max(
            STATE.settings.image_429_cooldown_seconds, retry_after
        ))

    async def perform_generation():
        try:
            resp = await upstream_call(
                f"{STATE.nai.image_host}/ai/generate-image", body,
                on_rate_limited=record_image_429,
                requires_anlas=est["anlas"] > 0,
                v5_free=est["v5"] > 0,
                image_count=image_count,
                # Retain the status to distinguish rejected requests from uncertain charges.
                image_lane=True, max_response_bytes=MAX_RESPONSE_BYTES,
                resolve_v5_cost=resolve_v5_cost if est["v5"] else None,
            )
        except GateError as exc:
            if not request_timing.was_sent() and not exc.billing_uncertain:
                # 还没发到上游就被本地拒绝：记「拒绝」+ 原因码，不算上游失败（审查 F6）
                await record(key, "image", model, "rejected", detail=f"{exc.status} {exc.message}"[:160], reason=exc.code)
                raise
            await record(key, "image", model, "error", detail=exc.message,
                         unconfirmed_anlas=est["anlas"] if exc.billing_uncertain else 0)
            await audit_generation(key, "image", model, "error", body)
            upstream_outcome(False)
            raise
        if resp.status_code not in (200, 201):
            await record(key, "image", model, "error", detail=f"upstream {resp.status_code}",
                         unconfirmed_anlas=est["anlas"] if resp.status_code >= 500 else 0)
            await audit_generation(key, "image", model, "error", body)
            upstream_outcome(resp.status_code < 500)
            if resp.status_code >= 500:
                notify_owner("upstream_5xx", f"NovelAI 图片接口返回 {resp.status_code}，上游可能故障。", 1800)
            raise err(resp.status_code, upstream_error_message(resp.status_code))
        await settle_record(key, "image", model, "ok", images=image_count, anlas=est["anlas"],
                            v5=est["v5"], legacy_free_images=legacy_free_images,
                            detail=detail)
        schedule_audit(key, "image", model, "ok", body, resp.content)
        upstream_outcome(True)
        return Response(resp.content, status_code=200,
                        media_type=resp.headers.get("content-type", "application/octet-stream"))

    if streaming:
        # Existing panels default to SSE; Launcher explicitly requests MessagePack.
        wire_format = p.get("stream", "sse")
        if wire_format not in ("sse", "msgpack"):
            raise err(400, "stream 仅支持 sse 或 msgpack")
        body.setdefault("parameters", {})["stream"] = wire_format
        dispatched = False
        delivered = False          # 全部最终图都已推给客户端（之后客户端断开不算「没拿到图」）

        def on_dispatch():
            nonlocal dispatched
            dispatched = True

        async def perform_stream(response):
            nonlocal delivered
            local_reject = False
            tracker = ImageEventTracker(image_count, wire_format)
            failure = None
            billing_uncertain = False
            try:
                # Bound total drain time even when an upstream sends endless
                # progress frames that would keep resetting its read timeout.
                async with asyncio.timeout(300):
                    async with STATE.nai.image_stream(
                        f"{STATE.nai.image_host}/ai/generate-image-stream", body,
                        requires_anlas=est["anlas"] > 0, v5_free=est["v5"] > 0,
                        on_rate_limited=record_image_429,
                        on_dispatch=on_dispatch,
                        queue_timeout=STATE.settings.queue_timeout,
                        before_dispatch=check_image_cooldown,
                        resolve_v5_cost=resolve_v5_cost if est["v5"] else None,
                    ) as handle:
                        billing_uncertain = True
                        try:
                            content_type = handle.response.headers.get("content-type", "")
                            media_type = content_type.split(";", 1)[0].strip().lower()
                            allowed = {STREAM_MEDIA_TYPES[wire_format]}
                            if wire_format == "msgpack":
                                # The endpoint's Swagger still advertises SSE;
                                # validate binary framing even under that header.
                                allowed.update(("application/msgpack", "application/octet-stream",
                                                "text/event-stream"))
                            if media_type not in allowed:
                                raise UpstreamError(502, "上游未返回有效的图片事件流")
                            await response.start()
                            async for chunk in handle.response.aiter_bytes():
                                # Never forward part of an error event before it
                                # has been identified, even across network chunks.
                                for frame in tracker.frames(chunk):
                                    handle.completed_images = tracker.completed_images
                                    await response.chunk(frame)
                                if tracker.failed:
                                    raise UpstreamError(502, "上游流式生成失败")
                                if tracker.completed_images == image_count:
                                    delivered = True
                                    break
                            tracker.finish()
                            if tracker.completed_images < image_count:
                                raise UpstreamError(502, "图片流提前结束，未收到全部最终图片")
                        finally:
                            handle.completed_images = tracker.completed_images
            except (UpstreamError, ImageStreamProtocolError, httpx.HTTPError, TimeoutError) as exc:
                billing_uncertain |= (isinstance(exc, UpstreamError) and exc.billing_uncertain
                                      or isinstance(exc, TimeoutError) and dispatched)
                status = exc.status if isinstance(exc, UpstreamError) else 502
                message = exc.message if isinstance(exc, UpstreamError) else (
                    str(exc) if isinstance(exc, ImageStreamProtocolError) else "图片流连接中断或超时")
                # 上游 error 事件的原话只进日志，不转发给客户端（可能含上游内部信息）
                failure = message + (f"（上游：{tracker.error_detail}）" if tracker.error_detail else "")
                await response.error(status, message)
            except GateError:
                # 还没发到上游就被本地拒绝（冷却 / V5 用完等）：由 run_stream 记一条「拒绝」，
                # 这里不能再记「上游出错」，也不能算进上游失败率（2026-10-10 审查 F6）
                local_reject = not dispatched
                raise
            finally:
                completed = tracker.completed_images
                if not dispatched and not completed and failure is None and not local_reject:
                    # 还在排队时客户端就断开了：没发到上游，不能记成「上游出错」，也不能拉低上游健康度
                    await record(key, "image_stream", model, "cancelled", detail="排队时客户端断开（未发到上游，未记费）")
                elif not (local_reject and not completed):      # 本地拒绝且没出图：交给 run_stream 记「拒绝」
                    settled_anlas = 0
                    if completed:
                        # 按完整结果重算首张减免，沿用派发前确认的 V5 额度状态。
                        completed_body = {**body, "parameters": {**p, "n_samples": completed}}
                        settled = estimate_image_cost(
                            completed_body, v5_allowance_available=bool(est["v5"]))
                        settled_anlas = settled["anlas"]
                        await settle_record(
                            key, "image_stream", model, "ok", images=completed,
                            anlas=settled["anlas"], v5=settled["v5"],
                            legacy_free_images=legacy_free_images,
                            detail=detail + (f"; 完成 {completed}/{image_count}" if completed < image_count else ""),
                        )
                    await audit_generation(key, "image_stream", model, "ok" if completed and not failure else "error", body,
                                           tracker.first_image if completed else None)
                    upstream_outcome(bool(completed) and not failure)
                    if failure or not completed:
                        # 未结算部分单独记为待核对费用。
                        await record(key, "image_stream", model, "error",
                                     detail=failure or "未收到最终图片，未记费",
                                     unconfirmed_anlas=max(0, est["anlas"]-settled_anlas)
                                     if billing_uncertain else 0)

        async def run_stream(response):
            try:
                await wait_for_user_image_slot(key)
                async with acquire_concurrency(key, image=True):
                    check_image_cooldown()
                    async with reserve_image_budget(key, est, legacy_free_images=legacy_free_images) as reserved:
                        nonlocal reservation
                        reservation = reserved
                        await complete_image_operation(perform_stream(response), can_cancel=lambda: not dispatched,
                                                       delivered=lambda: delivered)
            except GateError as exc:
                # 和非流式一样记「拒绝」：自动驾驶（节约模式 / Key 限流）、AIMD、每日微调都靠这些记录感知拥挤
                record(key, "image_stream", model, "rejected", detail=f"{exc.status} {exc.message}"[:160], reason=exc.code)
                await response.error(exc.status, exc.message)

        return ImageStreamResponse(run_stream, wire_format)

    await wait_for_user_image_slot(key)
    async with acquire_concurrency(key, image=True):
        # Recheck both cooldown and quota after any queue/budget wait.
        check_image_cooldown()
        async with reserve_image_budget(key, est, legacy_free_images=legacy_free_images) as reservation:
            return await complete_image_operation(perform_generation())


async def suggest_tags(request: Request):
    key = await authenticate(request)
    check_image_cooldown()
    await require_feature(key, "tags")
    admitted = False
    try:
        # Bound body reads, semaphore waiting and the optional upstream lookup.
        async with asyncio.timeout(STATE.settings.queue_timeout):
            admitted = await STATE.wait_for_tag_request(key["id"])
            if admitted is None:              # 已被同一 Key 更新的补全查询取代：回空结果，不记拒绝
                return JSONResponse({"tags": []})
            if not admitted:
                raise err(429, "补全查询排队人数过多，请稍后再试")
            return await _suggest_tags(request, key)
    except TimeoutError:
        record(key, "tags", "", "error", detail="补全查询排队或请求超时")
        raise err(429, "补全查询等待超时，请稍后再试") from None
    finally:
        if admitted:
            await STATE.finish_tag_request(key["id"])


async def _suggest_tags(request: Request, key):
    if request.method == "GET":
        body = {
            "prompt": request.query_params.get("prompt", ""),
            "model": request.query_params.get("model", ""),
        }
    else:
        body = await read_json(request, limit_mb=0.0625)

    async def record_tag_429(retry_after: float) -> None:
        cooldown = await STATE.block_image_generation(max(
            STATE.settings.image_429_cooldown_seconds, retry_after
        ))
        record(
            key, "tags", "", "error",
            detail=(f"上游限流(429)：全站图片生成暂停约 {cooldown} 秒，"
                    "保护上游 Token"),
        )

    async with acquire_concurrency(key):
        if await request.is_disconnected():
            _REQUEST_LOGGED.set(True); request_timing.mark_logged()         # 客户端已经换了新的查询，不算拒绝
            raise err(499, "补全查询已取消")
        check_image_cooldown()
        query = urlencode({
            "prompt": str(body.get("prompt", "") or ""),
            "model": str(body.get("model", "") or ""),
        })
        try:
            resp = await STATE.nai.request(
                "GET", f"{STATE.nai.image_host}/ai/generate-image/suggest-tags?{query}",
                accept="application/json", on_rate_limited=record_tag_429,
                image_lane=True, wait_for_image_slot=False)
        except UpstreamError as exc:
            if exc.status != 429:  # 真实上游 429 已由 record_tag_429 记录。
                record(key, "tags", "", "error", detail=exc.message)
            raise err(exc.status if exc.status in (429, 503) else 502, exc.message)
    if resp.status_code != 200:
        record(key, "tags", "", "error", detail=f"上游标签接口返回 HTTP {resp.status_code}")
        raise err(resp.status_code, upstream_error_message(resp.status_code))
    record(key, "tags", "", "ok")
    return Response(resp.content, media_type="application/json")


# ============================================================== 文本生成 =====

class TextStreamResponse(StreamingResponse):
    """Transfer the route's upstream/slot ownership to the ASGI response."""

    def __init__(self, content, resources: AsyncExitStack, **kwargs):
        super().__init__(content, **kwargs)
        self.resources = resources.pop_all()

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            async def cleanup():
                try:
                    # Also covers send failure while the generator is suspended.
                    await self.body_iterator.aclose()
                finally:
                    # Close upstream before releasing either concurrency slot,
                    # even if response headers failed before iteration started.
                    await self.resources.aclose()

            await _wait_cleanup(asyncio.create_task(cleanup()))


async def generate_stream(request: Request):
    key = await authenticate(request)
    await check_rpm(key)
    await require_feature(key, "text")
    body = await read_json(request, limit_mb=TEXT_BODY_MB)
    model = str(body.get("model", "?"))
    _require_text_model(key, model, "text")

    body, notes, problem = clamp_text_params(
        body,
        max_output_tokens=STATE.settings.max_text_output_tokens,
        max_input_chars=STATE.settings.max_input_chars,
    )
    if problem:
        record(key, "text", model, "rejected", detail=problem)
        raise err(400, problem)
    await _text_quota_check(key, body)

    daily_limit = -1 if key["is_admin"] else key["daily_text_tokens"]

    async with AsyncExitStack() as resources:
        await resources.enter_async_context(acquire_concurrency(key))
        # 拿到并发槽之后再读已用量，排队的请求才不会共用一个过期的计数。
        already = (await STATE.db.get_counter(key["id"], STATE.day()))["text_tokens"]
        try:
            resp = await STATE.nai.stream(_text_url(model, True), body)
        except UpstreamError as e:
            record(key, "text", model, "error", detail=e.message)
            raise err(e.status if e.status in (400, 422, 429, 503) else 502, e.message)
        resources.push_async_callback(resp.aclose)

        async def passthrough() -> AsyncIterator[bytes]:
            counted = 0
            hard_cut = False
            try:
                async for raw, event, payload in text_stream_events(resp):
                    if payload is not None:
                        counted += 1
                        if 0 <= daily_limit <= already + counted:
                            hard_cut = True
                            yield b"data: [DONE]\n\n"
                            return
                    yield encode_sse(raw, event)
            except UpstreamError as exc:
                record(key, "text", model, "error", detail=exc.message)
                yield encode_sse(json.dumps({"error": exc.message, "message": exc.message,
                                             "status_code": exc.status}, ensure_ascii=False).encode(), b"error")
            finally:
                d = ("达到每日上限被截断; " if hard_cut else "") + "; ".join(notes)
                if counted:
                    await settle_record(key, "text", model, "ok", tokens=counted, detail=d.strip("; "))

        return TextStreamResponse(passthrough(), resources, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})


async def generate_text(request: Request):
    key = await authenticate(request)
    await check_rpm(key)
    await require_feature(key, "text")
    body = await read_json(request, limit_mb=TEXT_BODY_MB)
    model = str(body.get("model", "?"))
    _require_text_model(key, model, "text")

    body, notes, problem = clamp_text_params(
        body,
        max_output_tokens=STATE.settings.max_text_output_tokens,
        max_input_chars=STATE.settings.max_input_chars,
    )
    if problem:
        record(key, "text", model, "rejected", detail=problem)
        raise err(400, problem)
    await _text_quota_check(key, body)

    async with acquire_concurrency(key):
        resp = await upstream_call(_text_url(model, False), body, accept="application/json")
    if resp.status_code != 200:
        record(key, "text", model, "error", detail=f"upstream {resp.status_code}")
        raise err(resp.status_code, upstream_error_message(resp.status_code))
    try:
        out = (resp.json() or {}).get("output", "")
    except Exception:
        out = ""
    record(key, "text", model, "ok", tokens=estimate_tokens(out), detail="; ".join(notes))
    return Response(resp.content, media_type="application/json")


async def generate_voice(request: Request):
    key = await authenticate(request)
    await check_rpm(key)
    await require_feature(key, "voice")
    if not key["is_admin"]:
        # 语音合成按 Anlas 计费，但目前没有接入额度与预算统计，因此仅管理员 Key 可用。
        record(key, "voice", "", "rejected", detail="voice is admin-only")
        raise err(403, "语音合成目前仅限管理员 Key（它按 Anlas 计费，暂未接入额度统计）")
    body = await read_json(request, limit_mb=1)
    async with acquire_concurrency(key):
        resp = await upstream_call(f"{STATE.nai.legacy_text_host}/ai/generate-voice", body)
    if resp.status_code != 200:
        raise err(resp.status_code, upstream_error_message(resp.status_code))
    record(key, "voice", str(body.get("voice", "")), "ok")
    return Response(resp.content, media_type=resp.headers.get("content-type", "audio/mpeg"))


# ====================================================== OpenAI 兼容桥(文本) ====

TEXT_MODELS = [
    "llama-3-erato-v1", "kayra-v1", "clio-v1",
    "nai-glm-4-6", "nai-xialong",
]


async def v1_models(request: Request):
    return {"object": "list", "data": [
        {"id": m, "object": "model", "owned_by": "novelai"} for m in TEXT_MODELS
    ]}


async def v1_me(request: Request):
    key = await authenticate(request)
    c = await STATE.db.get_counter(key["id"], STATE.day())
    generated_images = await STATE.db.generated_image_totals(key["id"])
    return {
        "name": key["name"],
        "is_admin": bool(key["is_admin"]),
        "generated_images_total": generated_images.get(key["id"], 0),
        "today": {
            "images": c["images"], "daily_images": key["daily_images"],
            "daily_images_base": daily_base(key),
            "legacy_free_images_today": c["legacy_free_images"],
            "anlas_today": round(float(c["anlas"]), 2),
            "daily_anlas": key["daily_anlas"],
            "anlas_auto": _anlas_auto(key) and bool(key["allow_anlas"]),
            "v5_today": c["v5"], "daily_v5": key["daily_v5"],
            "image_model_scope": key["image_model_scope"],
            "anlas_month": round(await STATE.db.month_anlas(key["id"], STATE.month()), 2),
            "monthly_anlas": key["monthly_anlas"],
            "text_tokens": c["text_tokens"], "daily_text_tokens": key["daily_text_tokens"],
            "requests": c["requests"],
        },
        "expires_at": key["expires_at"],
        # 闲置回收规则对成员可见：N 天内没有任何请求的 Key 会被自动删除（管理员 Key 除外）。
        "inactivity_delete_days": 0 if key["is_admin"] else STATE.settings.key_inactivity_delete_days,
        "features": await _feature_view(key),
    }


async def _feature_view(key) -> list[dict]:
    """该 Key 当前各项功能是否可用（已计入全局开关）。"""
    view = []
    for name, label in features.FEATURES.items():
        on = await features.check(STATE.db, key, name) is None
        if name == "voice" and not key["is_admin"]:
            on = False                  # 语音合成目前仅管理员 Key 可用（见 generate_voice）
        view.append({"id": name, "label": label, "on": on})
    return view


async def v1_chat(request: Request):
    key = await authenticate(request)
    await check_rpm(key)
    await require_feature(key, "text")
    body = await read_json(request, limit_mb=TEXT_BODY_MB)
    want_stream = bool(body.get("stream"))

    msgs = body.get("messages") or []
    input_text = "\n".join(
        str(m.get("content", "")) for m in msgs if isinstance(m, dict)
    ).strip()
    model_in = str(body.get("model", ""))
    model = model_in if model_in in TEXT_MODELS else "llama-3-erato-v1"
    ml = int(body.get("max_tokens") or 150)
    payload = {
        "input": input_text,
        "model": model,
        "parameters": {
            "max_length": min(max(1, ml), STATE.settings.max_text_output_tokens),
            "min_length": 1,
            "temperature": float(body.get("temperature", 0.9) or 0.9),
            "top_p": float(body.get("top_p", 0.9) or 0.9),
            "top_k": 40,
            # 上游默认把 input 当 base64 token 解析；OpenAI 兼容入口传的是明文。
            "use_string": True,
        },
    }
    nai_body, _, problem = clamp_text_params(
        payload,
        max_output_tokens=STATE.settings.max_text_output_tokens,
        max_input_chars=STATE.settings.max_input_chars,
    )
    if problem:
        raise err(400, problem)
    await _text_quota_check(key, nai_body)

    daily_limit = -1 if key["is_admin"] else key["daily_text_tokens"]

    async with AsyncExitStack() as resources:
        await resources.enter_async_context(acquire_concurrency(key))
        # 拿到并发槽之后再读已用量，排队的请求才不会共用一个过期的计数。
        already = (await STATE.db.get_counter(key["id"], STATE.day()))["text_tokens"]
        try:
            resp = await STATE.nai.stream(_text_url(model, True), nai_body)
        except UpstreamError as e:
            record(key, "chat", model, "error", detail=e.message)
            raise err(e.status if e.status in (400, 422, 429, 503) else 502, e.message)
        resources.push_async_callback(resp.aclose)

        def openai_chunk(content: str, finish: Optional[str] = None) -> str:
            return json.dumps({
                "id": "chatcmpl-naigate", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": model,
                "choices": [{"index": 0,
                             "delta": {"content": content} if content else {},
                             "finish_reason": finish}],
            }, ensure_ascii=False)

        result: dict[str, Any] = {"collected": [], "counted": 0}

        async def run_stream() -> AsyncIterator[bytes]:
            """消费 NAI SSE；流式时产出 OpenAI chunk，非流式时只收集文本。"""
            try:
                if want_stream:
                    yield ("data: " + openai_chunk("", None) + "\n\n").encode()
                async for _raw, _event, obj in text_stream_events(resp):
                    if obj is None:
                        break
                    text = obj["token"]
                    if not text:
                        continue
                    result["counted"] += 1
                    if 0 <= daily_limit <= already + result["counted"]:
                        break
                    if want_stream:
                        yield ("data: " + openai_chunk(text, None) + "\n\n").encode()
                    else:
                        result["collected"].append(text)
                if want_stream:
                    yield ("data: " + openai_chunk("", "stop") + "\n\n").encode()
                    yield b"data: [DONE]\n\n"
            except UpstreamError as exc:
                record(key, "chat", model, "error", detail=exc.message)
                if not want_stream:
                    raise err(exc.status, exc.message) from None
                yield encode_sse(json.dumps({"error": {"message": exc.message, "status": exc.status}},
                                             ensure_ascii=False).encode())
                yield b"data: [DONE]\n\n"
            finally:
                if result["counted"]:
                    await settle_record(key, "chat", model, "ok", tokens=result["counted"])

        if want_stream:
            return TextStreamResponse(run_stream(), resources, media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache",
                                              "X-Accel-Buffering": "no"})

        async for _ in run_stream():
            pass
        content = "".join(result["collected"])
        counted = result["counted"]
        return JSONResponse({
            "id": "chatcmpl-naigate", "object": "chat.completion",
            "created": int(time.time()), "model": model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": estimate_tokens(input_text),
                      "completion_tokens": max(counted, 1),
                      "total_tokens": estimate_tokens(input_text) + max(counted, 1)},
        })


# ================================================================ 页面 =======

@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": __version__, "upstream": STATE.nai.configured if STATE else False}


# 公告是管理员存储的 HTML：沙箱化后脚本无法执行，也读不到与 /admin 同源的数据。
_ANNOUNCEMENT_HEADERS = {"Content-Security-Policy": "sandbox allow-popups allow-popups-to-escape-sandbox", "X-Content-Type-Options": "nosniff"}


LANDING_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                # 头像来自 Discord CDN（以前被挡，成员看到的是空白圆）
                                "connect-src 'self'; img-src 'self' data: https://cdn.discordapp.com; frame-src 'self'; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-cache",
}


@app.get("/")
async def index():
    """成员落地页：自包含的静态页面，数据来自 /public/status 与 /v1/me。"""
    return FileResponse(Path(__file__).parent / "static" / "landing.html", headers=LANDING_HEADERS)


_FAVICON = (Path(__file__).parent / "static" / "favicon.svg").read_bytes()


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
async def favicon():
    """🦉 站点图标；/favicon.ico 也返回 SVG，现代浏览器都能识别，避免日志里出现 404。"""
    return Response(_FAVICON, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})


@app.get("/announcement")
async def announcement():
    """站长在后台编写的公告 HTML：沙箱化后嵌入落地页，其中脚本不会执行。"""
    p = SETTINGS.announcement_path
    html = p.read_text(encoding="utf-8") if p.exists() else ""
    if not html.strip():
        raise HTTPException(404, "没有公告")
    return Response(html, media_type="text/html", headers=_ANNOUNCEMENT_HEADERS)


_PUBLIC_STATUS_CACHE: dict = {"at": 0.0, "body": None}


@app.get("/public/live")
async def public_live(request: Request):
    """首页实时架构图：每秒轮询，只含汇总数字（请求数、额度、排队数、账号保护、名额与打码的候补名单）。"""
    body = await live.build(STATE, getattr(request.app.state, "registrar", None))
    # 登录的成员：附上「我的排队」，看板第一格直接显示排第几（不用再粘贴 Key）
    # 注意：live.build() 返回的是全站共享的缓存字典（1 秒内复用），绝不能就地改它——
    # 否则两个并发请求会在 await 处交错，把 A 的「我的排队」泄给没 Key 的 B。
    # 先把成员数据算好，再用浅拷贝拼成本次响应，保证按人隔离。
    from .registration_routes import _member_session
    discord_id = await _member_session(request)
    me = None
    if discord_id:
        reg = getattr(request.app.state, "registrar", None)
        key = await reg.key_row_for(discord_id) if reg is not None else None
        if key is not None and key["enabled"]:
            me = live.mine(STATE, key["id"])
    return JSONResponse({**body, "me": me}, headers={"Cache-Control": "no-store"})


_CLIENT_ERR: dict = {"window": 0.0, "total": 0, "ip": {}}


def _injected_script_error(msg: str, stack: str, host: str = "") -> bool:
    """手机自带浏览器（荣耀 / 华为 / 小米等）、翻译和广告插件往页面注入的脚本出的错：我们修不了，不进 Bug 追踪。
    特征：堆栈里有调用帧，但没有一帧来自本站文件（全是 <anonymous> / 扩展协议）；或者是没有来源的「Script error.」。"""
    if msg.strip() == "Script error.":
        return True
    frames = [ln.strip() for ln in stack.splitlines() if ln.strip().startswith("at ") or "@" in ln]
    if not frames:
        return False
    ours = tuple(x for x in ("/static/", "/admin", host, "127.0.0.1", "localhost") if x)
    return not any(any(o in f for o in ours) for f in frames)


@app.post("/public/client-error")
async def client_error(request: Request):
    """首页 / 后台网页的脚本报错上报（只收本站页面的错误；每 IP 每 10 分钟 10 条，全站每 10 分钟 100 条）。"""
    # 只收本站页面的上报：Origin/Referer 必须与访问的 Host 同源，挡掉外站往 Bug 面板灌垃圾
    _ref = request.headers.get("origin") or request.headers.get("referer") or ""
    if _ref:
        _oh = urlsplit(_ref).netloc.lower()
        _host = (request.headers.get("host") or "").lower()
        if _oh and _host and _oh != _host:
            return Response(status_code=204)
    now = time.time()
    if now - _CLIENT_ERR["window"] > 600:
        _CLIENT_ERR.update(window=now, total=0, ip={})
    ip = request.client.host if request.client else "?"
    if _CLIENT_ERR["total"] >= 100 or _CLIENT_ERR["ip"].get(ip, 0) >= 10:
        return Response(status_code=204)
    _CLIENT_ERR["total"] += 1
    _CLIENT_ERR["ip"][ip] = _CLIENT_ERR["ip"].get(ip, 0) + 1
    try:
        data = json.loads((await request.body())[:4096] or b"{}")
    except ValueError:
        return Response(status_code=204)
    if not isinstance(data, dict):
        return Response(status_code=204)
    page = str(data.get("page") or "")[:20]
    page = page if page in ("landing", "admin") else "other"
    msg = request_timing.client_name(str(data.get("msg") or ""))[:200] or "unknown"
    where = request_timing.client_name(f"{data.get('src') or ''}:{data.get('line') or ''}")
    stack = str(data.get("stack") or "")[:1500]
    if _injected_script_error(msg, stack, (request.headers.get("host") or "").lower()):
        return Response(status_code=204)
    ua = request_timing.client_name(request.headers.get("user-agent", ""))
    bug(f"web:{page}", title=msg, detail=f"{where}\n{ua}\n{stack}", path=page, level="warn")
    return Response(status_code=204)


@app.get("/v1/live/me")
async def live_me(request: Request):
    """需要 Key：这把 Key 的图现在排第几 / 是否在生成。不写用量日志。"""
    try:
        key = await authenticate(request, passive=True)
    except GateError as exc:      # 直接返回，不写拒绝日志（页面每秒都会问一次）
        return JSONResponse({"mine": None, "error": exc.message}, status_code=exc.status,
                            headers={"Cache-Control": "no-store"})
    return JSONResponse(live.mine(STATE, key["id"]), headers={"Cache-Control": "no-store"})


@app.get("/public/status")
async def public_status(request: Request):
    """落地页使用的公开状态：不含任何密钥、成员信息或计数细节。缓存 8 秒，避免匿名请求放大数据库压力。"""
    now = time.monotonic()
    if _PUBLIC_STATUS_CACHE["body"] is not None and now - _PUBLIC_STATUS_CACHE["at"] < 8:
        return JSONResponse(_PUBLIC_STATUS_CACHE["body"], headers={"Cache-Control": "no-store"})
    body = await _public_status_body(request)
    _PUBLIC_STATUS_CACHE.update(at=now, body=body)
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


def _public_limits() -> dict:
    """首页「使用须知」里的排队与保底规则；数值来自后台「账号保护与排队」。"""
    guard = getattr(STATE, "guard", None)
    if guard is None:
        return {}
    v = guard.values
    return {"key_image_queue": v["key_image_queue"], "base_daily_images": v["base_daily_images"],
            "quiet_start": v["quiet_start"], "quiet_end": v["quiet_end"],
            "quiet": v["quiet_start"] != v["quiet_end"]}


async def _image_stability() -> dict:
    getter = getattr(STATE.db, "image_stability", None)
    if getter is None:
        return {"ok": 0, "error": 0}
    try:
        return await getter(time.time() - 24 * 3600)
    except Exception:
        return {"ok": 0, "error": 0}


async def _public_status_body(request: Request) -> dict:
    from . import features as feature_defs
    service = getattr(request.app.state, "registrar", None)
    flags = await feature_defs.global_flags(STATE.db)
    reg = {"open": False, "slots_left": None}
    # 未配置 Discord 注册时，新 Key 由后台创建，默认同样只开文生图。语音仅管理员可用，不对成员展示。
    defaults = ["image"] if flags.get("image") else []
    if service is not None:
        cfg = await service.settings()
        reg["open"] = cfg["open"]
        if cfg["max_users"]:
            reg["slots_left"] = max(0, cfg["max_users"] - await service.count_active())
        names = feature_defs.FEATURES if cfg["features"] is None else cfg["features"]
        defaults = [n for n in names if flags.get(n) and n != "voice"]
    p = SETTINGS.announcement_path
    return {
        "site": SETTINGS.site_url.rstrip("/"),
        "upstream": {k: v for k, v in STATE.upstream_health().items() if k in ("status", "image_cooldown_seconds")},
        "registration": reg,
        "default_features": [{"id": n, "label": feature_defs.FEATURES[n]} for n in defaults],
        "audit_notice": await audit_disclosure(STATE.db, SETTINGS),
        # 网页 Discord 登录：Discord 应用审核期间 OAuth 被封，暂停时首页改为提示用 /register、/quota（站长可随时改回 0）
        "web_login": not await site_flags.get(STATE.db, site_flags.WEB_LOGIN_PAUSED),   # 默认暂停（fail-closed）
        "algo_notice": (await site_flags.get(STATE.db, site_flags.ALGO_NOTICE))[:300],
        "discord_invite": SETTINGS.discord_invite_url,
        "key_inactivity_delete_days": SETTINGS.key_inactivity_delete_days,
        "limits": _public_limits(),
        # 正在处理的出图任务数（含正在生成的那一个）；全站串行出图，成员据此估计等待时间
        "image_jobs": len(getattr(STATE, "image_reservations", {}) or {}),
        "stability": await _image_stability(),
        "has_announcement": bool(p.exists() and p.read_text(encoding="utf-8").strip()),
    }


ADMIN_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data: https://cdn.discordapp.com; frame-src 'self'; object-src 'none'; "
                                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
}


@app.get("/admin")
async def admin_page():
    return FileResponse(Path(__file__).parent / "static" / "index.html", headers=ADMIN_HEADERS)


@app.get("/user/subscription")
async def user_subscription(request: Request):
    """给原生 NAI 客户端的虚拟订阅视图，不暴露真实上游账户余额。"""
    key = await authenticate(request)
    return await subscription_payload(STATE, key)


@app.get("/user/data")
async def user_data(request: Request):
    key = await authenticate(request)
    sub = await subscription_payload(STATE, key)
    public_sub = {name: value for name, value in sub.items() if name != "naiGate"}
    return {"subscription": public_sub, "trainingStepsLeft": sub["trainingStepsLeft"],
            "anlas": sub["trainingStepsLeft"]["fixedTrainingStepsLeft"]}


@app.get("/user/information")
async def user_information(request: Request):
    key = await authenticate(request)
    return {"tier": 3, "active": True, "username": key["name"],
            "expiresAt": int(key["expires_at"] or time.time() + 3650 * 86400)}


@app.get("/queue-status")
async def queue_status():
    return STATE.queue_snapshot()


app.get("/ai/user/subscription")(user_subscription)
app.get("/ai/user/data")(user_data)
app.get("/ai/user/information")(user_information)


# ================================================================ 路由注册 ====

for path in ("/ai/generate-image", "/nai/ai/generate-image"):
    app.post(path)(limit_inflight(generate_image))
for path in ("/ai/encode-vibe", "/nai/ai/encode-vibe"):
    app.post(path)(limit_inflight(encode_vibe))
for path in ("/ai/upscale", "/nai/ai/upscale"):
    app.post(path)(limit_inflight(upscale_image))
for path in ("/ai/augment-image", "/nai/ai/augment-image"):
    app.post(path)(limit_inflight(augment_image))
for path in ("/ai/generate-image/suggest-tags", "/nai/ai/generate-image/suggest-tags"):
    app.post(path)(suggest_tags)
    app.get(path)(suggest_tags)
for path in ("/ai/generate-image-stream", "/nai/ai/generate-image-stream"):
    app.post(path)(limit_inflight(generate_image_stream))
for path in ("/ai/generate-stream", "/nai/ai/generate-stream"):
    app.post(path)(limit_inflight(generate_stream))
for path in ("/ai/generate", "/nai/ai/generate"):
    app.post(path)(limit_inflight(generate_text))
for path in ("/ai/generate-voice", "/nai/ai/generate-voice"):
    app.post(path)(generate_voice)
app.get("/v1/models")(v1_models)
app.get("/v1")(v1_models)          # 部分客户端“测试连接”会直接请求填写的 Base URL（…/v1）
app.post("/v1/chat/completions")(limit_inflight(v1_chat))
app.get("/v1/me")(v1_me)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SETTINGS.host, port=SETTINGS.port)
