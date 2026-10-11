"""生成记录：提示词与原图（均可单独关闭，自动过期）。画廊小图从原图现场缩小，不另存缩略图。"""
from __future__ import annotations

import io
import json
import warnings
import zipfile
from typing import Optional

from PIL import Image

Image.MAX_IMAGE_PIXELS = 25_000_000      # 超过即报错（默认只是警告），防止解压炸弹
THUMB_SIDE = 768      # 画廊卡片按原图比例整张显示，高清屏上 512 会糊
THUMB_QUALITY = 82
MAX_IMAGE_BYTES = 15 * 1024 * 1024


def make_thumbnail(payload: bytes) -> Optional[bytes]:
    """从上游返回的 zip（或裸图片）里取第一张图，缩成小 JPEG；失败返回 None。"""
    try:
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        data = payload
        if payload[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = [n for n in archive.namelist() if n.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
                if not names:
                    return None
                info = archive.getinfo(names[0])
                if info.file_size > 40 * 1024 * 1024:
                    return None
                data = archive.read(names[0])
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            image.thumbnail((THUMB_SIDE, THUMB_SIDE))
            out = io.BytesIO()
            image.save(out, "JPEG", quality=THUMB_QUALITY, optimize=True)
            return out.getvalue()
    except Exception:
        return None


def prompt_texts(body: dict) -> tuple[str, str]:
    """正向 / 负向提示词（截断，防止超长内容写库）。"""
    params = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}
    positive = str(body.get("input") or "")
    negative = str(params.get("negative_prompt") or "")
    if not positive:
        caption = ((params.get("v4_prompt") or {}).get("caption") or {}) if isinstance(params.get("v4_prompt"), dict) else {}
        positive = str(caption.get("base_caption") or "")
    return positive[:2000], negative[:1000]


def _captions(node) -> tuple[str, list[str]]:
    """从 v4_prompt / v4_negative_prompt 里取 base_caption 和各角色 caption。"""
    cap = (node.get("caption") or {}) if isinstance(node, dict) else {}
    base = str(cap.get("base_caption") or "")
    chars = [str((c or {}).get("char_caption") or "") for c in (cap.get("char_captions") or []) if isinstance(c, dict)]
    return base, [c for c in chars if c]


def capture_prompts(body: dict) -> tuple[str, str, str]:
    """完整提示词：返回 (正面, 负面, extra_json)。
    extra 里是「正面 / 负面」之外的东西——角色提示词(多人图)和生成参数(种子/采样器/步数/CFG/尺寸…)，
    否则光存正负面会漏掉多角色场景和复现所需的参数。"""
    positive, negative = prompt_texts(body)
    p = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}
    pos_base, pos_chars = _captions(p.get("v4_prompt"))
    neg_base, neg_chars = _captions(p.get("v4_negative_prompt"))
    if not positive:
        positive = pos_base
    if not negative:                 # 只在 v4_negative_prompt 里写负面词的客户端，以前会漏记
        negative = neg_base
    extra = {
        "char_prompts": pos_chars[:12],
        "char_negatives": neg_chars[:12],
        "params": {k: p.get(k) for k in ("seed", "sampler", "steps", "scale", "width", "height",
                                         "noise_schedule", "cfg_rescale", "sm", "sm_dyn",
                                         "ucPreset", "qualityToggle", "n_samples") if p.get(k) is not None},
    }
    has = extra["char_prompts"] or extra["char_negatives"] or extra["params"]
    return positive[:4000], negative[:2000], (json.dumps(extra, ensure_ascii=False)[:4000] if has else "")


def full_image(payload: bytes) -> tuple[Optional[bytes], str]:
    """取上游返回的第一张图的原始字节（不缩放、不重压），超过大小上限则不存。返回 (bytes, content_type)。"""
    try:
        data, ctype = payload, "image/png"
        if payload[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = [n for n in archive.namelist() if n.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
                if not names:
                    return None, ""
                info = archive.getinfo(names[0])
                if info.file_size > MAX_IMAGE_BYTES:
                    return None, ""
                data = archive.read(names[0])
        if len(data) > MAX_IMAGE_BYTES:
            return None, ""
        n = data[:12]
        ctype = ("image/png" if n[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg" if n[:3] == b"\xff\xd8\xff"
                 else "image/webp" if n[:4] == b"RIFF" and n[8:12] == b"WEBP" else "image/png")
        return data, ctype
    except Exception:
        return None, ""


IMAGE_RETENTION_KEY = "audit_image_retention_days"   # 已停用：原图不再按天数删（2026-10-11 站长）
IMAGE_CAP_KEY = "audit_image_cap_mb"                  # 原图占用上限（MB）；见 site_flags.IMAGE_CAP_MB
MEMBER_DOWNLOAD_DAYS = 2      # 成员只能打包下载最近 2 天的原图：只用来找回客户端里误删的图


def audit_notice(prompts: bool, thumbs: bool, days: int, image_days: int = 0) -> str:
    """向成员披露记录范围（提示词、原图各自保留多久）；未开启记录则返回空串。
    「记录生成结果」开关只保存原图（不再另存缩略图），保留天数单独设置；0 天 = 不存图。"""
    keep = (lambda d: f"{d} 天后自动删除" if d > 0 else "长期保存")
    parts = []
    if prompts:
        parts.append(f"图片提示词（{keep(days)}）")
    if thumbs:
        parts.append(f"生成的原图（最近 {MEMBER_DOWNLOAD_DAYS} 天的可在首页打包下载，用来找回误删的图；"
                     "服务器空间满了会自动删掉最旧的）")
    if not parts:
        return ""
    return "为防止滥用和优化调度，本站会保留你的" + "、".join(parts) + "，仅站长可见。"


async def audit_image_days(db) -> int:
    """成员能打包下载最近几天的原图（固定 2 天）。原图本身不再按天数删，只按容量上限清理。"""
    return MEMBER_DOWNLOAD_DAYS


async def audit_image_cap_mb(db) -> int:
    from . import site_flags
    return await site_flags.get(db, site_flags.IMAGE_CAP_MB)


async def audit_disclosure(db, settings) -> str:
    """所有给成员看的「本站记录了什么」都走这一个函数，避免各处文案再各写一套。"""
    prompts, thumbs, days = await audit_flags(db, settings)
    return audit_notice(prompts, thumbs, days, MEMBER_DOWNLOAD_DAYS)


async def audit_flags(db, settings) -> tuple[bool, bool, int]:
    """记录开关（提示词、生成结果/原图、提示词保留天数）：后台保存的设置优先，其次是环境变量。
    第二项沿用旧设置名 audit_thumbs，含义已变为「保存原图」。"""
    async def read(name, default):
        getter = getattr(db, "get_setting", None)
        value = await getter(name, None) if getter else None
        return default if value is None else value

    def truth(value) -> bool:
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    prompts = truth(await read("audit_prompts", getattr(settings, "audit_prompts", False)))
    thumbs = truth(await read("audit_thumbs", getattr(settings, "audit_thumbs", False)))
    try:
        days = int(float(await read("audit_retention_days", getattr(settings, "audit_retention_days", 7))))
    except (TypeError, ValueError):
        days = 7
    return prompts, thumbs, max(0, min(days, 3650))        # 0 = 长期保留
