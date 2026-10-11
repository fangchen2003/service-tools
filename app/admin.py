"""管理后台 API。"""

from __future__ import annotations

from collections import OrderedDict

import anyio

import hashlib
import hmac
import math
import os
import shutil
import time
from urllib.parse import urlsplit
from typing import Any, Optional

import json

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from fastapi.routing import APIRoute

from . import site_flags
from .registration import RegistrationError
from .action_log import ADMIN_ACTIONS, log_action, summarize

from . import features as feature_defs
from . import token_store
from . import ops
from .audit import audit_image_days
from .policy import gen_key
from .body import read_json_body
from .allowance import SETTING, read_alert_threshold
from .reconciliation import ReconciliationError
from .state import RUNTIME_LIMIT_BOUNDS

PREFIX = "/admin/api"
_AUDIT_BODY_LIMIT = 64 * 1024


class AuditedRoute(APIRoute):
    """所有会改动数据的后台接口自动写入操作日志（谁、何时、做了什么、对象、参数摘要、结果）。

    未登录的请求不记录（防止被刷），登录本身无论成败都记录。
    """

    def get_route_handler(self):
        handler = super().get_route_handler()
        route = self

        async def audited(request: Request):
            if request.method in ("GET", "HEAD", "OPTIONS"):
                return await handler(request)
            path = route.path_format[len(PREFIX):] if route.path_format.startswith(PREFIX) else route.path_format
            label = ADMIN_ACTIONS.get((request.method, path), f"{request.method} {path}")
            is_login = path == "/login"
            body = None
            declared = request.headers.get("content-length", "")
            if declared.isdecimal() and int(declared) <= _AUDIT_BODY_LIMIT:
                try:                                    # 缓存请求体：处理函数随后仍能读到同一份内容
                    body = json.loads(await request.body() or b"null")
                except ValueError:
                    body = None
            target = await _audit_target(request, path)
            authed = is_login or check_session(request)
            try:
                response = await handler(request)
            except HTTPException as exc:
                if authed:
                    await _log(request, label, target, f"失败（{exc.status_code}）：{exc.detail}"[:200], ok=False)
                raise
            if authed:
                if path == "/keys" and isinstance(body, dict):
                    target = str(body.get("name") or "")
                if path == "/keys/{key_id}" and request.method == "DELETE" and \
                        request.query_params.get("ban") in ("1", "true", "True"):
                    label = "删除 Key 并永久禁止领取"
                await _log(request, label, target, "" if is_login else summarize(body))
            return response

        return audited


async def _log(request: Request, label: str, target: str, detail: str, ok: bool = True) -> None:
    db = getattr(getattr(request.app.state, "gate", None), "db", None)
    if db is not None:
        await log_action(db, _actor(request), label, target, detail, ok=ok)


def _actor(request: Request) -> str:
    from .state import _mask_ip
    return "后台 " + _mask_ip(_client_id(request))


async def _audit_target(request: Request, path: str) -> str:
    """操作前先记下对象名称（删除之后就查不到了）。"""
    try:
        if "{key_id}" in path:
            key = await request.app.state.gate.db.get_key(int(request.path_params["key_id"]))
            return f"Key #{request.path_params['key_id']} {key['name'] if key else '(不存在)'}"
        if "{token_id}" in path:
            return "上游 " + str(request.path_params.get("token_id", ""))[:24]
    except Exception:
        pass
    return ""


router = APIRouter(prefix=PREFIX, route_class=AuditedRoute)


def _safe_filename(name: str) -> str:
    import re as _re
    return _re.sub(r'[^\\w.-]', '_', str(name))[:40] or 'member'

COOKIE = "nai_gate_admin"


def _client_id(request: Request) -> str:
    # Let the ASGI server apply its trusted-proxy policy. Raw forwarding headers
    # are attacker-controlled on direct connections and must not select a bucket.
    return request.client.host if request.client else "unknown"


# ----------------------------------------------------------------- auth ----

def _secret(request: Request) -> str:
    s = request.app.state.gate.settings
    if s.secret_key:
        return s.secret_key
    # 未配置则自动生成并持久化
    f = s.data_dir / "secret_key"
    existing = f.read_text().strip() if f.exists() else ""
    if len(existing) >= 32:
        s.secret_key = existing
    else:
        # 不存在或损坏（空文件等）：生成新的，并先写临时文件再原子替换，崩溃也不会留下空密钥
        s.secret_key = os.urandom(32).hex()
        tmp = f.with_name(f.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(s.secret_key)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, f)
    return s.secret_key


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _session_key(request: Request) -> str:
    # 把管理员密码摘要混入签名密钥：修改 ADMIN_PASSWORD 即令所有旧会话失效。
    gate = request.app.state.gate
    pw = getattr(gate, "admin_pw_hash", None) or gate.settings.admin_password or ""
    return _secret(request) + ":" + hashlib.sha256(pw.encode()).hexdigest()


def _pw_hash(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _pw_matches(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        probe = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(probe, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


def _password_ok(request: Request, password: str) -> bool:
    """站长在后台改过密码则以保存的哈希为准，否则使用 ADMIN_PASSWORD 环境变量。"""
    gate = request.app.state.gate
    stored = getattr(gate, "admin_pw_hash", None)
    if stored:
        return _pw_matches(password, stored)
    return hmac.compare_digest(password.encode(), gate.settings.admin_password.encode())


def make_session_cookie(request: Request) -> str:
    secret = _session_key(request)
    payload = str(int(time.time()) + 7 * 86400)
    return payload + "." + _sign(secret, payload)


def check_session(request: Request) -> bool:
    secret = _session_key(request)
    raw = request.cookies.get(COOKIE, "")
    if "." not in raw:
        return False
    payload, sig = raw.split(".", 1)
    try:
        if not hmac.compare_digest(sig.encode(), _sign(secret, payload).encode()):
            return False
    except (TypeError, ValueError):
        return False
    try:
        return int(payload) > time.time()
    except ValueError:
        return False


def _host_only(netloc: str) -> str:
    return (urlsplit("//" + netloc).hostname or "").lower()


def _origin_ok(request: Request) -> bool:
    """浏览器对跨源写请求一定会带 Origin；其主机名必须与本站 Host 一致（忽略端口，
    以兼容反向代理）。同级子域名、其他站点都会被拒。X-Forwarded-* 不被信任。
    无 Origin（curl 等非浏览器客户端）放行，它们拿不到浏览器里的 Cookie。"""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    host = _host_only(urlsplit(origin).netloc)
    if not host:
        return False
    allowed = {_host_only(request.headers.get("host", ""))}
    allowed.update(_host_only(urlsplit(o).netloc) for o in request.app.state.gate.settings.admin_allowed_origins)
    allowed.discard("")
    return host in allowed


def require_admin(request: Request) -> None:
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _origin_ok(request):
        raise HTTPException(403, "来源校验失败")
    if not check_session(request):
        raise HTTPException(401, "未登录或会话已过期")


# ----------------------------------------------------------------- routes ----

@router.post("/login")
async def login(request: Request, response: Response):
    if not _origin_ok(request):
        raise HTTPException(403, "来源校验失败")
    if not await request.app.state.gate.hit_login(_client_id(request)):
        raise HTTPException(429, "登录尝试过于频繁，请稍后再试")
    body = await read_json_body(request)
    password = str(body.get("password", ""))
    gate = request.app.state.gate
    if not getattr(gate, "admin_pw_hash", None):
        if not gate.settings.admin_password:
            raise HTTPException(503, "尚未设置 ADMIN_PASSWORD 环境变量，管理端已锁定")
        if gate.settings.admin_password == "changeme-please":
            raise HTTPException(503, "ADMIN_PASSWORD 仍是示例值 changeme-please，请先在 .env 中改成强密码")
    if not _password_ok(request, password):
        raise HTTPException(401, "密码错误")
    response.set_cookie(
        COOKIE, make_session_cookie(request),
        httponly=True, secure=request.app.state.gate.settings.admin_cookie_secure,
        samesite="strict", max_age=7 * 86400,
    )
    return {"ok": True}


@router.put("/password")
async def change_password(request: Request, response: Response):
    """站长在后台修改密码：需要当前密码；成功后所有已登录会话（包括当前）立即失效，需用新密码重新登录。"""
    require_admin(request)
    gate = request.app.state.gate
    if not await gate.hit_login(_client_id(request)):           # 同样受登录限流保护，防止被劫持会话后猜当前密码
        raise HTTPException(429, "尝试过于频繁，请稍后再试")
    body = await read_json_body(request)
    current, new = str(body.get("current", "")), str(body.get("new", ""))
    if not _password_ok(request, current):
        raise HTTPException(401, "当前密码不正确")
    if len(new) < 12 or new == "changeme-please" or new == current:
        raise HTTPException(422, "新密码至少 12 个字符，且不能与当前密码或示例密码相同")
    stored = _pw_hash(new)
    await gate.db.set_setting("admin_password_hash", stored)
    gate.admin_pw_hash = stored
    response.delete_cookie(COOKIE, httponly=True, secure=gate.settings.admin_cookie_secure, samesite="strict")
    return {"ok": True, "relogin": True}


@router.post("/logout")
async def logout(request: Request, response: Response):
    response.delete_cookie(
        COOKIE, httponly=True, secure=request.app.state.gate.settings.admin_cookie_secure,
        samesite="strict",
    )
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    require_admin(request)
    return {"ok": True}


@router.get("/reconciliation")
async def reconciliation_status(request: Request, response: Response):
    require_admin(request)
    response.headers["Cache-Control"] = "no-store"
    return {**await request.app.state.gate.reconciliation.status(),
            "csrf_token": _reconciliation_csrf(request)}


def _reconciliation_csrf(request: Request) -> str:
    # Use a purpose-specific HMAC of the authenticated session for CSRF checks.
    return _sign(_session_key(request), "reconciliation:" + request.cookies[COOKIE])


@router.post("/reconciliation")
async def reconcile_anlas(request: Request, response: Response):
    require_admin(request)
    # Session-bound CSRF works across TLS proxies and isolates sibling origins.
    csrf = request.headers.get("x-nai-admin-csrf", "")
    if not hmac.compare_digest(csrf.encode(), _reconciliation_csrf(request).encode()):
        raise HTTPException(403, "会话校验失败，请刷新后台后重试")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "请使用 JSON 请求")
    if await read_json_body(request, limit=1024):
        raise HTTPException(400, "本操作不接受自定义账号或地址")
    response.headers["Cache-Control"] = "no-store"
    try:
        return {**await request.app.state.gate.reconciliation.run(),
                "csrf_token": _reconciliation_csrf(request)}
    except ReconciliationError as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(exc.status, str(exc), headers=headers) from None


def _discord_profile(row) -> Optional[dict]:
    """成员的 Discord 资料：显示名、用户名、头像地址、个人资料链接（点开可直接私信）。"""
    if row is None:
        return None
    discord_id, username, display, avatar = str(row[1]), row[2], row[3], row[4]
    if avatar:
        avatar_url = f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar}.png?size=64"
    else:
        avatar_url = f"https://cdn.discordapp.com/embed/avatars/{(int(discord_id) >> 22) % 6}.png"
    return {"id": discord_id, "username": username, "display_name": display, "avatar_url": avatar_url,
            "profile_url": f"https://discord.com/users/{discord_id}"}


def _key_json(row, counter, generated_images_total: int = 0) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "token": row["token"],
        "enabled": bool(row["enabled"]),
        "daily_images": row["daily_images"],
        "daily_anlas": row["daily_anlas"],
        "daily_v5": row["daily_v5"],
        "monthly_anlas": row["monthly_anlas"],
        "daily_text_tokens": row["daily_text_tokens"],
        "rpm": row["rpm"],
        "allow_anlas": bool(row["allow_anlas"]),
        "allow_img2img": bool(row["allow_img2img"]),
        "exclude_global_v5": bool(row["exclude_global_v5"]),
        "image_model_scope": row["image_model_scope"],
        "is_admin": bool(row["is_admin"]),
        "is_test": bool(row["is_test"]) if "is_test" in row.keys() else False,
        "quota_auto": row["quota_auto"] == 1 if "quota_auto" in row.keys() else False,
        "features": feature_defs.key_features(row),
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "used": {
            "images": counter["images"],
            "generated_images_total": generated_images_total,
            "legacy_free_images": counter["legacy_free_images"],
            "anlas": round(float(counter["anlas"]), 2),
            "v5": counter["v5"],
            "text_tokens": counter["text_tokens"],
            "requests": counter["requests"],
        },
    }


