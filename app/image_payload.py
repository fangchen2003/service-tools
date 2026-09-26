"""Resolve Launcher multipart image pointers before normal Gate validation."""
import asyncio
import base64
import json
import re

from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile

from .body import read_bounded_body, read_json_body
from .nai import _wait_cleanup
from .policy import REFERENCE_LIMIT


_PART_NAME = re.compile(r"(?:image|mask|reference_image|ref_multiple_[0-9]+|director_ref_[0-9]+)")


async def read_image_body(request: Request, limit: int) -> dict:
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        return await read_json_body(request, limit)
    body = await read_bounded_body(request, limit)

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def parse():
        try:
            return await Request(request.scope, receive).form(max_files=REFERENCE_LIMIT + 3, max_fields=10)
        except Exception:
            raise HTTPException(400, "multipart 请求格式无效或过大") from None

    # Let a bounded parse finish on cancellation, then close every upload. Otherwise
    # cancellation between spooling and returning FormData can orphan temp files.
    parsing = asyncio.create_task(parse())
    try:
        form = await asyncio.shield(parsing)
        items = form.multi_items()
        if len({name for name, _ in items}) != len(items):
            raise HTTPException(400, "multipart 字段不能重名")
        part = form.get("request")
        if isinstance(part, UploadFile):
            raw = await part.read()
        elif isinstance(part, str):
            raw = part.encode("utf-8")
        else:
            raise HTTPException(400, "multipart 请求缺少 request JSON 字段")
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            raise HTTPException(400, "multipart request 字段不是合法 JSON") from None
        if not isinstance(data, dict):
            raise HTTPException(400, "multipart request 字段必须是 JSON 对象")
        attachments = {name: part for name, part in items if name != "request"}
        if any(not _PART_NAME.fullmatch(name) or not isinstance(part, UploadFile)
               for name, part in attachments.items()):
            raise HTTPException(400, "multipart 包含不支持的图片附件字段")
        used, encoded = set(), {}
        expanded_size = len(raw)

        async def resolve(container, field):
            nonlocal expanded_size
            value = container.get(field)
            if not isinstance(value, str) or not value:
                return
            if value not in attachments:
                if _PART_NAME.fullmatch(value):
                    raise HTTPException(400, "multipart 缺少图片字段引用的附件")
                return  # Existing inline Base64 is checked by the route's policy.
            upload = attachments[value]
            size = 4 * (((upload.size or 0) + 2) // 3)
            expanded_size += size - len(value)
            if expanded_size > limit:
                raise HTTPException(413, "附件展开后的请求体过大")
            if value not in encoded:
                encoded[value] = base64.b64encode(await upload.read()).decode("ascii")
                if not encoded[value]:
                    raise HTTPException(400, "图片附件不能为空")
            container[field] = encoded[value]
            used.add(value)

        # Resolve only image slots, never arbitrary strings/JSON paths or filenames.
        containers = [data]
        if isinstance(data.get("parameters"), dict):
            containers.append(data["parameters"])
        for container in containers:
            for field in ("image", "mask", "reference_image"):
                await resolve(container, field)
            for field in ("reference_image_multiple_cached", "director_reference_images_cached"):
                values = container.get(field, [])
                if isinstance(values, list):
                    if len(values) > REFERENCE_LIMIT:
                        raise HTTPException(400, "每次最多使用 16 张参考图")
                    for item in values:
                        if isinstance(item, dict):
                            await resolve(item, "data")
        if used != attachments.keys():
            raise HTTPException(400, "multipart 包含未被图片字段引用的附件")
        return data
    finally:
        async def close():
            try:
                form = await parsing
            except Exception:
                return
            await form.close()
        await _wait_cleanup(asyncio.create_task(close()))
