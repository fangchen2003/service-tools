"""Private bot bridge and public OAuth callback; never render API keys to a browser."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .registration import RegistrationError

router = APIRouter(prefix="/self-register")


class Intent(BaseModel):
    discord_id: str
    guild_id: str


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
