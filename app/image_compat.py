"""Normalize complete precise references; preserve upstream Vibe identifiers."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import json
import re

from PIL import Image

from .image_tools import dimensions
from .policy import validate_image_references

_HEX_KEY = re.compile(r"[0-9a-f]{64}")
_SIZE_ERROR = "参考图转换后的请求体过大，请缩小图片或减少参考数量"


def needs_reference_normalization(payload: dict) -> bool:
    """Canonical PNG references can retain the existing streaming admission."""
    p = payload.get("parameters", {})
    if p.get("director_reference_images"):
        return True
    return any(not _HEX_KEY.fullmatch(item["cache_secret_key"])
               or not item["data"].startswith("iVBORw0KGgo")
               for item in p.get("director_reference_images_cached", []))


def _cache_key(namespace: str, field: str, data: str) -> str:
    digest = hmac.new(namespace.encode(), (field + "\0").encode(), hashlib.sha256)
    # Avoid another full-size copy of an adapted image for hashing.
    for start in range(0, len(data), 65536):
        digest.update(data[start:start + 65536].encode("ascii"))
    return digest.hexdigest()


class _BoundedPNG(io.BytesIO):
    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise ValueError(_SIZE_ERROR)
        return super().write(data)


def _precise_png(data: str, *, max_chars: int) -> str:
    if not isinstance(data, str) or not data or len(data) > 25 * 1024 * 1024:
        raise ValueError("精确参考必须包含完整 Base64 图片")
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("精确参考必须包含有效 Base64 图片") from None
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) > max_chars:
            raise ValueError(_SIZE_ERROR)
        return data  # Preserve official PNG data and metadata byte for byte.
    if not (raw.startswith(b"\xff\xd8\xff") or
            raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"):
        raise ValueError("精确参考只支持 PNG、JPEG 或 WebP 静态图片")
    dimensions(raw)  # Check pixels and frames before allocating a conversion.
    # A Base64 group of four characters encodes at most three PNG bytes.
    with Image.open(io.BytesIO(raw)) as image, _BoundedPNG(3 * (max_chars // 4)) as output:
        with image.convert("RGBA" if "A" in image.getbands() else "RGB") as converted:
            converted.save(output, format="PNG")
        with output.getbuffer() as png:
            return base64.b64encode(png).decode("ascii")


def _json_size(payload: dict, limit: int) -> int:
    """Count metadata without materializing another entire JSON request."""
    size = 0
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
    for chunk in encoder.iterencode(payload):
        for start in range(0, len(chunk), 65536):
            size += len(chunk[start:start + 65536].encode("utf-8"))
            if size > limit:
                raise ValueError(_SIZE_ERROR)
    return size


def normalize_image_references(payload: dict, namespace: str, *,
                               max_bytes: int = 25 * 1024 * 1024) -> dict:
    """Adapt precise references within an incremental serialized-size budget.

    Vibe entries, including UUIDv4 identifiers, pass through unchanged. Precise
    conversions and legacy identifiers get content-bound, Key-scoped hashes.
    Only changed containers are copied; source images are never persisted.
    """
    problem = validate_image_references(payload, transport_only=True)
    if problem:
        raise ValueError(problem)
    out = dict(payload)
    p = out["parameters"] = dict(payload.get("parameters", {}))
    raw = p.get("director_reference_images", [])
    field = "director_reference_images_cached"
    originals = p.get(field, [])
    if raw:
        originals = [{"data": data} for data in raw]
        del p["director_reference_images"]
    if not originals:
        return out

    # Reserve metadata/key overhead once. Pending source data does not need to
    # be duplicated in the output; charge each resulting Base64 image in turn.
    entries = [{**item, "data": "", "cache_secret_key": "0" * 64} for item in originals]
    p[field] = entries
    remaining = max_bytes - _json_size(out, max_bytes)
    for original, entry in zip(originals, entries):
        data = _precise_png(original["data"], max_chars=remaining)
        remaining -= len(data)
        entry["data"] = data
        key = original.get("cache_secret_key")
        entry["cache_secret_key"] = (key if isinstance(key, str) and _HEX_KEY.fullmatch(key)
                                      and data == original["data"] else _cache_key(namespace, field, data))
    return out
