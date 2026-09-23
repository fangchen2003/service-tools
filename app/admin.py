"""管理后台 API。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, Response

from .policy import gen_key
from .body import read_json_body
from .allowance import SETTING, read_alert_threshold

router = APIRouter(prefix="/admin/api")

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
    if f.exists():
        s.secret_key = f.read_text().strip()
    else:
        s.secret_key = os.urandom(32).hex()
        f.write_text(s.secret_key)
    return s.secret_key


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session_cookie(request: Request) -> str:
    secret = _secret(request)
    payload = str(int(time.time()) + 7 * 86400)
    return payload + "." + _sign(secret, payload)


def check_session(request: Request) -> bool:
    secret = _secret(request)
    raw = request.cookies.get(COOKIE, "")
    if "." not in raw:
        return False
    payload, sig = raw.split(".", 1)
    if not hmac.compare_digest(sig, _sign(secret, payload)):
        return False
    try:
        return int(payload) > time.time()
    except ValueError:
        return False


def require_admin(request: Request) -> None:
    if not check_session(request):
        raise HTTPException(401, "未登录或会话已过期")


# ----------------------------------------------------------------- routes ----

@router.post("/login")
async def login(request: Request, response: Response):
    if not await request.app.state.gate.hit_login(_client_id(request)):
        raise HTTPException(429, "登录尝试过于频繁，请稍后再试")
    body = await read_json_body(request)
    password = str(body.get("password", ""))
    if not request.app.state.gate.settings.admin_password:
        raise HTTPException(503, "尚未设置 ADMIN_PASSWORD 环境变量，管理端已锁定")
    if not hmac.compare_digest(password, request.app.state.gate.settings.admin_password):
        raise HTTPException(401, "密码错误")
    response.set_cookie(
        COOKIE, make_session_cookie(request),
        httponly=True, secure=request.app.state.gate.settings.admin_cookie_secure,
        samesite="strict", max_age=7 * 86400,
    )
    return {"ok": True}


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


def _key_json(row, counter) -> dict[str, Any]:
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
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "used": {
            "images": counter["images"],
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
    out = []
    for r in rows:
        c = await st.db.get_counter(r["id"], today)
        out.append(_key_json(r, c))
    return {"keys": out}


@router.post("/keys")
async def create_key(request: Request):
    require_admin(request)
    st = request.app.state.gate
    body = await read_json_body(request)

    def _int_field(name: str, default: int, lo: int, hi: int) -> int:
        try:
            v = int(body.get(name, default))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    daily_images = _int_field("daily_images", st.settings.default_daily_images, 0, 1000000)
    monthly_anlas = float(body.get("monthly_anlas", st.settings.default_monthly_anlas) or 0)
    monthly_anlas = max(0.0, min(monthly_anlas, 100000.0))
    daily_anlas = float(body.get("daily_anlas", st.settings.default_daily_anlas) or 0)
    daily_anlas = max(0.0, min(daily_anlas, 100000.0))
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
        "image_model_scope": image_model_scope,
        "expires_at": expires_at,
    })
    c = await st.db.get_counter(row["id"], st.day())
    return {"key": _key_json(row, c)}


@router.post("/keys/{key_id}/regenerate")
async def regenerate_key(request: Request, response: Response, key_id: int):
    require_admin(request)
    token = gen_key("nai")
    if not await request.app.state.gate.db.rotate_key_token(key_id, token):
        raise HTTPException(404, "key 不存在")
    response.headers["Cache-Control"] = "no-store"
    return {"token": token}


@router.patch("/keys/{key_id}")
async def patch_key(request: Request, key_id: int):
    require_admin(request)
    st = request.app.state.gate
    if not await st.db.get_key(key_id):
        raise HTTPException(404, "key 不存在")
    body = await read_json_body(request)
    fields: dict[str, Any] = {}
    if "name" in body:
        fields["name"] = str(body["name"] or "").strip()[:60] or "未命名"
    if "enabled" in body:
        fields["enabled"] = bool(body["enabled"])
    if "daily_images" in body:
        fields["daily_images"] = max(0, min(int(body["daily_images"]), 1000000))
    if "daily_anlas" in body:
        fields["daily_anlas"] = max(0.0, min(float(body["daily_anlas"] or 0), 100000.0))
    if "daily_v5" in body:
        fields["daily_v5"] = max(0, min(int(body["daily_v5"]), 100000))
    if "monthly_anlas" in body:
        fields["monthly_anlas"] = max(0.0, min(float(body["monthly_anlas"] or 0), 100000.0))
    if "daily_text_tokens" in body:
        fields["daily_text_tokens"] = max(0, min(int(body["daily_text_tokens"]), 100_000_000))
    if "rpm" in body:
        fields["rpm"] = max(1, min(int(body["rpm"]), 600))
    if "allow_anlas" in body:
        fields["allow_anlas"] = bool(body["allow_anlas"])
    if "allow_img2img" in body:
        fields["allow_img2img"] = bool(body["allow_img2img"])
    if "exclude_global_v5" in body:
        fields["exclude_global_v5"] = bool(body["exclude_global_v5"])
    if "image_model_scope" in body:
        fields["image_model_scope"] = "all" if body["image_model_scope"] == "all" else "legacy"
    if "expires_days" in body:
        d = max(0, int(body["expires_days"]))
        fields["expires_at"] = (time.time() + d * 86400) if d > 0 else None
    await st.db.update_key(key_id, fields)
    row = await st.db.get_key(key_id)
    c = await st.db.get_counter(key_id, st.day())
    return {"key": _key_json(row, c)}


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
    return {"ok": True, "key": _key_json(row, counter)}


@router.delete("/keys/{key_id}")
async def delete_key(request: Request, key_id: int):
    require_admin(request)
    await request.app.state.gate.db.delete_key(key_id)
    return {"ok": True}


@router.get("/logs")
async def logs(request: Request, key_id: Optional[int] = None, page: int = 1):
    require_admin(request)
    per_page = 20
    page = max(1, min(int(page), 1_000_000))
    db = request.app.state.gate.db
    total = await db.count_logs(key_id=key_id)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    rows = await db.list_logs(limit=per_page, offset=(page - 1) * per_page,
                              key_id=key_id)
    return {
        "logs": [dict(r) for r in rows],
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": pages,
    }


@router.get("/overview")
async def overview(request: Request):
    require_admin(request)
    st = request.app.state.gate
    data = await st.db.overview(st.day(), st.week_days(7))
    data["pool"] = await st.nai.status()
    data["pool_configured"] = st.nai.configured
    budget = await st.db.get_setting("global_monthly_anlas", st.settings.global_monthly_anlas)
    data["anlas_budget"] = float(budget or 0)
    v5lim = await st.db.get_setting("global_daily_v5", st.settings.global_daily_v5)
    data["v5_limit"] = int(float(v5lim or 0))
    return data


@router.get("/settings")
async def get_settings(request: Request):
    require_admin(request)
    st = request.app.state.gate
    v = await st.db.get_setting("global_monthly_anlas", st.settings.global_monthly_anlas)
    v5 = await st.db.get_setting("global_daily_v5", st.settings.global_daily_v5)
    return {"global_monthly_anlas": float(v or 0), "global_daily_v5": int(float(v5 or 0)),
            SETTING: await read_alert_threshold(st.db)}


@router.get("/allowance")
async def allowance(request: Request):
    require_admin(request)
    return await request.app.state.gate.nai.allowance.snapshot(request.app.state.gate.nai.pool)


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
    v = float(body.get("global_monthly_anlas", 0) or 0)
    v = max(0.0, min(v, 1000000.0))
    await st.db.set_setting("global_monthly_anlas", v)
    g5 = int(body.get("global_daily_v5", 0) or 0)
    g5 = max(0, min(g5, 100000))
    await st.db.set_setting("global_daily_v5", g5)
    await st.db.set_setting(SETTING, threshold)
    return {"ok": True, "global_monthly_anlas": v, "global_daily_v5": g5, SETTING: threshold}


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
