"""Bounded image-tool contracts; official public client ac00ce9, 2026-09-22.

The Gate already assumes Opus accounts. Costs follow the official client, not
client-supplied dimensions or cost fields. Source images are never persisted.
"""
from __future__ import annotations

import base64
import binascii
import io
import math
import zipfile

from PIL import Image, UnidentifiedImageError

MAX_PIXELS = 3_145_728
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
UPSCALE_MODEL = "nai-diffusion-5-curated"
DIRECTOR_TOOLS = {"bg-removal", "lineart", "sketch", "colorize", "emotion",
                  "declutter", "declutter-keep-bubbles"}


def dimensions(raw: bytes, *, output: bool = False) -> tuple[int, int]:
    """Decode, not just sniff headers. Pixel bounds are checked before allocation."""
    try:
        with Image.open(io.BytesIO(raw)) as img:
            width, height = img.size
            limit = MAX_PIXELS * 4 if output else MAX_PIXELS
            if (img.format not in {"PNG", "JPEG", "WEBP"}
                    or getattr(img, "n_frames", 1) != 1
                    or min(width, height) < 1 or max(width, height) > 8192
                    or width * height > limit):
                raise ValueError("图片格式或尺寸超出限制（输入最多 3145728 像素）")
            img.load()
        return width, height
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError,
            SyntaxError, OverflowError) as exc:
        raise ValueError("图片损坏或不是有效的 PNG、JPEG、WebP 静态图片") from exc


def prepare_tool(body: dict, operation: str) -> tuple[dict, int]:
    image = body.get("image")
    if not isinstance(image, str) or not image or len(image) > 24 * 1024 * 1024:
        raise ValueError("image 必须是有效的 Base64 图片")
    try:
        raw = base64.b64decode(image, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("image 必须是有效的 Base64 图片") from exc
    width, height = dimensions(raw)
    if min(width, height) < 64:
        raise ValueError("图片宽高至少为 64 像素")
    for name, actual in (("width", width), ("height", height)):
        if name in body and (type(body[name]) is not int or body[name] != actual):
            raise ValueError("声明的宽高必须与图片实际尺寸一致")
    if operation == "upscale":
        if (body.get("model", UPSCALE_MODEL) != UPSCALE_MODEL
                or type(body.get("scale", 2)) is not int or body.get("scale", 2) != 2
                or type(body.get("declared_blur_sigma", 0)) not in (int, float)
                or body.get("declared_blur_sigma", 0) != 0):
            raise ValueError("放大仅支持官方当前模型、2 倍尺寸和默认模糊参数")
        cost = next(cost for pixels, cost in (
            (1048576, 1), (1747627, 2), (2446678, 3), (MAX_PIXELS, 4)
        ) if width * height <= pixels)
        return {"image": image, "model": UPSCALE_MODEL, "declared_blur_sigma": 0}, cost
    tool = body.get("req_type")
    if not isinstance(tool, str) or tool not in DIRECTOR_TOOLS:
        raise ValueError("不支持的导演工具")
    prompt = body.get("prompt", "")
    if not isinstance(prompt, str) or len(prompt.encode("utf-8")) > 8192:
        raise ValueError("导演工具提示词最多 8192 字节")
    payload = {"image": image, "width": width, "height": height, "req_type": tool}
    if tool in {"colorize", "emotion"}:
        defry = body.get("defry", 0)
        if type(defry) is not int or not 0 <= defry <= 5:
            raise ValueError("defry 必须是 0 到 5 的整数")
        payload.update(prompt=prompt, defry=defry)
    # As on the official page, small sources are expanded to Normal before
    # Director processing. This also gives Remove BG its 65-Anlas minimum.
    if width * height < 1_011_712:
        ratio = math.sqrt(1_048_576 / (width * height))
        width, height = math.floor(width * ratio), math.floor(height * ratio)
        if max(width, height) > 8192:
            raise ValueError("图片长宽比过大")
        with Image.open(io.BytesIO(raw)) as img:
            resized = img.resize((width, height), Image.Resampling.LANCZOS)
            with io.BytesIO() as buffer:
                resized.save(buffer, format="PNG")
                payload["image"] = base64.b64encode(buffer.getvalue()).decode("ascii")
        payload.update(width=width, height=height)
    base = max(2, math.ceil((2.951823174884865e-6 + 5.753298233447344e-7 * 28)
                           * width * height))
    cost = 3 * base + 5 if tool == "bg-removal" else (
        0 if width * height <= 1048576 else base)
    return payload, cost


def validate_result(raw: bytes, operation: str, tool: str, *,
                    expected_images: int | None = None) -> tuple[str, int]:
    """Validate bounded output in memory. Never extract upstream archive paths."""
    if not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("上游图片结果为空或过大")
    expected = expected_images if expected_images is not None else (3 if tool == "bg-removal" else 1)
    if not raw.startswith(b"PK"):
        dimensions(raw, output=True)
        if expected != 1:
            raise ValueError("上游未返回完整数量的图片结果")
        media = "image/png" if raw.startswith(b"\x89PNG") else (
            "image/jpeg" if raw.startswith(b"\xff\xd8") else "image/webp")
        return media, 1
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if (len(entries) != expected or any(e.is_dir() or e.flag_bits & 1 for e in entries)
                    or any('/' in e.filename or '\\' in e.filename or ':' in e.filename
                           or e.filename in {'.', '..'} for e in entries)
                    or sum(e.file_size for e in entries) > MAX_RESPONSE_BYTES):
                raise ValueError("上游压缩包内容或大小无效")
            for entry in entries:
                dimensions(archive.read(entry), output=True)
        return "application/zip", expected
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
        raise ValueError("上游图片压缩包损坏") from exc
