"""Private bot bridge and public OAuth callback; never render API keys to a browser."""
from __future__ import annotations

import hmac
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .registration import RegistrationError
from .registration import COMMAND_GUILD

router = APIRouter(prefix="/self-register")


class Intent(BaseModel):
    discord_id: str
    guild_id: str


@router.post("/find")
async def find(request: Request, body: Intent):
    service = getattr(request.app.state, "registrar", None)
    if service is None:
        raise HTTPException(503, "自助服务尚未配置")
    if not hmac.compare_digest(request.headers.get("Authorization", ""),
                               "Bearer " + service.bridge_secret):
        raise HTTPException(401, "未授权")
    try:
        status = await service.find(body.discord_id, body.guild_id)
    except RegistrationError as exc:
        raise HTTPException(403, str(exc)) from exc
    return JSONResponse({"status": status}, headers={"Cache-Control": "no-store"})


@router.post("/charge")
async def charge(request: Request, body: Intent):
    service = getattr(request.app.state, "registrar", None)
    if service is None:
        raise HTTPException(503, "自助服务尚未配置")
    if not hmac.compare_digest(request.headers.get("Authorization", ""),
                               "Bearer " + service.bridge_secret):
        raise HTTPException(401, "未授权")
    if body.guild_id != COMMAND_GUILD or not body.discord_id.isdecimal():
        raise HTTPException(403, "请在指定服务器使用 /charge。")
    day = datetime.now(ZoneInfo(service.db.tz)).strftime("%Y-%m-%d")
    result = await service.db.charge_registered_key(body.discord_id, day)
    if result == "not_registered":
        raise HTTPException(403, "当前账号没有有效的自助注册 Key。")
    return JSONResponse({"status": result}, headers={"Cache-Control": "no-store"})


@router.post("/vibe")
async def vibe(request: Request, body: Intent):
    service = getattr(request.app.state, "registrar", None)
    if service is None:
        raise HTTPException(503, "自助服务尚未配置")
    if not hmac.compare_digest(request.headers.get("Authorization", ""),
                               "Bearer " + service.bridge_secret):
        raise HTTPException(401, "未授权")
    if body.guild_id != COMMAND_GUILD or not body.discord_id.isdecimal():
        raise HTTPException(403, "请在指定服务器使用 /vibe。")
    result = await service.db.claim_registered_vibe(body.discord_id)
    if result["status"] == "not_registered":
        raise HTTPException(403, "当前账号没有有效的自助注册 Key。")
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/intent")
async def intent(request: Request, body: Intent):
    service = getattr(request.app.state, "registrar", None)
    if service is None:
        raise HTTPException(503, "自助注册尚未配置")
    given = request.headers.get("Authorization", "")
    if not hmac.compare_digest(given, "Bearer " + service.bridge_secret):
        raise HTTPException(401, "未授权")
    try:
        url = await service.begin(body.discord_id, body.guild_id)
    except RegistrationError as exc:
        raise HTTPException(403, str(exc)) from exc
    return JSONResponse({"url": url}, headers={"Cache-Control": "no-store"})


@router.get("/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if state.startswith("wb."):
        from .workbench_auth import callback as workbench_callback
        return await workbench_callback(request, code, state, error)
    service = getattr(request.app.state, "registrar", None)
    if service is None:
        raise HTTPException(503, "自助注册尚未配置")
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
               "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"}
    if error:
        return HTMLResponse("Discord 授权未完成，请重新使用 /register。", status_code=400, headers=headers)
    try:
        await service.finish(code, state)
    except RegistrationError as exc:
        return HTMLResponse(str(exc), status_code=403, headers=headers)
    return HTMLResponse("注册成功。API Key 和网址已发送到你的 Discord 私信，请勿分享 Key。", headers=headers)
