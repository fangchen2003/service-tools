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
        # 关停时还在跑的审计与 Bug 写库任务先写完再关库（10/10 22:55 部署时一条审计写在关库之后丢失）
        if _AUDIT_TASKS:
            await asyncio.wait(list(_AUDIT_TASKS), timeout=10)
        if getattr(STATE, "bugs", None):
            try:
                await asyncio.wait_for(STATE.bugs.drain(), timeout=5)
            except Exception:
                pass
        await STATE.db.close()


__version__ = "2.22.1"

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


# 当前请求已认出的 Key，以及本请求是否已写过用量日志；统一收口于 app.core.context 单一源
from .core.context import REQUEST_KEY as _REQUEST_KEY, REQUEST_LOGGED as _REQUEST_LOGGED

_PATH_KIND = (("generate-image-stream", "image_stream"), ("suggest-tags", "tags"), ("generate-image", "image"),
              ("encode-vibe", "vibe_encode"), ("upscale", "upscale"), ("augment-image", "augment-image"),
              ("generate-voice", "voice"), ("generate-stream", "text"), ("/ai/generate", "text"),
              ("/v1/chat/completions", "chat"))


def _kind_for_path(path: str) -> str:
    return next((kind for needle, kind in _PATH_KIND if needle in path), "account")


def _row_flag(row, name: str) -> bool:
    try:
        return bool(row[name])
    except Exception:
        return False


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
    is_test_call = bool(
        _row_flag(row, "is_test")
        or _row_flag(row, "is_admin")
        or (request.client and getattr(request.client, "host", "").startswith(("172.30.", "127.0.0.1", "10.", "192.168.")))
    )
    if sources is not None and not is_test_call:
        try:                            # 来源网段统计（防 Key 分享）；失败不能影响请求
            await sources.observe(row, client_id, client=request.headers.get("user-agent", ""))
        except Exception as exc:
            bug("key_sources", exc)
    from .key_sources import network_of
    found = network_of(client_id)
    request_timing.set_source(found[1] if found else "")
    if share is not None and found and not is_test_call:
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
    reason = await features.check(STATE.db, key, name, STATE.settings)
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


# ============================================================== 后台维护任务 =====
from . import background_tasks

check_upstream_perf = background_tasks.check_upstream_perf
maintenance_loop = background_tasks.maintenance_loop
inactive_key_cleanup_loop = background_tasks.inactive_key_cleanup_loop
ANLAS_REBALANCE_SECONDS = background_tasks.ANLAS_REBALANCE_SECONDS
anlas_rebalance_loop = background_tasks.anlas_rebalance_loop
registration_reset_loop = background_tasks.registration_reset_loop


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


# ============================================================== 图片与工具路由 =====
from .routers import image_routes

# 兼容性重导出（保持单测与既有导入契约 100% 兼容）
read_image_payload = image_routes.read_image_payload
_anlas_auto = image_routes._anlas_auto
_flag = image_routes._flag
_manual = image_routes._manual
daily_base = image_routes.daily_base
guard_precheck = image_routes.guard_precheck
image_admission = image_routes.image_admission
acquire_concurrency = image_routes.acquire_concurrency
_acquire_slots = image_routes._acquire_slots
quota_image_check = image_routes.quota_image_check
ImageReservation = image_routes.ImageReservation
reserve_image_budget = image_routes.reserve_image_budget
complete_image_operation = image_routes.complete_image_operation
upstream_call = image_routes.upstream_call
wait_for_user_image_slot = image_routes.wait_for_user_image_slot
image_tool = image_routes.image_tool
upscale_image = image_routes.upscale_image
augment_image = image_routes.augment_image
encode_vibe = image_routes.encode_vibe
generate_image = image_routes.generate_image
generate_image_stream = image_routes.generate_image_stream
_generate_image = image_routes._generate_image
suggest_tags = image_routes.suggest_tags
_suggest_tags = image_routes._suggest_tags


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


# ============================================================== 文本与聊天路由 =====
from .routers import text_routes

TextStreamResponse = text_routes.TextStreamResponse
generate_stream = text_routes.generate_stream
generate_text = text_routes.generate_text
generate_voice = text_routes.generate_voice
TEXT_MODELS = text_routes.TEXT_MODELS
v1_models = text_routes.v1_models
v1_me = text_routes.v1_me
_feature_view = text_routes._feature_view
v1_chat = text_routes.v1_chat



# ================================================================ 页面与公共路由 =======
from .routers import public_routes

app.include_router(public_routes.router)

# 兼容性重导出（保持单测与既有导入契约 100% 兼容）
healthz = public_routes.healthz
LANDING_HEADERS = public_routes.LANDING_HEADERS
index = public_routes.index
favicon = public_routes.favicon
announcement = public_routes.announcement
_PUBLIC_STATUS_CACHE = public_routes._PUBLIC_STATUS_CACHE
public_live = public_routes.public_live
_CLIENT_ERR = public_routes._CLIENT_ERR
_injected_script_error = public_routes._injected_script_error
client_error = public_routes.client_error
live_me = public_routes.live_me
public_status = public_routes.public_status
_public_limits = public_routes._public_limits
_image_stability = public_routes._image_stability
_public_status_body = public_routes._public_status_body
ADMIN_HEADERS = public_routes.ADMIN_HEADERS
admin_page = public_routes.admin_page
user_subscription = public_routes.user_subscription
user_data = public_routes.user_data
user_information = public_routes.user_information
queue_status = public_routes.queue_status


# ================================================================ 路由注册 ====

image_routes.register_routes(app, limit_inflight)
text_routes.register_routes(app, limit_inflight)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SETTINGS.host, port=SETTINGS.port)
