"""核心策略：Opus 免费档判定、Anlas 估算、参数钳制、token 估算。

计价依据：2026-09-24 官网客户端 787d312 与官方余额差额实测。
"""

from __future__ import annotations

import copy
import base64
import binascii
import math
import re
from typing import Any, Optional, Tuple

V5_COST_MULTIPLIER = 1.5

# 仅允许明确认识的图片模型族。不能把未知模型当作旧模型，否则上游新增模型时
# 可能绕过 V5/Anlas 的保护逻辑。
LEGACY_IMAGE_MODEL_PREFIXES = (
    "nai-diffusion-4-5", "nai-diffusion-4", "nai-diffusion-3", "nai-diffusion-2",
    "nai-diffusion-furry-3",
)
LEGACY_IMAGE_MODEL_EXACT = {
    "safe-diffusion", "nai-diffusion", "nai-diffusion-furry",
}

# 免费 Key 按预设钳制尺寸，计费按像素面积判断。
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
        # V4/V4.5 encodings already contain the extraction amount. V3 raw images
        # still require it; an explicitly supplied array must always be valid.
        if name == "reference_information_extracted_multiple" and model in VIBE_ENCODED_MODELS and name not in p:
            continue
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
    """单张参考附加费，批次减免在总价中处理；编码另行收费。"""
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
                    smea: bool, smea_dyn: bool) -> float:
    """V3+ 通用单张价格（Anlas）。1024x1024/28steps -> 20A。"""
    r = width * height
    smea_factor = 1.4 if (smea and smea_dyn) else (1.2 if smea else 1.0)
    base = 2.951823174884865e-6 * r + 5.753298233447344e-7 * r * steps
    # 模型倍率、SMEA 和强度之后才做最终取整，避免多次取整累积误差。
    return math.ceil(base) * smea_factor


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
    """首张是否符合免费条件；批次内其余图片仍按张计费。"""
    p = params.get("parameters", params)
    if _has_paid_extras(params):
        return False
    if int(p.get("steps", 28) or 0) > 28:
        return False
    w = int(p.get("width", 0) or 0)
    h = int(p.get("height", 0) or 0)
    if w <= 0 or h <= 0 or w * h > 1024 * 1024:
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
    """V5 首张能否使用 Opus 额度；是否耗尽由上游额度检查决定。"""
    return opus_free_eligible(params)


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
    V5 多图批次可同时占首张额度并支付其余图片的 Anlas。
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
    is_v5 = is_v5_model(str(params.get("model", "")))
    if is_v5:
        per *= V5_COST_MULTIPLIER
    if p.get("mask"):
        # 局部重绘使用 img2img.strength。
        inpaint = p.get("img2img") or {}
        if not isinstance(inpaint, dict):
            raise ValueError("img2img 必须是 JSON 对象")
        strength = inpaint.get("strength", 1.0)
        if not _unit_value(strength):
            raise ValueError("img2img.strength 必须是 0 到 1 的有限数值")
        # V4 按强度折算；2026-09-24 实测 V5 Full 重绘使用完整基础价。
        if str(params.get("model", "")).lower().startswith("nai-diffusion-4"):
            per *= strength
    elif p.get("image"):
        strength = float(p.get("strength", 1.0))
        per *= strength
    per = max(2, math.ceil(per))

    # 先排除参考附加费，判断基础生成是否满足 Opus 首张减免条件。
    base_params = dict(params)
    base_p = {name: value for name, value in p.items() if name not in REFERENCE_FIELDS}
    if "parameters" in params:
        base_params["parameters"] = base_p
    else:
        base_params = base_p
    shaped = legacy_normal_free_eligible(base_params)

    if is_v5:
        free_first = bool(is_opus and v5_allowance_available and v5_allowance_eligible(params))
        return {"anlas": per * (n - int(free_first)), "v5": int(free_first)}

    paid_outputs = n - int(is_opus and shaped)
    # 官方余额实测：符合条件的多图首张参考费减免，单张照收。
    reference_cost = reference_surcharge(params) * max(1, paid_outputs)
    return {"anlas": per * paid_outputs + reference_cost, "v5": 0}


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
        return out, notes, "本站未开放 img2img / 局部重绘"

    if max_pixels < 64 * 64:
        return out, notes, "MAX_PIXELS 不能小于 4096（最小尺寸 64x64）"

    # 批量张数 -> 1
    if int(p.get("n_samples", 1) or 1) != 1:
        p["n_samples"] = 1
        notes.append("n_samples 已限制为 1，避免额外图片产生 Anlas")

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
        pw, ph = (w, h) if eligible else snap_v5_preset(w, h)
        # 先保留原有预设限制，再缩到站点面积上限内，避免预设调整反而超限。
        pw, ph = fit_size(pw, ph, max_pixels)
        if (w, h) != (pw, ph):
            p["width"], p["height"] = pw, ph
            notes.append(f"分辨率 {w}x{h} 已按安全钳制调整为 {pw}x{ph}")

    # 沿用免费 Key 的保守钳制；SMEA 本身不排除首张减免资格。
    if p.get("sm") or p.get("sm_dyn"):
        p["sm"] = False
        p["sm_dyn"] = False
        notes.append("SMEA 已按安全钳制规则关闭")

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
