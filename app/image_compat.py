"""Normalize known client transport variants, never relax quotas or cache-only rules.

st-chatu8 sends UUID cache identifiers and mobile JPEG precise references.
Older Launcher versions send the official uncached precise-reference array.
The canonical upstream representation remains a complete PNG plus a 64-hex key.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import hmac
import io
import json
import re

from PIL import Image

from .image_tools import dimensions
from .policy import REFERENCE_LIMIT

_HEX_KEY = re.compile(r"[0-9a-f]{64}")
_UUID_KEY = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def _cache_key(namespace: str, field: str, data: str) -> str:
    # Bind adapted cache entries to this authenticated virtual key and content.
    # Never use a UUID alone: two users can choose the same UUID for other images.
    return hmac.new(namespace.encode(), (field + "\0" + data).encode(), hashlib.sha256).hexdigest()


def _precise_png(data: str) -> str:
    if not isinstance(data, str) or not data or len(data) > 25 * 1024 * 1024:
        raise ValueError("精确参考必须包含完整 Base64 图片")
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("精确参考必须包含有效 Base64 图片") from None
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return data  # Preserve official PNG data and metadata byte for byte.
    if not (raw.startswith(b"\xff\xd8\xff") or
            raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"):
        raise ValueError("精确参考只支持 PNG、JPEG 或 WebP 静态图片")
    dimensions(raw)  # Bound dimensions and decode before allocating a conversion.
    with Image.open(io.BytesIO(raw)) as image, io.BytesIO() as output:
        image.convert("RGBA" if "A" in image.getbands() else "RGB").save(output, format="PNG")
        return base64.b64encode(output.getvalue()).decode("ascii")


def normalize_image_references(payload: dict, namespace: str, *,
                               max_bytes: int = 25 * 1024 * 1024) -> dict:
    """Return a copy; invalid/cache-only entries still fail the existing policy.

    Full official 64-hex PNG entries are not rewritten. Converted or legacy
    entries receive content-bound, key-scoped identifiers. No image is persisted.
    """
    out = copy.deepcopy(payload)
    p = out.get("parameters", {})
    if not isinstance(p, dict):
        raise ValueError("parameters 必须是 JSON 对象")
    raw_precise = p.get("director_reference_images", [])
    cached_precise = p.get("director_reference_images_cached", [])
    if not isinstance(raw_precise, list) or not isinstance(cached_precise, list):
        raise ValueError("精确参考图片字段必须是数组")
    if raw_precise and cached_precise:
        raise ValueError("精确参考原始数组与缓存数组不能同时提交")
    if len(raw_precise) > REFERENCE_LIMIT or len(cached_precise) > REFERENCE_LIMIT:
        raise ValueError("每次最多使用 16 张参考图")
    changed = False
    if raw_precise:
        cached_precise = []
        for image in raw_precise:
            data = _precise_png(image)
            cached_precise.append({"data": data,
                "cache_secret_key": _cache_key(namespace, "precise", data)})
        p["director_reference_images_cached"] = cached_precise
        del p["director_reference_images"]
        changed = True
    for field in ("reference_image_multiple_cached", "director_reference_images_cached"):
        entries = p.get(field, [])
        if not isinstance(entries, list):
            raise ValueError(f"{field} 必须是数组")
        if len(entries) > REFERENCE_LIMIT:
            raise ValueError("每次最多使用 16 张参考图")
        for entry in entries:
            if not isinstance(entry, dict):
                continue  # Existing validation supplies a safe error.
            key, data = entry.get("cache_secret_key"), entry.get("data")
            if not isinstance(key, str) or not isinstance(data, str) or not data:
                continue  # Never turn a cache-only pointer into a trusted hit.
            if not (_HEX_KEY.fullmatch(key) or _UUID_KEY.fullmatch(key)):
                continue  # Arbitrary strings are not a supported client variant.
            if field == "director_reference_images_cached":
                converted = _precise_png(data)
                if converted != data:
                    entry["data"] = converted
                    data = converted
                    entry["cache_secret_key"] = _cache_key(namespace, field, data)
                    changed = True
            if _UUID_KEY.fullmatch(key):
                entry["cache_secret_key"] = _cache_key(namespace, field, data)
                changed = True
    if changed:
        # JPEG -> PNG expansion must not bypass the request memory boundary.
        encoded = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > max_bytes:
            raise ValueError("参考图转换后的请求体过大，请缩小图片或减少参考数量")
    return out
