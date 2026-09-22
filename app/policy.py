"""核心策略：Opus 免费档判定、Anlas 估算、参数钳制、token 估算。

参考（社区逆向公式 + 官方博客，估算用）：
- https://tapwavezodiac.github.io/novelaiUKB/Image-Generation.html
- https://blog.novelai.net/subscription-updates-usage-limits-...-88a208d5d9c5

== 2026-08 V5 发布后的额度现实 ==

* V4.5 及更老模型：单张纯文生图、<=28 步、像素面积 <=1024x1024
  且无付费附加功能时为 0 Anlas；自定义长宽比不影响免费资格。
* V5 (nai-diffusion-5)：不再参与上述无限免费档！
  - Opus 有独立的「V5 周额度」：约 1800 张 Normal/周，服务端按 ~0.5%/小时恢复
    （约 190 张/天）。额度内的 Normal(<=28步) 生成不扣 Anlas。
  - 额度耗尽或超出条件时按 Anlas 计费，实测默认设置：Small 约 11A、
    Normal(1024px) 约 26A、Large 约 39A —— 约为 V4 公式的 ~1.3 倍。
* Anlas：订阅每月账单日「回满」到档位额度（Opus=10000），不叠加、不按天恢复。

估算公式（V3+ 通用）：
  per_image = ceil((2.951823174884865e-6 * r + 5.753298233447344e-7 * r * steps)
                   * smea_factor)
  1024x1024 / 28 steps -> 20 Anlas（与社区实测一致）；V5 再 x1.3。
"""

from __future__ import annotations

import copy
import base64
import binascii
import math
import re
from typing import Any, Optional, Tuple

V5_COST_MULTIPLIER = 1.3  # V5 实测价格 / V4 公式 ≈ 1.3（11/26/39 vs 8/20/44）

# 仅允许明确认识的图片模型族。不能把未知模型当作旧模型，否则上游新增模型时
# 可能绕过 V5/Anlas 的保护逻辑。
LEGACY_IMAGE_MODEL_PREFIXES = (
    "nai-diffusion-4-5", "nai-diffusion-4", "nai-diffusion-3", "nai-diffusion-2",
    "nai-diffusion-furry-3",
)
LEGACY_IMAGE_MODEL_EXACT = {
    "safe-diffusion", "nai-diffusion", "nai-diffusion-furry",
}

# V5 官方「Normal」预设（用户实测：这三个尺寸走周额度、不扣 Anlas）。
# 判定规则：逐维小于等于任一预设即视为额度内（涵盖 Small 等更小尺寸）；
# 面积达标但非预设的自定义尺寸（如 896x1152）按额度外计费（保守，防止真实 Anlas 被扣）。
V5_NORMAL_PRESETS = ((832, 1216), (1216, 832), (1024, 1024))

# Official public client capabilities, checked 2026-09-21. V3 receives source
# images; V4/V4.5 receive pre-encoded vibes. V5 has neither reference feature.
VIBE_RAW_MODELS = {
    "nai-diffusion-3", "nai-diffusion-3-inpainting",
    "nai-diffusion-furry-3", "nai-diffusion-furry-3-inpainting",
}
VIBE_ENCODED_MODELS = {
    "nai-diffusion-4", "nai-diffusion-4-full", "nai-diffusion-4-full-inpainting",
    "nai-diffusion-4-curated", "nai-diffusion-4-curated-preview",
    "nai-diffusion-4-curated-inpainting", "nai-diffusion-4-5",
    "nai-diffusion-4-5-full", "nai-diffusion-4-5-full-inpainting",
    "nai-diffusion-4-5-curated", "nai-diffusion-4-5-curated-inpainting",
}
PRECISE_REFERENCE_MODELS = {model for model in VIBE_ENCODED_MODELS
                            if model.startswith("nai-diffusion-4-5")}
REFERENCE_LIMIT = 16
VIBE_ENCODING_ANLAS = 2
REFERENCE_FIELDS = (
    "reference_image_multiple", "reference_image_multiple_cached",
    "reference_information_extracted_multiple", "reference_strength_multiple",
    "director_reference_images_cached", "director_reference_descriptions",
    "director_reference_information_extracted", "director_reference_strength_values",
    "director_reference_secondary_strength_values",
)


