"""Bound request bytes before decoding JSON or parsing multipart data."""
import json

import anyio

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


MAX_JSON_DEPTH = 32
_THREAD_PARSE_BYTES = 256 * 1024


def _reject_constant(name: str):
    raise ValueError(f"不支持 {name}")


def _too_deep(value, limit: int = MAX_JSON_DEPTH) -> bool:
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            children = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        if depth > limit:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def parse_json(raw: bytes | str):
    """严格 JSON：拒绝 NaN/Infinity（上游序列化会失败）与过深嵌套（deepcopy/递归会崩）。"""
    data = json.loads(raw, parse_constant=_reject_constant)
    if _too_deep(data):
        raise ValueError("JSON 嵌套过深")
    return data


async def parse_json_body(raw: bytes) -> dict:
    try:
        if len(raw) > _THREAD_PARSE_BYTES:
            # 大请求体放到线程里解析，避免阻塞事件循环拖慢其他成员。
            data = await anyio.to_thread.run_sync(parse_json, raw)
        else:
            data = parse_json(raw)
    except (ValueError, RecursionError):
        raise HTTPException(400, "请求体不是合法 JSON") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")
    return data


async def read_json_body(request: Request, limit: int = 1024 * 1024) -> dict:
    return await parse_json_body(await read_bounded_body(request, limit))