@router.get("/keys")
async def list_keys(request: Request):
    require_admin(request)
    st = request.app.state.gate
    rows = await st.db.list_keys()
    today = st.day()
    totals = await st.db.generated_image_totals()
    sources = await st.db.key_source_summary(time.time() - 24 * 3600)
    out = []
    for r in rows:
        c = await st.db.get_counter(r["id"], today)
        item = _key_json(r, c, totals.get(r["id"], 0))
        item["sources_24h"] = sources.get(r["id"], [])
        out.append(item)
    return {"keys": out, "share_alert_nets": st.settings.key_share_alert_nets}


def _features_field(body: dict):
    """features: null/缺省 = 沿用旧行为（全局开启的都可用）；列表 = 仅允许所列功能。"""
    if "features" not in body or body["features"] is None:
        return None
    value = body["features"]
    if not isinstance(value, list) or any(not isinstance(v, str) or v not in feature_defs.FEATURES for v in value):
        raise HTTPException(422, "features 必须是功能名列表：" + ",".join(feature_defs.FEATURES))
    return feature_defs.dump(value)


def _num(value: Any, kind: type, lo, hi, name: str):
    """严格解析后台数值：空值、负数、越界、小数（整数字段）一律 422。

    这些字段里 0 代表“不限”，绝不能把空值或非法值静默变成 0，否则一次误操作就会拆掉额度保护。
    """
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        raise HTTPException(422, f"{name} 不能为空")
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(422, f"{name} 必须是有效数字") from None
    if not math.isfinite(v):
        raise HTTPException(422, f"{name} 必须是有效数字")
    if kind is int:
        if not v.is_integer():
            raise HTTPException(422, f"{name} 必须是整数")
        v = int(v)
    if not lo <= v <= hi:
        raise HTTPException(422, f"{name} 必须在 {lo}～{hi} 之间")
    return v


async def _new_key_features(request: Request, body: dict):
    """新建 Key 未指定功能时，沿用“新成员默认开通的功能”（默认只有文生图），而不是全部功能。"""
    if "features" in body:
        return _features_field(body)
    db = request.app.state.gate.db
    reg = await ops.registration_settings(db, getattr(request.app.state, "registrar", None))
    if reg.get("features") is None and await db.get_setting("register_features", None) == "*":
        return None                     # 站长明确把默认设为“全部已开放功能”
    return feature_defs.dump(reg.get("features") or ["image"])


@router.post("/keys")
async def create_key(request: Request):
    require_admin(request)
    st = request.app.state.gate
    body = await read_json_body(request)

    def _int_field(name: str, default: int, lo: int, hi: int) -> int:
        return _num(body.get(name, default), int, lo, hi, name)

    daily_images = _int_field("daily_images", st.settings.default_daily_images, 0, 1000000)
    monthly_anlas = _num(body.get("monthly_anlas", st.settings.default_monthly_anlas), float, 0.0, 100000.0, "monthly_anlas")
    daily_anlas = _num(body.get("daily_anlas", st.settings.default_daily_anlas), float, 0.0, 100000.0, "daily_anlas")
    daily_v5 = _int_field("daily_v5", st.settings.default_daily_v5, 0, 100000)
    daily_text = _int_field("daily_text_tokens", st.settings.default_daily_text_tokens, 0, 100_000_000)
    rpm = _int_field("rpm", st.settings.default_rpm, 1, 600)
    expires_days = _int_field("expires_days", st.settings.default_expires_days, 0, 3650)
    expires_at = (time.time() + expires_days * 86400) if expires_days > 0 else None
    image_model_scope = "all" if body.get("image_model_scope") == "all" else "legacy"

    row = await st.db.create_key({
        "name": str(body.get("name", "") or "").strip()[:60] or "未命名",
        "token": gen_key("nai"),
        "daily_images": daily_images,
        "daily_anlas": daily_anlas,
        "daily_v5": daily_v5,
        "monthly_anlas": monthly_anlas,
        "daily_text_tokens": daily_text,
        "rpm": rpm,
        "allow_anlas": bool(body.get("allow_anlas", False)),
        "allow_img2img": bool(body.get("allow_img2img", False)),
        "exclude_global_v5": bool(body.get("exclude_global_v5", False)),
        "is_test": bool(body.get("is_test", False)),
        "image_model_scope": image_model_scope,
        "features": await _new_key_features(request, body),
        "expires_at": expires_at,
    })
    # 后台手动建的 Key 用站长填的额度和模型范围：标为手动，动态额度算法不覆盖（否则 10 分钟内会被改成算法默认值、
    # 连 V4.5-only 也会被开成 V5）；Anlas 同理。要交给算法，可在成员页切回「自动」。
    await st.db._db.execute("UPDATE api_keys SET quota_auto=-1, anlas_auto=-1 WHERE id=?", (row["id"],))
    await st.db._db.commit()
    row = await st.db.get_key(row["id"])
    c = await st.db.get_counter(row["id"], st.day())
    return {"key": _key_json(row, c, 0)}


async def _notify_member(request: Request, key_id: int, text: str) -> None:
    """后台对 Discord 成员做了会影响使用的操作时，机器人私信本人（?notify=0 / body.notify=false 可关闭）。"""
    registrar = getattr(request.app.state, "registrar", None)
    if registrar is None:
        return
    discord_id = await registrar.registration_for_key(key_id)
    if discord_id is None:
        return
    try:
        sent = await registrar.send_dm(discord_id, "🦉 猫头鹰公益站通知：" + text)
    except Exception:              # 私信失败绝不能让后台操作本身失败
        sent = False
    blocked = "" if sent else getattr(registrar, "last_dm_block", "")
    import re as _re
    summary = _re.sub(r"nai-[A-Za-z0-9_\-]+", "nai-***", text)[:80]     # 操作日志里绝不能出现明文 Key
    from .action_log import log_action
    await log_action(request.app.state.gate.db, "系统", "私信成员", f"Key #{key_id}",
                     (f"已私信：{summary}" if sent else f"未私信（{blocked}）：{summary}" if blocked
                      else "私信失败（对方可能关闭了私信）"), ok=sent or bool(blocked))