def _reference_list(p: dict, name: str) -> list:
    value = p.get(name, [])
    if not isinstance(value, list):
        raise ValueError(f"{name} 必须是数组")
    return value


def _base64_data(value: Any, *, source_image: bool = False, png: bool = False) -> bool:
    if not isinstance(value, str) or not value or len(value) > 25 * 1024 * 1024:
        return False
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return False
    if png:
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if source_image:
        return (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")
                or (data.startswith(b"RIFF") and data[8:12] == b"WEBP"))
    return bool(data)


def _unit_value(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 <= value <= 1 and math.isfinite(value))


def validate_vibe_encoding(body: dict) -> Optional[str]:
    if not isinstance(body.get("model"), str) or body["model"] not in VIBE_ENCODED_MODELS:
        return "Vibe 预编码仅支持 V4 / V4.5；V3 使用原图，V5 暂不支持 Vibe"
    if not _base64_data(body.get("image"), source_image=True):
        return "image 必须是无 data URL 前缀的 PNG、JPEG 或 WebP base64"
    if not _unit_value(body.get("informationExtracted")):
        return "informationExtracted 必须是 0 到 1 的有限数值"
    return None


def validate_image_references(payload: dict) -> Optional[str]:
    """Validate complete JSON reference data without trusting shared cache hits.

    The 0..1 range and 16 precise-reference limit are local input limits, not
    claims about every upstream client's unlocked controls.
    """
    p = payload.get("parameters", payload)
    if not isinstance(p, dict):
        return "parameters 必须是 JSON 对象"
    model = str(payload.get("model", "")).strip().lower()
    try:
        groups = {name: _reference_list(p, name) for name in REFERENCE_FIELDS}
    except ValueError as exc:
        return str(exc)
    if any(p.get(name) for name in ("director_reference_images", "characterReferences", "reference_image")):
        return "请使用完整的 reference_image_multiple 或 director_reference_images_cached 参考参数"
    vibes = groups["reference_image_multiple"]
    cached_vibes = groups["reference_image_multiple_cached"]
    precise = groups["director_reference_images_cached"]
    if vibes and cached_vibes:
        return "Vibe 原始数组与缓存数组不能同时提交"
    vibe_count = len(vibes or cached_vibes)
    precise_count = len(precise)
    if vibe_count > REFERENCE_LIMIT or precise_count > REFERENCE_LIMIT:
        return "每次最多使用 16 张参考图"
    if vibe_count and precise_count:
        return "精确参考与 Vibe 不能同时使用"
    if vibe_count and model not in VIBE_RAW_MODELS | VIBE_ENCODED_MODELS:
        return "当前模型不支持 Vibe；请使用 V3、V4 或 V4.5"
    if precise_count and model not in PRECISE_REFERENCE_MODELS:
        return "精确参考仅支持 V4.5"

    for name in ("reference_information_extracted_multiple", "reference_strength_multiple"):
        values = groups[name]
        if len(values) != vibe_count or not all(_unit_value(value) for value in values):
            return f"{name} 须与 Vibe 数量一致，且各值在 0 到 1 之间"
    for name in ("director_reference_information_extracted", "director_reference_strength_values",
                 "director_reference_secondary_strength_values"):
        values = groups[name]
        if len(values) != precise_count or not all(_unit_value(value) for value in values):
            return f"{name} 须与精确参考数量一致，且各值在 0 到 1 之间"
    descriptions = groups["director_reference_descriptions"]
    if len(descriptions) != precise_count:
        return "精确参考描述数量须与参考图片一致"
    for item in descriptions:
        caption = item.get("caption") if isinstance(item, dict) else None
        if (not isinstance(caption, dict)
                or caption.get("base_caption") not in ("character", "style", "character&style")
                or caption.get("char_captions") != [] or item.get("legacy_uc") is not False):
            return "精确参考描述须为 character、style 或 character&style"
    for item in vibes:
        if (not _base64_data(item, source_image=model in VIBE_RAW_MODELS)
                or (model in VIBE_ENCODED_MODELS and _base64_data(item, source_image=True))):
            return "Vibe 须为有效 base64；V3 必须传原图而不是 V4 编码"
    for item in cached_vibes + precise:
        is_precise = any(item is entry for entry in precise)
        if (not isinstance(item, dict)
                or not isinstance(item.get("cache_secret_key"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["cache_secret_key"])
                or not _base64_data(item.get("data"), png=is_precise,
                                    source_image=not is_precise and model in VIBE_RAW_MODELS)
                or (not is_precise and model in VIBE_ENCODED_MODELS
                    and _base64_data(item.get("data"), source_image=True))):
            return "缓存参考必须包含 64 位小写十六进制 cache_secret_key 和完整 base64 data；精确参考须为 PNG"
    return None


def reference_surcharge(payload: dict) -> int:
    """Additional Anlas per output image; encoding is a separate paid request."""
    p = payload.get("parameters", payload)
    precise = len(p.get("director_reference_images_cached") or [])
    vibes = len(p.get("reference_image_multiple") or p.get("reference_image_multiple_cached") or [])
    model = str(payload.get("model", "")).strip().lower()
    return precise * 5 + (max(0, vibes - 4) * 2 if model in VIBE_ENCODED_MODELS else 0)


# ---------------------------------------------------------------- tokens ----

def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中英混合，1 token ≈ 3.5 字符）。仅用于配额预检。"""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 3.5))


def is_v5_model(model: str) -> bool:
    m = (model or "").lower()
    return m in ("nai-diffusion-5", "nai-v5") or m.startswith(
        ("nai-diffusion-5-", "nai-v5-")
    )


def image_model_tier(model: str) -> Optional[str]:
    """返回 legacy / v5；未列入白名单的模型返回 None。"""
    m = (model or "").strip().lower()
    if is_v5_model(m):
        return "v5"
    if (m in LEGACY_IMAGE_MODEL_EXACT or
            any(m == prefix or m.startswith(prefix + "-")
                for prefix in LEGACY_IMAGE_MODEL_PREFIXES)):
        return "legacy"
    return None


# ------------------------------------------------------------ 图片计费 ----

def _per_image_cost(width: int, height: int, steps: int,
                    smea: bool, smea_dyn: bool) -> int:
    """V3+ 通用单张价格（Anlas）。1024x1024/28steps -> 20A。"""
    r = width * height
    smea_factor = 1.4 if (smea and smea_dyn) else (1.2 if smea else 1.0)
    base = 2.951823174884865e-6 * r + 5.753298233447344e-7 * r * steps
    return max(2, math.ceil(base * smea_factor))


def _has_paid_extras(params: dict) -> bool:
    p = params.get("parameters", params)
    if p.get("controlnet_model") or p.get("controlnet_condition"):
        return True
    # V4/V5 角色参考 / 精确参考按张额外收费
    if p.get("characterReferences"):
        return True
    if reference_surcharge(params):
        return True
    return False


def opus_free_eligible(params: dict) -> bool:
    """是否符合单张、纯文生图、低步数、无附加付费功能的基础条件。"""
    p = params.get("parameters", params)
    if int(p.get("n_samples", 1) or 1) != 1:
        return False
    if p.get("image") or p.get("mask"):
        return False  # img2img / inpaint 不免费
    if _has_paid_extras(params):
        return False
    if int(p.get("steps", 28) or 0) > 28:
        return False
    if p.get("sm") or p.get("sm_dyn"):
        return False
    w = int(p.get("width", 0) or 0)
    h = int(p.get("height", 0) or 0)
    if w * h > 1024 * 1024:
        return False
    return True


def legacy_normal_free_eligible(params: dict) -> bool:
    """免费尺寸按像素面积判断，不要求匹配 Normal 预设。"""
    if not opus_free_eligible(params):
        return False
    p = params.get("parameters", params)
    w = int(p.get("width", 0) or 0)
    h = int(p.get("height", 0) or 0)
    return w > 0 and h > 0


def v5_allowance_eligible(params: dict) -> bool:
    """V5 是否走 Opus 周额度（不扣 Anlas）。

    形状条件与老模型相同（单张/纯文生图/<=28步/无SMEA/无付费特性），
    分辨率额外要求：逐维 <= 任一 Normal 预设（832x1216 / 1216x832 / 1024x1024）。
    """
    if not opus_free_eligible(params):
        return False
    p = params.get("parameters", params)
    w = int(p.get("width", 0) or 0)
    h = int(p.get("height", 0) or 0)
    return any(w <= pw and h <= ph for pw, ph in V5_NORMAL_PRESETS)


def snap_v5_preset(width: int, height: int) -> Tuple[int, int]:
    """按长宽比吸附到最近的 V5 Normal 预设。"""
    ar = width / max(1, height)
    if ar > 1.1:
        return 1216, 832
    if ar < 0.9:
        return 832, 1216
    return 1024, 1024


def estimate_image_cost(params: dict, is_opus: bool = True, *,
                        v5_allowance_available: bool = True) -> dict[str, int]:
    """估算一次 /ai/generate-image 的消耗。

    返回 {"anlas": 扣多少 Anlas, "v5": 占多少个 V5 额度单位}。
    二者互斥：V5 符合额度条件时只占额度，不符合时只按 Anlas（x1.3）。
    """
    problem = validate_image_references(params)
    if problem:
        raise ValueError(problem)
    p = params.get("parameters", params)
    w = int(p.get("width", 832) or 832)
    h = int(p.get("height", 1216) or 1216)
    steps = int(p.get("steps", 28) or 28)
    smea = bool(p.get("sm"))
    smea_dyn = bool(p.get("sm_dyn"))
    n = max(1, int(p.get("n_samples", 1) or 1))

    per = _per_image_cost(w, h, steps, smea, smea_dyn)
    if p.get("image"):  # img2img 按强度折算
        strength = float(p.get("strength", 1.0) or 1.0)
        per = max(2, math.ceil(per * max(0.01, strength)))

    # Reference charges are additional to the base generation cost. Preserve
    # the Opus base discount, then charge references even when that base is 0.
    base_params = dict(params)
    base_p = {name: value for name, value in p.items() if name not in REFERENCE_FIELDS}
    if "parameters" in params:
        base_params["parameters"] = base_p
    else:
        base_params = base_p
    shaped = legacy_normal_free_eligible(base_params)
    reference_cost = reference_surcharge(params) * n

    if is_v5_model(str(params.get("model", ""))):
        if v5_allowance_available and v5_allowance_eligible(params):
            return {"anlas": 0, "v5": 1}   # 走 Opus 的 V5 周额度
        return {"anlas": max(1, math.ceil(per * n * V5_COST_MULTIPLIER)), "v5": 0}

    total = per * n
    if is_opus and shaped:
        total -= per  # 老模型：Opus 免费档扣掉首张
    return {"anlas": max(total, 0) + reference_cost, "v5": 0}


def estimate_image_anlas(params: dict, is_opus: bool = True) -> int:
    """兼容旧接口：只返回 Anlas 部分。"""
    return estimate_image_cost(params, is_opus)["anlas"]


# ------------------------------------------------------------ 图片钳制 ----

def fit_size(width: int, height: int, max_pixels: int) -> Tuple[int, int]:
    """等比缩小到像素面积上限内，边长取 64 的倍数。"""
    if width * height <= max_pixels:
        return width, height
    scale = math.sqrt(max_pixels / (width * height))
    w2 = max(64, int(round(width * scale / 64)) * 64)
    h2 = max(64, int(round(height * scale / 64)) * 64)
    while w2 * h2 > max_pixels and (w2 > 64 or h2 > 64):
        if w2 >= h2 and w2 > 64:
            w2 -= 64
        elif h2 > 64:
            h2 -= 64
        else:
            break
    return w2, h2


def clamp_image_params(payload: dict, *, max_pixels: int, max_steps: int,
                       allow_img2img: bool) -> Tuple[dict, list[str], Optional[str]]:
    """把请求改写进「V5 额度条件 / 老模型免费档」的形状。

    返回 (新payload, 变更说明列表, 错误)。
    错误非 None 表示该请求被拒绝（例如 img2img 未开放）。
    对 V5：钳制后可走周额度（不烧 Anlas）；对老模型：钳制后直接免费。
    """
    notes: list[str] = []
    out = copy.deepcopy(payload)
    p = out.get("parameters")
    if not isinstance(p, dict):
        p = {}
        out["parameters"] = p

    # img2img / inpaint 审查
    is_img2img = bool(p.get("image") or p.get("mask"))
    if is_img2img and not allow_img2img:
        return out, notes, "本站未开放 img2img / 局部重绘（该功能会消耗 Anlas）"

    # 批量张数 -> 1
    if int(p.get("n_samples", 1) or 1) != 1:
        p["n_samples"] = 1
        notes.append("n_samples 已强制为 1（免费档/额度条件仅限单张）")

    # steps -> 上限
    if int(p.get("steps", 0) or 0) > max_steps:
        p["steps"] = max_steps
        notes.append(f"steps 已钳制到 {max_steps}")

    # 老模型保留免费面积内的自定义尺寸；V5 继续使用预设边界。
    w = int(p.get("width", 0) or 0)
    h = int(p.get("height", 0) or 0)
    if w > 0 and h > 0:
        if is_v5_model(str(out.get("model", ""))):
            eligible = any(w <= pw and h <= ph for pw, ph in V5_NORMAL_PRESETS)
        else:
            eligible = w * h <= 1024 * 1024
        if not eligible:
            pw, ph = snap_v5_preset(w, h)
            if (w, h) != (pw, ph):
                p["width"], p["height"] = pw, ph
                notes.append(f"分辨率 {w}x{h} 已吸附到 Normal 预设 "
                             f"{pw}x{ph}（避免产生 Anlas）")

    # SMEA -> 关闭（额外计费）
    if p.get("sm") or p.get("sm_dyn"):
        p["sm"] = False
        p["sm_dyn"] = False
        notes.append("SMEA 已关闭（会产生额外 Anlas 消耗）")

    # ControlNet / 角色参考（额外计费）
    if p.get("controlnet_model") or p.get("controlnet_condition"):
        return out, notes, "本站未开放 ControlNet（该功能会消耗 Anlas）"
    if p.get("characterReferences"):
        p.pop("characterReferences", None)
        notes.append("已移除角色参考（精确参考会消耗 Anlas）")

    return out, notes, None


# ------------------------------------------------------------ 文本钳制 ----

def clamp_text_params(payload: dict, *, max_output_tokens: int,
                      max_input_chars: int) -> Tuple[dict, list[str], Optional[str]]:
    notes: list[str] = []
    out = copy.deepcopy(payload)
    p = out.get("parameters")
    if not isinstance(p, dict):
        p = {}
        out["parameters"] = p

    input_text = str(out.get("input", "") or "")
    if len(input_text) > max_input_chars:
        return out, notes, f"输入过长（{len(input_text)} 字符，上限 {max_input_chars}）"

    ml = int(p.get("max_length", 0) or 0)
    if ml <= 0:
        p["max_length"] = min(150, max_output_tokens)
    elif ml > max_output_tokens:
        p["max_length"] = max_output_tokens
        notes.append(f"max_length 已钳制到 {max_output_tokens}")
    min_l = int(p.get("min_length", 1) or 1)
    if min_l > p["max_length"]:
        p["min_length"] = p["max_length"]
    return out, notes, None


def text_model_host(model: str, modern_host: str, legacy_host: str) -> str:
    """Kayra/Erato/GLM/Xialong 等新模型走 text.novelai.net，老模型走 api.novelai.net。"""
    m = (model or "").lower()
    if any(k in m for k in ("kayra", "erato", "glm", "xialong")):
        return modern_host
    return legacy_host


# ------------------------------------------------------------ 工具 ----

def mask_token(token: str, keep: int = 6) -> str:
    if len(token) <= keep:
        return "*" * len(token)
    return token[:keep] + "…" + token[-4:]


def gen_key(prefix: str = "nai") -> str:
    import secrets
    return f"{prefix}-{secrets.token_urlsafe(24)}"
