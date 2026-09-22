"""Bound request bytes before decoding JSON or parsing multipart data."""
import json

from fastapi import HTTPException, Request
from starlette.requests import ClientDisconnect


async def read_bounded_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isascii() or not declared.isdecimal():
            raise HTTPException(400, "Content-Length 无效")
        digits = declared.lstrip("0") or "0"
        # Compare digit counts first: huge numeric headers must not reach int().
        if len(digits) > len(str(limit)) or int(digits) > limit:
            raise HTTPException(413, "请求体过大")
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                raise HTTPException(413, "请求体过大")
            body.extend(chunk)
    except ClientDisconnect:
        raise HTTPException(400, "请求体传输中断") from None
    return bytes(body)


async def read_json_body(request: Request, limit: int = 1024 * 1024) -> dict:
    raw = await read_bounded_body(request, limit)
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise HTTPException(400, "请求体不是合法 JSON") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")
    return data