def _describe_changes(before, fields: dict) -> list[str]:
    out = []
    if "enabled" in fields and bool(before["enabled"]) != fields["enabled"]:
        out.append("你的 Key 已被站长" + ("恢复，可以继续使用" if fields["enabled"] else "暂停，暂时无法使用（有疑问请到 🛠️｜问题反馈）"))
    labels = {"daily_images": "每日图片", "daily_v5": "每日 V5", "daily_anlas": "每日 Anlas"}
    for name, label in labels.items():
        if name in fields and before[name] != fields[name]:
            out.append(f"{label}额度：{before[name] or '不限'} → {fields[name] or '不限'}")
    if "allow_anlas" in fields and bool(before["allow_anlas"]) != fields["allow_anlas"]:
        out.append("已为你" + ("开通" if fields["allow_anlas"] else "关闭") + " Anlas（付费规格）")
    if "image_model_scope" in fields and before["image_model_scope"] != fields["image_model_scope"]:
        out.append("可用模型：" + ("含 V5" if fields["image_model_scope"] == "all" else "仅 V4.5 及以下"))
    old_exp, new_exp = before["expires_at"], fields.get("expires_at", before["expires_at"])
    # 编辑框每次都按「剩余天数」重算到期时间，相差不到 1 天视为没改
    if "expires_at" in fields and (old_exp is None) != (new_exp is None) or (
            old_exp and new_exp and abs(old_exp - new_exp) > 86400):
        out.append("Key 有效期已调整" + (f"，约 {max(0, round((fields['expires_at'] - time.time()) / 86400))} 天后到期" if fields["expires_at"] else "为长期有效"))
    return out


@router.post("/keys/{key_id}/regenerate")
async def regenerate_key(request: Request, response: Response, key_id: int):
    require_admin(request)
    token = gen_key("nai")
    if not await request.app.state.gate.db.rotate_key_token(key_id, token):
        raise HTTPException(404, "key 不存在")
    if request.query_params.get("notify", "1") != "0":
        await _notify_member(request, key_id, f"站长为你重置了 Key，旧 Key 已失效。新的 Key（请妥善保存）：`{token}`")
    response.headers["Cache-Control"] = "no-store"
    return {"token": token}


QUICK_V5_DAILY = 15


@router.patch("/keys/{key_id}")
async def patch_key(request: Request, key_id: int):
    require_admin(request)
    st = request.app.state.gate
    if not await st.db.get_key(key_id):
        raise HTTPException(404, "key 不存在")
    body = await read_json_body(request)
    quota = body.get("quota")
    if quota not in (None, "auto", "manual"):
        raise HTTPException(422, "quota 只能是 auto（算法）或 manual（手动）")
    if quota == "auto":
        cur = await st.db.get_key(key_id)
        if cur["is_test"] and body.get("is_test", True) is not False:
            raise HTTPException(422, "测试 Key 不归算法管理，只能手动设置额度")
    if quota is not None:
        # 2026-10-11 站长：额度只有两种——「算法」全部交给算法；「手动」填多少就是多少，不和算法混用
        for k in ("anlas_mode", "v5_pinned", "quota_mode"):
            body.pop(k, None)
        if quota == "auto":
            for k in ("daily_images", "daily_v5", "daily_anlas", "monthly_anlas", "allow_anlas",
                      "exclude_global_v5", "image_model_scope", "features"):
                body.pop(k, None)
    fields: dict[str, Any] = {}
    if "name" in body:
        fields["name"] = str(body["name"] or "").strip()[:60] or "未命名"
    if "enabled" in body:
        fields["enabled"] = bool(body["enabled"])
    if "daily_images" in body:
        fields["daily_images"] = _num(body["daily_images"], int, 0, 1000000, "daily_images")
    if "daily_anlas" in body:
        fields["daily_anlas"] = _num(body["daily_anlas"], float, 0.0, 100000.0, "daily_anlas")
    if "daily_v5" in body:
        fields["daily_v5"] = _num(body["daily_v5"], int, 0, 100000, "daily_v5")
    if "monthly_anlas" in body:
        fields["monthly_anlas"] = _num(body["monthly_anlas"], float, 0.0, 100000.0, "monthly_anlas")
    if "daily_text_tokens" in body:
        fields["daily_text_tokens"] = _num(body["daily_text_tokens"], int, 0, 100_000_000, "daily_text_tokens")
    if "rpm" in body:
        fields["rpm"] = _num(body["rpm"], int, 1, 600, "rpm")
    if "allow_anlas" in body:
        fields["allow_anlas"] = bool(body["allow_anlas"])
    if "allow_img2img" in body:
        fields["allow_img2img"] = bool(body["allow_img2img"])
    if "exclude_global_v5" in body:
        fields["exclude_global_v5"] = bool(body["exclude_global_v5"])
    if "is_test" in body:
        fields["is_test"] = bool(body["is_test"])
    if "image_model_scope" in body:
        fields["image_model_scope"] = "all" if body["image_model_scope"] == "all" else "legacy"
    if "features" in body:
        fields["features"] = _features_field(body)
    mode = body.get("anlas_mode")
    if mode is not None:
        # 成员页快捷设置：off 关闭（算法也不会再开）/ auto 交给算法 / manual 手动每天 N
        if mode == "off":
            fields.update(allow_anlas=False, daily_anlas=0.0)
        elif mode == "manual":
            fields.update(allow_anlas=True, daily_anlas=_num(body.get("daily_anlas", 0), float, 1.0, 100000.0, "daily_anlas"))
        elif mode == "auto":
            fields.update(allow_anlas=False, daily_anlas=0.0)
        else:
            raise HTTPException(422, "anlas_mode 只能是 off / auto / manual")
    if "v5_pinned" in body:
        # 成员页「手动 · V5 N/天」：站长定的基础值，算法按它算实际额度（不低于普通成员，节约模式同样放大）
        fields["v5_pinned"] = _num(body["v5_pinned"], int, 1, 1000, "v5_pinned")
        fields["image_model_scope"] = "all"
    if body.get("image_model_scope") == "all" and "daily_v5" not in body and "v5_pinned" not in fields:
        before_v5 = await st.db.get_key(key_id)
        if before_v5 and not before_v5["daily_v5"]:
            fields["daily_v5"] = QUICK_V5_DAILY     # 从「仅 V4.5」开到 V5 时给一个默认日额度，和早期成员一致
    if "expires_days" in body:
        d = _num(body["expires_days"], int, 0, 3650, "expires_days")
        fields["expires_at"] = (time.time() + d * 86400) if d > 0 else None
    before = await st.db.get_key(key_id)
    if quota == "auto":
        # Anlas 也交给算法：先收回手动授权，再由 anlas_pool 按条件分配（Sonnet 审查 P1：否则手动的 150/天永久保留）
        fields.update(exclude_global_v5=False, features=None, allow_anlas=False, daily_anlas=0.0)
    if quota is not None:
        fields["v5_pinned"] = None                  # 手动就是填的值，不再有「基础值 + 不低于普通成员」的中间状态
    if quota is None and "daily_v5" in fields and "v5_pinned" not in fields and fields["daily_v5"] != before["daily_v5"]:
        fields["v5_pinned"] = fields["daily_v5"] or None      # 编辑窗口里手填的 V5 张数也作为手动基础值
    await st.db.update_key(key_id, fields)
    quota_mode = body.get("quota_mode") or ("auto" if quota == "auto" else None)
    if quota == "manual":
        await st.db._db.execute("UPDATE api_keys SET quota_auto=-1, anlas_auto=-1 WHERE id=?", (key_id,))
        await st.db._db.commit()
        mode = None
    # 只有额度 / 模型的值真的变了才转为手动（2026-10-10：提交了一个没变的「模型 = 全部」就被冻结在 18 张）
    quota_changed = any(k in fields and fields[k] != before[k] for k in ("daily_images", "daily_v5", "image_model_scope")) \
        or ("v5_pinned" in fields and fields["v5_pinned"] != before["v5_pinned"])
    if quota == "manual":
        pass
    elif quota_mode == "auto" or quota_changed:
        # 手动改了额度或模型 → 这把 Key 由站长管理（-1），动态额度算法不再覆盖；选「交给算法」恢复为 1 并立即重算
        await st.db._db.execute("UPDATE api_keys SET quota_auto=?, v5_pinned=CASE WHEN ?=1 THEN NULL ELSE v5_pinned END "
                                "WHERE id=?", (1 if quota_mode == "auto" else -1, 1 if quota_mode == "auto" else 0, key_id))
        await st.db._db.commit()
        if quota_mode == "auto" or "v5_pinned" in fields:
            try:
                from . import quota_algo
                await quota_algo.run(st)
            except Exception as exc:
                st.bugs.capture("quota_algo", exc) if getattr(st, "bugs", None) else None
    if quota == "auto":
        mode = "auto"
    if quota == "manual":
        pass
    elif mode == "auto" or "allow_anlas" in fields or "daily_anlas" in fields:
        # 手动设置后由站长管理（-1），自动分配不会再覆盖；选「交给算法」则回到 0，下次重算时按条件分配
        await st.db._db.execute("UPDATE api_keys SET anlas_auto=? WHERE id=?", (0 if mode == "auto" else -1, key_id))
        await st.db._db.commit()
        if mode == "auto":
            try:
                from . import anlas_pool
                await anlas_pool.rebalance(st)
            except Exception as exc:
                st.bugs.capture("anlas_pool", exc) if getattr(st, "bugs", None) else None
    changes = _describe_changes(before, fields)
    if changes and body.get("notify", True) is not False:
        await _notify_member(request, key_id, "站长调整了你的 Key：\n• " + "\n• ".join(changes))
    row = await st.db.get_key(key_id)
    c = await st.db.get_counter(key_id, st.day())
    totals = await st.db.generated_image_totals(key_id)
    return {"key": _key_json(row, c, totals.get(key_id, 0))}


