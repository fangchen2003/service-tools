"""Admin registration controls. Included separately by the application router."""
import hmac
from contextlib import nullcontext

from fastapi import APIRouter, HTTPException, Request, Response

from .admin import COOKIE, _secret, _sign, require_admin
from .body import read_json_body
from .registration import registration_status

router = APIRouter(prefix="/admin/api")


def _csrf_token(request: Request) -> str:
    return _sign(_secret(request), "registration:" + request.cookies[COOKIE])


async def _status(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    status = await registration_status(request.app.state.gate.db)
    return {**status, "configured": getattr(request.app.state, "registrar", None) is not None,
            "csrf_token": _csrf_token(request)}


@router.get("/registration")
async def get_registration(request: Request, response: Response):
    require_admin(request)
    return await _status(request, response)


@router.put("/registration")
async def put_registration(request: Request, response: Response):
    require_admin(request)
    csrf = request.headers.get("x-nai-admin-csrf", "")
    # Constant-time bytes comparison also rejects non-ASCII input safely.
    if not hmac.compare_digest(csrf.encode(), _csrf_token(request).encode()):
        raise HTTPException(403, "CSRF 校验失败，请刷新管理页面")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "Content-Type 必须是 application/json")
    body = await read_json_body(request)
    if set(body) != {"enabled", "daily_limit"}:
        raise HTTPException(422, "必须且只能提供 enabled 和 daily_limit")
    if type(body["enabled"]) is not bool:
        raise HTTPException(422, "enabled 必须是布尔值")
    if type(body["daily_limit"]) is not int or not 0 <= body["daily_limit"] <= 100000:
        raise HTTPException(422, "daily_limit 必须是 0 到 100000 的整数")
    db = request.app.state.gate.db
    service = getattr(request.app.state, "registrar", None)
    # Linearize policy changes with final admission, issuance, and DM rollback.
    async with service.lock if service is not None else nullcontext():
        # An isolated transaction cannot be prematurely committed by another
        # task using Database's shared connection. Failure rolls back both.
        async with db._open_connection() as connection:
            await connection.executemany(
                """INSERT INTO site_settings(key,value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                [("registration_enabled", str(body["enabled"])),
                 ("registration_daily_limit", str(body["daily_limit"]))])
            await connection.commit()
        return await _status(request, response)