@router.post("/keys/{key_id}/reset-daily-image-quota")
async def reset_daily_image_quota(request: Request, key_id: int):
    """仅重置指定 Key 今日的 V5 与 Anlas 配额计数，保留审计日志。"""
    require_admin(request)
    st = request.app.state.gate
    row = await st.db.get_key(key_id)
    if not row:
        raise HTTPException(404, "key 不存在")
    await st.db.reset_daily_image_quota(key_id, st.day())
    counter = await st.db.get_counter(key_id, st.day())
    totals = await st.db.generated_image_totals(key_id)
    if request.query_params.get("notify", "1") != "0":
        await _notify_member(request, key_id, "站长已为你重置了今天的额度，可以继续使用。")
    return {"ok": True, "key": _key_json(row, counter, totals.get(key_id, 0))}


@router.post("/keys/{key_id}/coupons")
async def give_coupon(request: Request, key_id: int):
    """给成员发一张券（现在只有重置券）：{"kind": "reset", "days": 7, "note": "唱得真好听"}。成员在首页自己使用。"""
    require_admin(request)
    body = await read_json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(422, "参数必须是对象")
    kind = body.get("kind", "reset")
    days = body.get("days", 7)
    if kind != "reset":
        raise HTTPException(422, "目前只有重置券（reset）")
    if type(days) not in (int, float) or not 0 < days <= 30:
        raise HTTPException(422, "有效期 days 必须在 1～30 天之间")
    st = request.app.state.gate
    if not await st.db.get_key(key_id):
        raise HTTPException(404, "key 不存在")
    note = str(body.get("note") or "")[:200]
    c = await st.db.add_coupon(key_id, kind, float(days), note, str(body.get("by") or "站长")[:40])
    return {"ok": True, "coupon": c}


@router.get("/status-codes")
async def status_codes(request: Request, hours: int = 24):
    """成员接口 HTTP 状态码分布（最近 1 / 24 / 168 小时）。"""
    require_admin(request)
    from . import status_stats
    return await status_stats.distribution(request.app.state.gate.db, max(1, min(int(hours), 24 * 30)))


@router.post("/gift")
async def gift_member(request: Request):
    """奖励一位 Discord 用户（站长手动触发，先做成接口，以后机器人夸人也走这里）：
    {"discord_id", "username"?, "global_name"?, "reason", "coupon_days": 7,
     "reply": {"channel_id", "message_id", "text"}?}
    没有 Key → 直接发一把（不受名额限制）并私信 Key；然后发一张重置券（note = reason）；有 reply 就让奶妹回复那条消息。"""
    require_admin(request)
    body = await read_json_body(request)
    if not isinstance(body, dict) or not str(body.get("discord_id", "")).isdecimal():
        raise HTTPException(422, "需要 discord_id")
    reg = getattr(request.app.state, "registrar", None)
    if reg is None:
        raise HTTPException(503, "Discord 机器人没有配置")
    st = request.app.state.gate
    user = {"id": str(body["discord_id"]), "username": str(body.get("username") or ""),
            "global_name": str(body.get("global_name") or ""), "avatar": ""}
    try:
        g = await reg.gift(user)
    except RegistrationError as exc:
        raise HTTPException(409, str(exc)) from None
    reason = str(body.get("reason") or "")[:200]
    out: dict[str, Any] = {"new_key": g["new"], "key_id": g["key_id"]}
    days = body.get("coupon_days", 7)
    if days:
        out["coupon"] = await st.db.add_coupon(g["key_id"], "reset", float(days), reason, "奶妹")
    if g["new"]:
        # 奖励的 Key 不占别人的名额：名额上限跟着加 1，否则「已领人数 ≤ 名额」的交叉校验会报警（10/10 实际触发）
        cfg = await reg.settings()
        active = await reg.count_active()
        if cfg.get("max_users") and active > cfg["max_users"]:
            # 静默调整：奖励发 Key 不是「放名额」，不发名额公告（10/10 站长反馈）
            await ops.set_registration(st.db, {"max_users": active, "notify": False}, st, reg)
            out["max_users"] = active
    if g["new"]:
        extra = (f"\n\n🎁 这把 Key 是奶妹送你的（{reason}）" if reason else "") + \
                ("，还附带一张重置券，用 Key 登录首页就能看到、7 天内有效～" if days else "")
        out["dm_sent"] = await reg.send_dm(user["id"], g["message"] + extra)
        if not out["dm_sent"]:
            out["dm_block"] = getattr(reg, "last_dm_block", "") or "对方关闭了私信"
            out["key"] = g["key"]           # 私信没发出去：交给站长手动转交
    rp = body.get("reply")
    if isinstance(rp, dict) and rp.get("channel_id") and rp.get("message_id") and rp.get("text"):
        out["replied"] = await reg.reply_in_channel(str(rp["channel_id"]), str(rp["message_id"]), str(rp["text"]))
    await log_action(st.db, "后台", "奖励成员", f"Discord:{user['id']}",
                     f"{'新发 Key · ' if g['new'] else ''}重置券 {days} 天 · {reason}"
                     + (" · 已私信" if out.get("dm_sent") else "") + (" · 已回复" if out.get("replied") else ""))
    return out


@router.get("/keys/{key_id}/coupons")
async def list_key_coupons(request: Request, key_id: int):
    require_admin(request)
    return {"coupons": await request.app.state.gate.db.list_coupons(key_id, active_only=False)}


@router.delete("/keys/{key_id}")
async def delete_key(request: Request, key_id: int, ban: bool = False):
    """删除成员 / Key。Discord 自助领取的成员会同时清掉领取记录并摘除身份组；ban=true 则永久禁止该账号再次领取。"""
    require_admin(request)
    gate = request.app.state.gate
    if not await gate.db.get_key(key_id):
        raise HTTPException(404, "key 不存在")
    registrar = getattr(request.app.state, "registrar", None)
    discord_id = await registrar.registration_for_key(key_id) if registrar is not None else None
    if discord_id is not None:
        if request.query_params.get("notify", "1") != "0":
            await _notify_member(request, key_id, "你的 Key 已被站长" + (
                "删除，并且这个 Discord 账号不能再领取。" if ban else "删除。如需继续使用，可以在 🔑｜领取key 重新 /register。"))
        await (registrar.ban if ban else registrar.revoke)(discord_id)
        return {"ok": True, "discord_id": discord_id, "banned": bool(ban)}
    await gate.db.delete_key(key_id)
    return {"ok": True}


@router.get("/logs")
async def logs(request: Request, key_id: Optional[int] = None, page: int = 1,
               feature: Optional[str] = None, hide_test: bool = False, rid: str = ""):
    require_admin(request)
    per_page = 20
    page = max(1, min(int(page), 1_000_000))
    db = request.app.state.gate.db
    if feature and feature not in feature_defs.FEATURES:
        raise HTTPException(422, "未知功能")
    kinds = feature_defs.kinds_for(feature) if feature else None
    rid = "".join(c for c in rid.strip().lower() if c in "0123456789abcdef")[:16]
    total = await db.count_logs(key_id=key_id, kinds=kinds, hide_test=hide_test, rid=rid)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    rows = await db.list_logs(limit=per_page, offset=(page - 1) * per_page,
                              key_id=key_id, kinds=kinds, hide_test=hide_test, rid=rid)
    return {
        "logs": [dict(r) for r in rows],
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": pages,
    }


@router.get("/quota-algo")
async def quota_algo_get(request: Request):
    """动态额度：当前结果、参数、最近 30 天的每日微调记录。"""
    require_admin(request)
    from . import quota_algo
    db = request.app.state.gate.db
    params = {k: await db.get_setting(k, v) for k, v in quota_algo.DEFAULTS.items()}
    return {"last": json.loads(await db.get_setting(quota_algo.STATE_KEY, "{}") or "{}"),
            "history": json.loads(await db.get_setting(quota_algo.HISTORY_KEY, "[]") or "[]"),
            "params": params}


@router.get("/autopilot")
async def autopilot_get(request: Request):
    """自动驾驶：每条规则的最新判断（观察模式下只记录不执行）和最近 7 天的每小时快照。"""
    require_admin(request)
    from . import autopilot
    db = request.app.state.gate.db
    return {"last": json.loads(await db.get_setting(autopilot.STATE_KEY, "{}") or "{}"),
            "history": json.loads(await db.get_setting(autopilot.HISTORY_KEY, "[]") or "[]")}


@router.put("/quota-algo")
async def quota_algo_put(request: Request):
    """修改初始值 / 步长 / 范围；保存后立即重算。"""
    require_admin(request)
    from . import quota_algo
    body = await read_json_body(request)
    st = request.app.state.gate
    bounds = {"quota_auto_enabled": (0, 1), "quota_target_avg": (10, 2000), "quota_base": (0, 2000),
              "quota_ceiling_max": (10, 5000), "quota_base_min": (0, 2000), "quota_step": (1, 500),
              "quota_base_step": (1, 500), "quota_v5_min": (0, 500), "quota_v5_max": (1, 500)}
    updates = {}
    for k, v in body.items():
        if k in bounds:
            updates[k] = _num(v, int, bounds[k][0], bounds[k][1], k)
    if updates:
        await st.db.set_settings_bulk(updates)
        # 改了初始值时，同步当前值（否则要等明天的微调）
        if "quota_target_avg" in updates:
            await st.db.set_setting("quota_ceiling", updates["quota_target_avg"])
        if "quota_base" in updates:
            await st.db.set_setting("quota_base_now", updates["quota_base"])
    result = await quota_algo.run(st)
    return {"ok": True, "last": result}


@router.get("/discord-bot")
async def discord_bot_get(request: Request):
    """Discord 机器人：配置、在线状态（心跳）、最近 50 条点赞 / 评论记录。"""
    require_admin(request)
    from . import bot_config
    return await bot_config.snapshot(request.app.state.gate.db)


@router.put("/discord-bot")
async def discord_bot_put(request: Request):
    """修改机器人配置；机器人一分钟内生效。"""
    require_admin(request)
    from . import bot_config
    try:
        values = bot_config.validate(await read_json_body(request))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await bot_config.save(request.app.state.gate.db, values)
    return {"ok": True, "config": await bot_config.load(request.app.state.gate.db)}


@router.get("/keys/{key_id}/share")
async def key_share_detail(request: Request, key_id: int):
    """防分享溯源：证据与处罚记录 + 最近 60 次请求的来源网络（打码）和客户端。"""
    require_admin(request)
    st = request.app.state.gate
    trail = await st.db._db.execute_fetchall(
        "SELECT ts, kind, status, src, client, substr(detail,1,60) FROM usage_log WHERE key_id=? ORDER BY ts DESC LIMIT 60",
        (key_id,))
    return {"evidence": await st.share.evidence(key_id),
            "trail": [{"ts": r[0], "kind": r[1], "status": r[2], "src": r[3], "client": r[4], "detail": r[5]} for r in trail]}


@router.post("/keys/{key_id}/share-clear")
async def key_share_clear(request: Request, key_id: int):
    """误判：清零风险分、解除暂停、违规次数归零（证据保留）。"""
    require_admin(request)
    await request.app.state.gate.share.clear(key_id)
    return {"ok": True}


@router.get("/share-guard")
async def share_guard_get(request: Request):
    require_admin(request)
    st = request.app.state.gate
    return {"mode": await st.share.mode(), "keys": await st.share.report()}


@router.put("/share-guard")
async def share_guard_put(request: Request):
    """防分享模式：enforce 执行 / observe 只记录并提醒站长 / off 关闭。"""
    require_admin(request)
    body = await read_json_body(request)
    if body.get("mode") not in ("enforce", "observe", "off"):
        raise HTTPException(422, "mode 只能是 enforce / observe / off")
    from .share_guard import MODE_SETTING
    await request.app.state.gate.db.set_setting(MODE_SETTING, body["mode"])
    return {"ok": True, "mode": body["mode"]}


@router.get("/modules")
async def modules_get(request: Request):
    """模块：每个模块回答的问题、依据、数学原理、开关、最近一次运行、交叉校验、关键参数（依据等级）。"""
    require_admin(request)
    kernel = getattr(request.app.state.gate, "kernel", None)
    return {"modules": await kernel.snapshot() if kernel else []}


@router.put("/modules/{name}")
async def modules_put(request: Request, name: str):
    """启用 / 关闭一个模块；关闭只停止自动调整，保护类的硬限制不受影响。"""
    require_admin(request)
    kernel = getattr(request.app.state.gate, "kernel", None)
    if kernel is None or name not in kernel.modules:
        raise HTTPException(404, "没有这个模块")
    body = await read_json_body(request)
    if not isinstance(body.get("enabled"), bool):
        raise HTTPException(422, "enabled 必须是 true / false")
    await kernel.set_enabled(name, body["enabled"])
    await kernel.tick_all()
    return {"ok": True, "modules": await kernel.snapshot()}


@router.get("/errors")
async def errors_list(request: Request, all: bool = False):
    """Bug 追踪：按特征归并的错误（未处理的在前）。detail 含堆栈，只在后台显示。"""
    require_admin(request)
    tracker = request.app.state.gate.bugs
    items = await tracker.list(include_resolved=all)
    keys = {r["last_key"] for r in items if r["last_key"]}
    names = {}
    for key_id in keys:
        row = await request.app.state.gate.db.get_key(key_id)
        if row:
            names[key_id] = row["name"]
    for r in items:
        r["key_name"] = names.get(r["last_key"], "")
    return {"errors": items, "open": sum(1 for r in items if r["resolved_at"] is None)}


@router.post("/errors/{sig}/resolve")
async def errors_resolve(sig: str, request: Request):
    """标记已处理；之后再出现会作为「复发」重新提醒。"""
    require_admin(request)
    if not await request.app.state.gate.bugs.resolve(sig[:12]):
        raise HTTPException(404, "没有这条未处理的错误")
    return {"ok": True}


@router.get("/perf")
async def upstream_perf(request: Request):
    """上游表现：V4.5 / V5 的生成耗时、排队、限流、成功率、产能，与过去 7 天对比。"""
    require_admin(request)
    from . import perf
    return await perf.collect(request.app.state.gate, time.time())


@router.get("/risk-check")
async def risk_check(request: Request):
    """风险检查（只读）：出站指纹盘点（请求头 / TLS / 出口链路）与白嫖行为信号。

    伪装类指标（TLS 指纹伪装、浏览器指纹、代理换出口）明确留空并说明原因，见 app/risk_check.py。"""
    require_admin(request)
    from . import risk_check
    return await risk_check.collect(request.app.state.gate, time.time())


@router.get("/guard")
async def guard_get(request: Request):
    """账号保护与排队（P0 / P1）的当前设置和实时用量。"""
    require_admin(request)
    st = request.app.state.gate
    data = st.guard.describe()
    accounts = []
    for t in st.nai.pool:
        c = await st.db.get_upstream_counter(t.token_id, st.day())
        accounts.append({"position": t.position, "usable": t.usable, "today": c["images"],
                         "today_units": round(c.get("units", c["images"]), 1),
                         "this_hour": st.guard.hour_count(t.token_id)})
    data["accounts"] = accounts
    data["queued_images"] = sum(st.guard.image_inflight.values())
    return data


@router.put("/guard")
async def guard_put(request: Request):
    require_admin(request)
    body = await read_json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(422, "参数必须是对象")
    try:
        await request.app.state.gate.guard.save(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return await guard_get(request)


@router.get("/anlas-pool")
async def anlas_pool_get(request: Request):
    """Anlas 自动分配：上次结果与参数。"""
    require_admin(request)
    from . import anlas_pool
    db = request.app.state.gate.db
    last = await db.get_setting(anlas_pool.STATE_KEY, None)
    return {"last": json.loads(last) if last else None,
            "settings": {k: await anlas_pool._setting(db, k) for k in anlas_pool.DEFAULTS}}


@router.put("/anlas-pool")
async def anlas_pool_put(request: Request):
    """修改参数并立即重算一次。"""
    require_admin(request)
    from . import anlas_pool
    body = await read_json_body(request)
    st = request.app.state.gate
    bounds = {"anlas_auto_enabled": (0, 1), "anlas_reserve": (0, 10000), "anlas_member_daily_cap": (0, 1000),
              "anlas_min_images_7d": (0, 10000), "anlas_min_key_age_days": (0, 365)}
    values = {}
    for name, (low, high) in bounds.items():
        if name in body:
            values[name] = _num(body[name], int, low, high, name)
    if values:
        await st.db.set_settings_bulk(values)
    await anlas_pool.rebalance(st)
    return await anlas_pool_get(request)


@router.get("/shadow")
async def scheduler_shadow(request: Request, hours: int = 24):
    """调度影子模式：用真实请求回放新规则，只计算不执行。hours=24 或 168。"""
    require_admin(request)
    from . import shadow
    hours = 168 if hours >= 168 else 24
    return await shadow.collect(request.app.state.gate, time.time() - hours * 3600)


@router.get("/actions")
async def admin_actions(request: Request, page: int = 1, actor: str = "", action: str = ""):
    require_admin(request)
    per_page = 30
    db = request.app.state.gate.db
    where, args = [], []
    kind = (actor or "").strip()
    if kind == "admin":
        where.append("actor LIKE '后台%'")
    elif kind == "system":
        where.append("actor = '系统'")
    elif kind == "member":
        where.append("actor LIKE 'Discord:%'")
    act = (action or "").strip()
    if act:
        where.append("action = ?")
        args.append(act)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = int((await (await db._db.execute(
        "SELECT COUNT(*) FROM admin_actions" + clause, tuple(args))).fetchone())[0])
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, min(int(page), 1_000_000)), pages)
    rows = [dict(r) for r in await (await db._db.execute(
        "SELECT * FROM admin_actions" + clause + " ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(args) + (per_page, (page - 1) * per_page))).fetchall()]
    action_kinds = [r[0] for r in await (await db._db.execute(
        "SELECT DISTINCT action FROM admin_actions ORDER BY action")).fetchall()]
    return {"actions": rows, "page": page, "per_page": per_page, "total": total, "pages": pages,
            "action_kinds": action_kinds}


@router.get("/overview")
async def overview(request: Request):
    require_admin(request)
    st = request.app.state.gate
    data = await st.db.overview(st.day(), st.week_days(7))
    data["pool"] = await st.nai.status()
    data["pool_configured"] = st.nai.configured
    data["anlas_budget"] = float(await site_flags.get(st.db, site_flags.GLOBAL_MONTHLY_ANLAS, st.settings))
    data["v5_limit"] = await site_flags.get(st.db, site_flags.GLOBAL_DAILY_V5, st.settings)
    data["feature_usage"] = await _feature_usage(st)
    data["log_since"] = (await st.db._db.execute_fetchall("SELECT MIN(ts) FROM usage_log"))[0][0]
    data["economy"] = await ops.economy_enabled(st.db)
    return data


async def _feature_usage(st) -> list[dict]:
    """每项功能今日 / 近 7 天的调用次数（成功 / 失败 / 拒绝）、图片、tokens、Anlas 与使用人数。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    day_start = datetime.fromisoformat(st.day()).replace(tzinfo=ZoneInfo(st.db.tz)).timestamp()
    week_start = day_start - 6 * 86400

    def empty():
        return {"ok": 0, "error": 0, "rejected": 0, "images": 0, "tokens": 0, "anlas": 0.0}

    out = {name: {"id": name, "label": label, "today": empty(), "week": empty()}
           for name, label in feature_defs.FEATURES.items()}
    for period, since in (("today", day_start), ("week", week_start)):
        for row in await st.db.usage_by_kind(since):
            name = feature_defs.KIND_FEATURE.get(row["kind"])
            if name is None:
                continue
            bucket = out[name][period]
            status = row["status"] if row["status"] in ("ok", "rejected") else "error"
            bucket[status] += int(row["n"])
            if row["status"] == "ok":
                bucket["images"] += int(row["images"])
                bucket["tokens"] += int(row["tokens"])
                bucket["anlas"] = round(bucket["anlas"] + float(row["anlas"]), 2)
    return list(out.values())


@router.get("/settings")
async def get_settings(request: Request):
    require_admin(request)
    st = request.app.state.gate
    return {"global_monthly_anlas": float(await site_flags.get(st.db, site_flags.GLOBAL_MONTHLY_ANLAS, st.settings)),
            "global_daily_v5": await site_flags.get(st.db, site_flags.GLOBAL_DAILY_V5, st.settings),
            SETTING: await read_alert_threshold(st.db),
            "v5_capacity": await ops.v5_capacity(st.db, st.settings, getattr(request.app.state, "registrar", None))}


@router.get("/runtime-limits")
async def get_runtime_limits(request: Request):
    require_admin(request)
    return request.app.state.gate.runtime_limits_snapshot()


@router.put("/runtime-limits")
async def put_runtime_limits(request: Request):
    require_admin(request)
    body = await read_json_body(request)
    if not body or set(body) - set(RUNTIME_LIMIT_BOUNDS):
        raise HTTPException(422, "包含未知或空的运行限制设置")
    for name, value in body.items():
        minimum, maximum = RUNTIME_LIMIT_BOUNDS[name]
        if type(value) is not int or not minimum <= value <= maximum:
            raise HTTPException(422, f"{name} 必须是 {minimum}～{maximum} 的整数")
    return await request.app.state.gate.update_runtime_limits(body)


@router.get("/allowance")
async def allowance(request: Request):
    require_admin(request)
    return await request.app.state.gate.nai.allowance.snapshot(request.app.state.gate.nai.pool)


async def _token_payload(request: Request) -> tuple[str, dict]:
    body = await read_json_body(request, limit=4096)
    token = str(body.get("token", "")).strip()
    if not token_store.valid_token(token):
        raise HTTPException(422, "Token 格式不对：应以 pst- 开头（在 NovelAI 的 账号设置 → Get Persistent API Token 里获取）")
    return token, body


def _announce_token_change(request: Request, text: str) -> None:
    alerter = getattr(request.app.state.gate, "alerter", None)
    if alerter is not None:
        alerter.notify("upstream_token_changed", text, cooldown=0)


@router.post("/upstream-tokens")
async def add_upstream_token(request: Request):
    """后台添加一把上游 Token：先向 NovelAI 验证，通过后加入令牌池（保存到权限 600 的文件，不进数据库）。"""
    require_admin(request)
    token, body = await _token_payload(request)
    nai = request.app.state.gate.nai
    check = await nai.verify_token(token)
    if not check["ok"]:
        raise HTTPException(422, check["error"])
    try:
        await nai.add_token(token, allow_anlas=bool(body.get("allow_anlas", False)))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    _announce_token_change(request, f"后台添加了一把上游 Token（订阅等级 {check['tier']}）。如果这不是你操作的，请立刻更换后台密码。")
    return {"ok": True, "tier": check["tier"], "pool": await nai.status()}


@router.put("/upstream-tokens/{token_id}")
async def replace_upstream_token(request: Request, token_id: str):
    """替换某个槽位的 Token（例如在 NovelAI 重置了 Token 后粘贴新的）；V5 日限、启停、并发和当天计数延续。"""
    require_admin(request)
    token, _ = await _token_payload(request)
    nai = request.app.state.gate.nai
    check = await nai.verify_token(token)
    if not check["ok"]:
        raise HTTPException(422, check["error"])
    try:
        await nai.replace_token(token_id, token)
    except LookupError:
        raise HTTPException(404, "上游 Token 不存在") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    _announce_token_change(request, "后台替换了一把上游 Token。如果这不是你操作的，请立刻更换后台密码。")
    return {"ok": True, "tier": check["tier"], "pool": await nai.status()}


@router.delete("/upstream-tokens/{token_id}")
async def remove_upstream_token(request: Request, token_id: str):
    require_admin(request)
    nai = request.app.state.gate.nai
    try:
        removed = await nai.remove_token(token_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    if not removed:
        raise HTTPException(404, "上游 Token 不存在")
    _announce_token_change(request, "后台删除了一把上游 Token。")
    return {"ok": True, "pool": await nai.status()}


@router.put("/upstream-tokens/{token_id}/v5-limit")
async def set_upstream_v5_limit(request: Request, token_id: str):
    require_admin(request)
    body = await read_json_body(request)
    limit = body.get("v5_daily_limit")
    if type(limit) is not int or not 0 <= limit <= 100000:
        raise HTTPException(422, "上游 V5 日限额必须是 0～100000 的整数（0 为不限）")
    if not await request.app.state.gate.nai.set_v5_daily_limit(token_id, limit):
        raise HTTPException(404, "上游 Token 不存在")
    return {"ok": True, "v5_daily_limit": limit}


@router.put("/upstream-tokens/{token_id}/image-concurrency")
async def set_upstream_image_concurrency(request: Request, token_id: str):
    require_admin(request)
    body = await read_json_body(request)
    limit = body.get("image_concurrency")
    if type(limit) is not int or not 1 <= limit <= 4:
        raise HTTPException(422, "上游图片并发必须是 1～4 的整数")
    if not await request.app.state.gate.nai.set_image_concurrency(token_id, limit):
        raise HTTPException(404, "上游 Token 不存在")
    return {"ok": True, "image_concurrency": limit}


@router.put("/upstream-tokens/{token_id}/enabled")
async def set_upstream_enabled(request: Request, token_id: str):
    require_admin(request)
    body = await read_json_body(request)
    enabled = body.get("enabled")
    if type(enabled) is not bool:
        raise HTTPException(422, "enabled 必须是布尔值")
    if not await request.app.state.gate.nai.set_admin_enabled(token_id, enabled):
        raise HTTPException(404, "上游 Token 不存在")
    return {"ok": True, "enabled": enabled}


@router.put("/settings")
async def put_settings(request: Request):
    require_admin(request)
    st = request.app.state.gate
    body = await read_json_body(request)
    threshold = body.get(SETTING, await read_alert_threshold(st.db))
    if type(threshold) is not int or not 1 <= threshold <= 100:
        raise HTTPException(422, "V5 告警阈值必须为 1～100 的整数百分比")
    # 缺省字段保持原值（以前缺省会被写成 0 = 不限）。
    v = _num(body.get("global_monthly_anlas", await site_flags.get(st.db, site_flags.GLOBAL_MONTHLY_ANLAS, st.settings)),
             float, 0.0, 1000000.0, "global_monthly_anlas")
    g5 = _num(body.get("global_daily_v5", await site_flags.get(st.db, site_flags.GLOBAL_DAILY_V5, st.settings)),
              int, 0, 100000, "global_daily_v5")
    await site_flags.put(st.db, site_flags.GLOBAL_MONTHLY_ANLAS, v)
    await site_flags.put(st.db, site_flags.GLOBAL_DAILY_V5, g5)
    await st.db.set_setting(SETTING, threshold)
    return {"ok": True, "global_monthly_anlas": v, "global_daily_v5": g5, SETTING: threshold,
            "v5_capacity": await ops.v5_capacity(st.db, st.settings, getattr(request.app.state, "registrar", None))}


@router.get("/announcement")
async def get_announcement(request: Request):
    require_admin(request)
    p = request.app.state.gate.settings.announcement_path
    if p.exists():
        return {"html": p.read_text(encoding="utf-8")}
    return {"html": ""}


@router.put("/announcement")
async def put_announcement(request: Request):
    require_admin(request)
    body = await read_json_body(request)
    html = str(body.get("html", ""))[:20000]
    request.app.state.gate.settings.announcement_path.write_text(html, encoding="utf-8")
    return {"ok": True}


# ----------------------------------------------------------- 成员与生成记录 ----

# 自动标签：近 24 小时因这些「成员自己能改」的原因被拒 / 失败（按 detail 识别；顺序即优先级）
AUTO_TAG_PATTERNS = (
    ("开着 Vibe", "%Vibe%"),
    ("传了底图", "%img2img%"),
    ("画师串 NaN", "%NaN%"),
    ("角色超 6 个", "%角色太多%"),
    ("请求过快", "%请求过于频繁%"),
    ("调文本接口", "%文本生成%"),
)
TAG_MAX_LEN, TAG_NOTE_MAX = 12, 200


@router.put("/keys/{key_id}/tags")
async def put_key_tag(request: Request, key_id: int):
    """给成员打 / 改手动标签（只在后台显示）。body: {tag, note}"""
    require_admin(request)
    st = request.app.state.gate
    if not await st.db.get_key(key_id):
        raise HTTPException(404, "key 不存在")
    body = await read_json_body(request)
    tag = " ".join(str(body.get("tag") or "").split())[:TAG_MAX_LEN]
    if not tag:
        raise HTTPException(422, "标签不能为空")
    note = str(body.get("note") or "").strip()[:TAG_NOTE_MAX]
    who = str(body.get("by") or "站长").strip()[:20] or "站长"
    await st.db._db.execute(
        "INSERT INTO key_tags(key_id, tag, note, by, ts) VALUES (?,?,?,?,?) "
        "ON CONFLICT(key_id, tag) DO UPDATE SET note=excluded.note, by=excluded.by, ts=excluded.ts",
        (key_id, tag, note, who, time.time()))
    await st.db._db.commit()
    return {"ok": True, "tag": tag}


@router.delete("/keys/{key_id}/tags/{tag}")
async def delete_key_tag(request: Request, key_id: int, tag: str):
    require_admin(request)
    st = request.app.state.gate
    cur = await st.db._db.execute("DELETE FROM key_tags WHERE key_id=? AND tag=?", (key_id, tag))
    await st.db._db.commit()
    if not cur.rowcount:
        raise HTTPException(404, "没有这个标签")
    return {"ok": True}


async def member_tags(db, since: float) -> tuple[dict[int, list], dict[int, list]]:
    """标签：手动（key_tags，站长 / 运维打的）+ 自动（since 之后因已知的成员侧原因被拒 / 失败）。"""
    manual_tags: dict[int, list] = {}
    for kid, tag, note, by, ts in await db._db.execute_fetchall(
            "SELECT key_id, tag, note, by, ts FROM key_tags ORDER BY ts"):
        manual_tags.setdefault(kid, []).append({"tag": tag, "note": note, "by": by, "ts": ts})
    auto_tags: dict[int, list] = {}
    for kid, label, n in await db._db.execute_fetchall(
            "SELECT key_id, CASE "
            + " ".join(f"WHEN detail LIKE '{pat}' THEN '{label}'" for label, pat in AUTO_TAG_PATTERNS)
            + " END AS label, COUNT(*) FROM usage_log WHERE ts>? AND status IN ('rejected','error') "
              "AND key_id IS NOT NULL GROUP BY key_id, label HAVING label IS NOT NULL", (since,)):
        auto_tags.setdefault(kid, []).append({"tag": label, "count": n})
    # 疑似同一人：取自动驾驶最近一次的 alt_link 结果（每 10 分钟算一次）
    try:
        from . import alt_guard
        last = json.loads(await db.get_setting("autopilot_last", "{}") or "{}")
        for p in ((last.get("rules") or {}).get("alt_link") or {}).get("value") or []:
            a, b = p["keys"]
            for me, other, other_name in ((a, b, p["names"][1]), (b, a, p["names"][0])):
                auto_tags.setdefault(me, []).append({
                    "tag": "疑似同一人", "count": p["score"],
                    "note": f"和 #{other} {other_name}：{alt_guard.describe(p['signals'])}（{p['score']} 分，只观察）"})
    except (TypeError, ValueError, KeyError):
        pass
    return manual_tags, auto_tags


@router.get("/members")
async def members(request: Request):
    """每位成员（Key）的今日 / 近 7 天 / 累计用量，以及来源。"""
    require_admin(request)
    st = request.app.state.gate
    today = st.day()
    week = await st.db.member_usage(st.week_days(7)[0])
    totals = await st.db.generated_image_totals()
    reg = {int(r[0]): r for r in await st.db._db.execute_fetchall(
        "SELECT key_id, discord_id, username, display_name, avatar, created_at FROM discord_registrations")}
    since = time.time() - 24 * 3600
    milestones = await st.db.member_milestones(since)
    sources = await st.db.key_source_summary(since)
    clients = await st.db.latest_member_clients()
    out = []
    from .share_guard import decayed
    share_map = {}
    for kid, score, ts, strikes, paused in await st.db._db.execute_fetchall(
            "SELECT key_id, score, score_ts, strikes, paused_until FROM share_state"):
        share_map[kid] = {"score": round(decayed(score, ts, time.time()), 1), "strikes": strikes,
                          "paused_until": paused if paused > time.time() else 0}
    manual_tags, auto_tags = await member_tags(st.db, since)
    # 今日「重置今日额度」痕迹：按服务日起点统计次数与最近一次（从操作日志取，只读）
    import re as _re
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    _tz = getattr(st, "tz", None) or _ZI(st.settings.tz)
    day_start = _dt.now(_tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    reset_map: dict[int, dict] = {}
    for a_ts, a_target in await st.db._db.execute_fetchall(
            "SELECT ts, target FROM admin_actions WHERE action='重置今日额度' AND ok=1 AND ts>=?", (day_start,)):
        mm = _re.match(r"Key #(\d+)", a_target or "")
        if not mm:
            continue
        e = reset_map.setdefault(int(mm.group(1)), {"count": 0, "last_ts": 0.0})
        e["count"] += 1
        if a_ts > e["last_ts"]:
            e["last_ts"] = a_ts
    for row in await st.db.list_keys():
        counter = await st.db.get_counter(row["id"], today)
        w = week.get(row["id"], {})
        out.append({
            "id": row["id"], "name": row["name"], "enabled": bool(row["enabled"]),
            "is_admin": bool(row["is_admin"]), "is_test": bool(row["is_test"]), "discord_id": str(reg[row["id"]][1]) if row["id"] in reg else None,
            "discord": _discord_profile(reg.get(row["id"])),
            "created_at": row["created_at"], "last_used_at": row["last_used_at"],
            "expires_at": row["expires_at"],
            "daily_images": row["daily_images"], "daily_v5": row["daily_v5"],
            "allow_anlas": bool(row["allow_anlas"]), "anlas_auto": row["anlas_auto"] == 1,
            "anlas_mode": "manual" if row["anlas_auto"] == -1 and row["allow_anlas"] else "off" if row["anlas_auto"] == -1 else "auto",
            "daily_anlas": row["daily_anlas"], "image_model_scope": row["image_model_scope"],
            "quota_auto": row["quota_auto"] == 1, "v5_pinned": row["v5_pinned"],
            "today": {"images": counter["images"], "v5": counter["v5"], "anlas": round(float(counter["anlas"]), 2),
                      "text_tokens": counter["text_tokens"], "requests": counter["requests"]},
            "week": {"images": int(w.get("images", 0)), "v5": int(w.get("v5", 0)),
                     "anlas": round(float(w.get("anlas", 0)), 2), "requests": int(w.get("requests", 0))},
            "total_images": totals.get(row["id"], 0),
            # 筛选用：领取时间（Discord 领取优先，否则建 Key 时间）、首次 / 最近成功出图、24h 网段数与被拒次数
            "registered_at": reg[row["id"]][5] if row["id"] in reg else row["created_at"],
            "first_image_at": milestones.get(row["id"], {}).get("first_image_at"),
            "last_image_at": milestones.get(row["id"], {}).get("last_image_at"),
            "rejected_24h": milestones.get(row["id"], {}).get("rejected_24h", 0),
            "rejected_why": milestones.get(row["id"], {}).get("rejected_why", []),
            "sources_24h": len(sources.get(row["id"], [])),
            "share": share_map.get(row["id"]),
            "reset_today": reset_map.get(row["id"]),
            "tags": manual_tags.get(row["id"], []),
            "auto_tags": auto_tags.get(row["id"], []),
            "last_client": clients.get(row["id"], ""),
        })
    return {"members": out, "share_alert_nets": st.settings.key_share_alert_nets,
            "inactivity_days": st.settings.key_inactivity_delete_days}


@router.get("/audit")
async def audit_list(request: Request, key_id: Optional[int] = None, page: int = 1, per_page: int = 24):
    require_admin(request)
    st = request.app.state.gate
    per_page = per_page if per_page in (24, 48, 96) else 24
    page = max(1, min(int(page), 1_000_000))
    rows, total = await st.db.list_audit(per_page, (page - 1) * per_page, key_id)
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:      # 页码超出（跳页太大、或保留期清掉了记录）：回到最后一页，和用量日志 / 操作日志一致
        page = pages
        rows, total = await st.db.list_audit(per_page, (page - 1) * per_page, key_id)
    return {"items": [dict(r) for r in rows], "page": page, "per_page": per_page, "total": total, "pages": pages}


_THUMB_CACHE: "OrderedDict[int, bytes]" = OrderedDict()   # 现场缩图约 70ms/张，翻页回来不再重算；约 400×55KB ≈ 22MB


@router.get("/audit/{audit_id}/thumb")
async def audit_thumb(request: Request, audit_id: int):
    """画廊用的小图：不再另存缩略图，从原图现场缩小（浏览器缓存 1 小时）；旧记录仍有存好的缩略图就直接用。"""
    require_admin(request)
    db = request.app.state.gate.db
    data = _THUMB_CACHE.get(audit_id)
    if data is not None:
        _THUMB_CACHE.move_to_end(audit_id)
    else:
        data = await db.audit_thumb(audit_id)
    if data is None:
        image, _ = await db.audit_image(audit_id)
        if image is not None:
            from .audit import make_thumbnail
            data = await anyio.to_thread.run_sync(make_thumbnail, image)
            if data is not None:
                _THUMB_CACHE[audit_id] = data
                while len(_THUMB_CACHE) > 400:
                    _THUMB_CACHE.popitem(last=False)
    if data is None:
        raise HTTPException(404, "没有图片（原图已过期或未记录）")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/audit/{audit_id}/image")
async def audit_image(request: Request, audit_id: int):
    """原图（清晰版）。没有原图时回退到缩略图。"""
    db = request.app.state.gate.db
    require_admin(request)
    data, ctype = await db.audit_image(audit_id)
    if data is None:
        data = await db.audit_thumb(audit_id)
        ctype = "image/jpeg"
    if data is None:
        raise HTTPException(404, "没有图片")
    return Response(data, media_type=ctype or "image/png", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/audit/export")
async def audit_export(request: Request, key_id: Optional[int] = None):
    """把某成员的全部原图打包成 zip 下载（附 prompts.txt）。"""
    require_admin(request)
    if key_id is None:
        raise HTTPException(400, "缺少 key_id")
    db = request.app.state.gate.db
    rows = await db.audit_images_for(key_id)
    if not rows:
        raise HTTPException(404, "该成员没有可导出的原图")
    name = await db._db.execute_fetchall("SELECT name FROM api_keys WHERE id=?", (key_id,))
    who = (name[0][0] if name else str(key_id)) or str(key_id)
    from . import exporter
    blob = await run_in_threadpool(exporter.build_zip, rows, who)
    fname = f"owl-{_safe_filename(who)}-{len(rows)}.zip"
    return Response(blob, media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=\"{fname}\"", "Cache-Control": "no-store"})


_LAST_LOG_EXPORT = 0.0


@router.get("/logs/export")
async def logs_export(request: Request, days: int = 30):
    """客户端日志导出（zip：请求、Key、网段、防分享证据），给站长定期独立复核。只读，不含提示词和 Key 原文。"""
    require_admin(request)
    from . import client_export
    # 全量读会占住唯一的主连接、拖慢出图记账：最多 90 天、每分钟 1 次（10/11 审查）
    days = max(1, min(int(days or 90), 90))
    global _LAST_LOG_EXPORT
    if time.time() - _LAST_LOG_EXPORT < 60:
        raise HTTPException(429, "导出太频繁，请 1 分钟后再试")
    _LAST_LOG_EXPORT = time.time()
    data = await client_export.collect(request.app.state.gate.db, days)
    blob = await run_in_threadpool(client_export.build_zip, data)
    stamp = time.strftime("%Y%m%d-%H%M")
    await log_action(request.app.state.gate.db, _actor(request), "导出客户端日志", f"{days or '全部'}天")
    return Response(blob, media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=\"owl-client-logs-{stamp}.zip\"",
                             "Cache-Control": "no-store"})


@router.get("/feedback")
async def feedback_list(request: Request):
    """成员 /反馈 提交的问卷，最新在前。"""
    require_admin(request)
    from . import feedback
    return {"questions": feedback.QUESTIONS, "items": await feedback.listing(request.app.state.gate.db, 500)}


@router.get("/feedback/export")
async def feedback_export(request: Request):
    require_admin(request)
    from . import feedback
    blob = feedback.to_csv(await feedback.listing(request.app.state.gate.db, 100000))
    return Response(blob, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=\"owl-feedback-{time.strftime('%Y%m%d-%H%M')}.csv\"",
                             "Cache-Control": "no-store"})


@router.get("/status")
async def server_status(request: Request):
    """告警与记录功能的当前状态（不含任何密钥）。"""
    require_admin(request)
    st = request.app.state.gate
    cfg = st.settings
    usage = shutil.disk_usage(cfg.data_dir)
    return {
        "alerts": {"configured": st.alerter.configured, "sent": st.alerter.sent},
        "audit": {**dict(zip(("prompts", "thumbs", "retention_days"), await ops.audit_flags(st.db, cfg))),
                  "image_retention_days": await audit_image_days(st.db)},
        "upstream": st.upstream_health(),
        "protection": {"auth_fail_max": cfg.auth_fail_max, "auth_fail_window": cfg.auth_fail_window,
                       "auth_block_seconds": cfg.auth_block_seconds,
                       "login_max_attempts": cfg.login_max_attempts},
        "disk": {"free_mb": usage.free // 2**20, "total_mb": usage.total // 2**20},
    }


@router.post("/alerts/test")
async def alerts_test(request: Request):
    require_admin(request)
    alerter = request.app.state.gate.alerter
    if not alerter.configured:
        raise HTTPException(409, "尚未配置告警渠道（ALERT_USER_ID / ALERT_CHANNEL_ID / ALERT_WEBHOOK_URL）")
    alerter.notify("test", "这是一条测试告警，收到说明告警渠道正常。", cooldown=0)
    return {"ok": True}


# ----------------------------------------------------------- 运行中开关 ----

async def _ops_snapshot(request: Request) -> dict:
    st = request.app.state.gate
    service = getattr(request.app.state, "registrar", None)
    reg = await ops.registration_settings(st.db, service)
    reg["active"] = await service.count_active() if service else 0
    reg["reset_at"] = service.reset_at if service else ""
    reg["v5_capacity"] = await ops.v5_capacity(st.db, st.settings, service)
    reg["waitlist"] = await service.waitlist() if service else []
    prompts, thumbs, days = await ops.audit_flags(st.db, st.settings)
    return {
        "registration": reg,
        "features": {"global": await feature_defs.global_flags(st.db), "labels": feature_defs.FEATURES,
                     # 后台“新建 Key”弹窗的默认功能，与服务端缺省逻辑一致
                     "new_key": feature_defs.parse_list(await _new_key_features(request, {}))},
        "audit": {"prompts": prompts, "thumbs": thumbs, "retention_days": days,
                  "image_retention_days": await audit_image_days(st.db),
                  "announce_configured": st.announcer.configured},
        "upstream": st.upstream_health(),
    }


@router.get("/ops")
async def ops_get(request: Request):
    require_admin(request)
    return await _ops_snapshot(request)


@router.put("/ops/registration")
async def ops_set_registration(request: Request):
    """开放 / 关闭自助注册、名额上限、新成员默认功能与额度；机器人下一条命令即生效。"""
    require_admin(request)
    body = await read_json_body(request)
    if "features" in body and body["features"] is not None:
        if not isinstance(body["features"], list) or any(v not in feature_defs.FEATURES for v in body["features"]):
            raise HTTPException(422, "features 必须是功能名列表")
    try:
        await ops.set_registration(request.app.state.gate.db, body, request.app.state.gate,
                                   getattr(request.app.state, "registrar", None))
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, str(exc) or "参数无效") from None
    return await _ops_snapshot(request)


@router.put("/ops/features")
async def ops_set_features(request: Request):
    """全局功能开关：{"flags": {"text": false, ...}}。关闭后所有成员（管理员 Key 除外）立即不可用。"""
    require_admin(request)
    body = await read_json_body(request)
    flags = body.get("flags")
    if not isinstance(flags, dict) or not flags or any(
            k not in feature_defs.FEATURES or type(v) is not bool for k, v in flags.items()):
        raise HTTPException(422, "flags 必须是 {功能名: true/false}")
    await ops.set_global_features(request.app.state.gate.db, flags)
    return await _ops_snapshot(request)


@router.put("/ops/economy")
async def ops_set_economy(request: Request):
    """站长手动开 / 关节约模式（全站免费档 14 步 + Euler-a）。变化会在成员公告频道通知。"""
    require_admin(request)
    body = await read_json_body(request)
    on = str(body.get("on")).strip().lower() in ("1", "true", "yes", "on") if not isinstance(body.get("on"), bool) else body["on"]
    changed = await ops.set_economy(request.app.state.gate.db, on, request.app.state.gate, by="站长")
    return {"economy": on, "changed": changed}


@router.put("/ops/audit")
async def ops_set_audit(request: Request):
    """开关生成记录。记录范围变化且 notify（默认 true）时，在成员公告频道发布数据记录说明。"""
    require_admin(request)
    body = await read_json_body(request)
    try:
        await ops.set_audit(request.app.state.gate, body)
    except (TypeError, ValueError):
        raise HTTPException(422, "参数无效") from None
    return await _ops_snapshot(request)
