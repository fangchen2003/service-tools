"""核心策略：Opus 免费档判定、Anlas 估算、参数钳制、token 估算。

计价依据：2026-09-24 官网客户端 787d312 与官方余额差额实测。
"""

from __future__ import annotations

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
        cache_key = item.get("cache_secret_key") if isinstance(item, dict) else None
        # BaiBai uses UUIDv4 for Vibe cache identifiers. Gate does not look up
        # these keys locally; complete reference data is still required below.
        valid_key = isinstance(cache_key, str) and (
            re.fullmatch(r"[0-9a-f]{64}", cache_key)
            or (not is_precise and re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", cache_key))
        )
        if (not isinstance(item, dict)
                or not valid_key
                or not _base64_data(item.get("data"), png=is_precise,
                                    source_image=not is_precise and model in VIBE_RAW_MODELS)
                or (not is_precise and model in VIBE_ENCODED_MODELS
                    and _base64_data(item.get("data"), source_image=True))):
            return "缓存参考必须包含 64 位小写十六进制 cache_secret_key（Vibe 也支持小写 UUIDv4）和完整 base64 data；精确参考须为 PNG"
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


INTEGER_IMAGE_PARAMS = ("width", "height", "steps", "n_samples")


def normalize_image_request(body: dict) -> None:
    """路由入口统一规范化：模型名小写去空格并回写；尺寸/步数/张数必须是整数。

    计价按 int() 截断而上游原样接收：放过 28.99 步或 1.99 张会让“免费”估价与实际扣费不一致。
    """
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("缺少 model")
    body["model"] = model.strip().lower()
    p = body.get("parameters")
    if not isinstance(p, dict):
        return
    for name in INTEGER_IMAGE_PARAMS:
        value = p.get(name)
        if value is None:
            continue
        if isinstance(value, float) and value.is_integer():
            value = p[name] = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65536:
            raise ValueError(f"{name} 必须是非负整数")

    # seed 规范化：客户端经常传 -1 表示随机种子；上游只接受 0..4294967295 的 uint32
    if "seed" in p:
        seed = p.get("seed")
        if isinstance(seed, float) and seed.is_integer():
            seed = p["seed"] = int(seed)
        if isinstance(seed, int):
            if seed < 0 or seed > 4294967295:
                import random
                p["seed"] = random.randint(0, 4294967295)
        elif seed is not None:
            import random
            p["seed"] = random.randint(0, 4294967295)

    # scale 校验
    scale = p.get("scale")
    if scale is not None:
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale < 0 or scale > 50:
            raise ValueError("scale 必须是 0 到 50 之间的有效数值")


# 以下上游限制来自实测（参考 Steven52065/novelai_proxy 2026-09 的记录，均为上游原始报错）：
# - 提示词上限 51200，上游按 UTF-8 字节计（中文约 1.7 万字）；
# - 文生图宽高必须是 64 的整数倍（如 786x786 会被拒）；
# - V4/V4.5/V5 用下列采样器上游直接 500。只拦这几个已证实的，不做白名单，避免误伤以后新增的采样器。
MAX_PROMPT_BYTES = 51200
DIMENSION_STEP = 64
V4_V5_REJECTED_SAMPLERS = frozenset({
    "plms", "ddim", "ddim_v3", "k_dpm_adaptive", "k_dpm_fast", "k_dpmpp_3m_sde", "nai_smea", "nai_smea_dyn",
})


def _prompt_texts(body: dict):
    yield "input", body.get("input")
    p = body.get("parameters")
    if not isinstance(p, dict):
        return
    for key in ("prompt", "negative_prompt"):
        yield f"parameters.{key}", p.get(key)
    chars_raw = p.get("characterPrompts")
    if isinstance(chars_raw, list):
        for i, item in enumerate(chars_raw):
            if isinstance(item, dict):
                yield f"parameters.characterPrompts[{i}].prompt", item.get("prompt")
                yield f"parameters.characterPrompts[{i}].uc", item.get("uc")
    for key in ("v4_prompt", "v4_negative_prompt"):
        caption = (p.get(key) or {}).get("caption") if isinstance(p.get(key), dict) else None
        if not isinstance(caption, dict):
            continue
        yield f"{key}.base_caption", caption.get("base_caption")
        chars = caption.get("char_captions")
        if isinstance(chars, list):
            for i, item in enumerate(chars):
                if isinstance(item, dict):
                    yield f"{key}.char_captions[{i}]", item.get("char_caption")


_NAME_DIGITS_WEIGHT = re.compile(r"(?<=[A-Za-z_])(\d+(?:\.\d+)?)::")


def fix_name_digit_weights(body: dict) -> int:
    """名字结尾的数字紧挨着 ::（如画师 bm94199::）时，在 :: 前补一个空格，原地改写，返回改了几处。

    本意是用 :: 结束一段加权，但上游会把 94199 当成权重，结果 NaN（10/11 线上 10 次失败全是这一种）。
    只改「数字前面紧挨着字母或下划线」的情况，独立的 1.2:: / -3:: 这类正常权重不动。
    10/11 有成员连续 9 次被预检拒绝、客户端看不到提示，所以改成自动纠正，不再拒绝。"""
    n = 0

    def fix(text):
        nonlocal n
        if not isinstance(text, str) or "::" not in text:
            return text
        new, k = _NAME_DIGITS_WEIGHT.subn(r"\1 ::", text)
        n += k
        return new

    if isinstance(body.get("input"), str):
        body["input"] = fix(body["input"])
    p = body.get("parameters")
    if not isinstance(p, dict):
        return n
    for key in ("prompt", "negative_prompt"):
        if isinstance(p.get(key), str):
            p[key] = fix(p[key])
    if isinstance(p.get("characterPrompts"), list):
        for item in p["characterPrompts"]:
            if isinstance(item, dict):
                for key in ("prompt", "uc"):
                    if isinstance(item.get(key), str):
                        item[key] = fix(item[key])
    for key in ("v4_prompt", "v4_negative_prompt"):
        caption = (p.get(key) or {}).get("caption") if isinstance(p.get(key), dict) else None
        if not isinstance(caption, dict):
            continue
        if isinstance(caption.get("base_caption"), str):
            caption["base_caption"] = fix(caption["base_caption"])
        if isinstance(caption.get("char_captions"), list):
            for item in caption["char_captions"]:
                if isinstance(item, dict) and isinstance(item.get("char_caption"), str):
                    item["char_caption"] = fix(item["char_caption"])
    return n


MAX_CHARACTERS = 6        # NovelAI V4 / V4.5 角色提示词上限


def upstream_parameter_problem(body: dict) -> Optional[str]:
    """在排队之前拦下上游一定会拒绝的请求，避免白占全站唯一的出图队列。"""
    for path, text in _prompt_texts(body):
        if isinstance(text, str):
            size = len(text.encode("utf-8"))
            if size >= MAX_PROMPT_BYTES:
                return f"提示词过长：{path} 有 {size} 字节（中文每字 3 字节），上游上限 {MAX_PROMPT_BYTES} 字节"
            # 不按括号层数拦截：NAI 花括号每层只 ×1.05（8 层≈1.48 倍），圆括号不是权重语法，不会溢出；
            # 10/11 这条规则误拦了一位成员的 V5 请求，已移除。NaN 的真实原因是下面的超大数值权重。
            # 异常数值权重（如画师名带数字紧挨着 :: 导致超大权重与 NaN 计算溢出）
            for m in re.finditer(r"(?<![\d.])([-+]?\d+(?:\.\d+)?)::", text):   # 回看断言：长串数字不再 O(n²)（10/11 审查 P0：5 万位数字卡 23 秒）
                try:
                    val = float(m.group(1))
                except ValueError:
                    continue
                if abs(val) > 100:
                    return f"检测到异常权重 {m.group(1)}::（可能是画师名里的数字紧挨着 ::），请在 :: 前加逗号或空格"
    p = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}
    if body.get("action", "generate") == "generate":
        for key in ("width", "height"):
            value = p.get(key)
            if isinstance(value, int) and value > 0 and value % DIMENSION_STEP:
                lower = max(DIMENSION_STEP, value // DIMENSION_STEP * DIMENSION_STEP)
                return f"{key}={value} 不是 64 的倍数，上游会拒绝；可改为 {lower} 或 {lower + DIMENSION_STEP}"
    model = str(body.get("model", ""))
    if model.startswith("nai-diffusion-4"):
        # V4 / V4.5 最多 6 个角色；超了上游不报清楚原因，直接回 500（2026-10-10 随风飞扬 7 个角色连续 500）
        counts = [len(p.get("characterPrompts")) if isinstance(p.get("characterPrompts"), list) else 0]
        for key in ("v4_prompt", "v4_negative_prompt"):
            caption = (p.get(key) or {}).get("caption") if isinstance(p.get(key), dict) else None
            if isinstance(caption, dict) and isinstance(caption.get("char_captions"), list):
                counts.append(len(caption["char_captions"]))
        if max(counts) > MAX_CHARACTERS:
            return f"角色太多：这次有 {max(counts)} 个角色，V4/V4.5 最多 {MAX_CHARACTERS} 个，请删掉几个角色再生成"
    sampler = p.get("sampler")
    if sampler is not None:
        if not isinstance(sampler, str) or not sampler.isascii() or len(sampler) > 64 or not sampler.replace("_", "").isalnum():
            return "采样器名称无效，请选择官方支持的标准采样器"
        if (sampler in V4_V5_REJECTED_SAMPLERS
                and (is_v5_model(model) or model.startswith("nai-diffusion-4"))):
            return f"采样器 {sampler} 不支持 V4/V4.5/V5 模型，请换用 k_euler_ancestral、k_dpmpp_2m 等"
    schedule = p.get("noise_schedule")
    if schedule is not None and (not isinstance(schedule, str) or not schedule.isascii() or len(schedule) > 32):
        return "noise_schedule 参数无效"
    return None


def is_v5_model(model: str) -> bool:
    m = (model or "").strip().lower()
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
        outer = p.get("strength", 0.0)
        if not _unit_value(outer):
            raise ValueError("strength 必须是 0 到 1 的有限数值")
        strength = max(strength, outer)   # 上游可能按任一字段计价，取较大者
        # V4 按强度折算；2026-09-24 实测 V5 Full 重绘使用完整基础价。
        if str(params.get("model", "")).lower().startswith("nai-diffusion-4"):
            per *= strength
    elif p.get("image"):
        strength = p.get("strength", 1.0)
        if not _unit_value(strength):
            raise ValueError("strength 必须是 0 到 1 的有限数值")
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


ECONOMY_STEPS = 14                       # 节约模式固定步数（镜像 NovelAI「节约模式」）
ECONOMY_SAMPLER = "k_euler_ancestral"    # 低步数下收敛快、质量稳，官方节约模式也锁这个


def clamp_image_params(payload: dict, *, max_pixels: int, max_steps: int,
                       allow_img2img: bool, economy: bool = False) -> Tuple[dict, list[str], Optional[str]]:
    """把请求改写进「V5 额度条件 / 老模型免费档」的形状。

    返回 (新payload, 变更说明列表, 错误)。
    错误非 None 表示该请求被拒绝（例如 img2img 未开放）。
    对 V5：钳制后可走周额度（不烧 Anlas）；对老模型：钳制后直接免费。
    economy=True（节约模式）：步数封顶 14、采样器锁 k_euler_ancestral，Anlas 约省 40%（我们计价按步数线性，
    V5 周额度按 (A+B·14)/(A+B·23) 自动少扣）；保留用户的负面词，只动步数和采样器。
    """
    notes: list[str] = []
    # 只改写 parameters 的顶层字段：浅拷贝即可，避免对大请求体做 deepcopy（内存/CPU 放大数倍）。
    out = dict(payload)
    p = dict(out["parameters"]) if isinstance(out.get("parameters"), dict) else {}
    out["parameters"] = p

    # img2img / inpaint 审查
    is_img2img = bool(p.get("image") or p.get("mask"))
    if is_img2img and not allow_img2img:
        return out, notes, "本站不支持图生图（img2img）/ 局部重绘（会消耗 Anlas）。请在客户端里移除参考图（原图）后再生成"

    if max_pixels < 64 * 64:
        return out, notes, "MAX_PIXELS 不能小于 4096（最小尺寸 64x64）"

    # 批量张数 -> 1
    if int(p.get("n_samples", 1) or 1) != 1:
        p["n_samples"] = 1
        notes.append("n_samples 已限制为 1，避免额外图片产生 Anlas")

    # steps -> 上限（节约模式再压到 14）
    eff_max_steps = min(max_steps, ECONOMY_STEPS) if economy else max_steps
    if int(p.get("steps", 0) or 0) > eff_max_steps:
        p["steps"] = eff_max_steps
        notes.append(f"steps 已钳制到 {eff_max_steps}" + ("（节约模式）" if economy else ""))

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

    # 节约模式：锁采样器（低步数下更稳），不动负面词
    if economy and p.get("sampler") and p.get("sampler") != ECONOMY_SAMPLER:
        p["sampler"] = ECONOMY_SAMPLER
        notes.append(f"节约模式：采样器已设为 {ECONOMY_SAMPLER}")

    # ControlNet / 角色参考（额外计费）
    if p.get("controlnet_model") or p.get("controlnet_condition"):
        return out, notes, "本站未开放 ControlNet（该功能会消耗 Anlas）"
    if p.get("characterReferences"):
        p.pop("characterReferences", None)
        notes.append("已移除角色参考（精确参考会消耗 Anlas）")

    return out, notes, None


def economy_trim(payload: dict) -> Tuple[dict, list[str]]:
    """节约模式下给「算法自动分到 Anlas」的 Key 用：只压步数和采样器，不动尺寸（尺寸超出免费档时照常扣 Anlas）。

    这些 Key 不走免费档钳制（要能用 Anlas 出超规格图），以前节约模式也一起跳过了，人多时它们仍按 28 步出图。
    """
    notes: list[str] = []
    out = dict(payload)
    p = dict(out["parameters"]) if isinstance(out.get("parameters"), dict) else {}
    out["parameters"] = p
    if int(p.get("steps", 0) or 0) > ECONOMY_STEPS:
        p["steps"] = ECONOMY_STEPS
        notes.append(f"steps 已钳制到 {ECONOMY_STEPS}（节约模式）")
    if p.get("sampler") and p.get("sampler") != ECONOMY_SAMPLER:
        p["sampler"] = ECONOMY_SAMPLER
        notes.append(f"节约模式：采样器已设为 {ECONOMY_SAMPLER}")
    return out, notes


# ------------------------------------------------------------ V4 结构化提示词 ----
def fill_v4_prompt(payload: dict) -> Tuple[dict, list[str]]:
    """V4 / V4.5 必须带 v4_prompt / v4_negative_prompt 结构，缺了上游直接回 500、不说原因
    （2026-10-10 成员「心事全在脸上」的 OpenAI 兼容类客户端只发 input + negative_prompt，连续 11 次 500；
    同参数补上结构后 200，已用测试 Key 复现）。官方网页端用 input / negative_prompt / characterPrompts 拼出这两个结构，这里照做。"""
    model = str(payload.get("model", ""))
    p = payload.get("parameters")
    if not model.startswith("nai-diffusion-4") or not isinstance(p, dict):
        return payload, []
    notes: list[str] = []
    out = dict(payload)
    p = dict(p)
    out["parameters"] = p
    chars = [c for c in (p.get("characterPrompts") or []) if isinstance(c, dict)] if isinstance(p.get("characterPrompts"), list) else []
    def centers(c):
        cen = c.get("center") if isinstance(c.get("center"), dict) else {"x": 0.5, "y": 0.5}
        return [{"x": cen.get("x", 0.5), "y": cen.get("y", 0.5)}]
    if not isinstance(p.get("v4_prompt"), dict):
        p["v4_prompt"] = {"caption": {"base_caption": str(out.get("input", "") or ""),
                                      "char_captions": [{"char_caption": str(c.get("prompt") or ""), "centers": centers(c)} for c in chars]},
                          "use_coords": bool(p.get("use_coords", False)), "use_order": True}
        notes.append("已按官方格式补上 v4_prompt")
    if not isinstance(p.get("v4_negative_prompt"), dict):
        p["v4_negative_prompt"] = {"caption": {"base_caption": str(p.get("negative_prompt", "") or ""),
                                               "char_captions": [{"char_caption": str(c.get("uc") or ""), "centers": centers(c)} for c in chars]},
                                   "legacy_uc": False}
        notes.append("已按官方格式补上 v4_negative_prompt")
    return out, notes


# ------------------------------------------------------------ V5 Medium ----
# 官方 2026-10-08 给 V5 Full 加了 Medium 档（蒸馏模型，锁 14 步 / Euler-a / Heavy 负面预设，不支持自定义负面词和
# rescale；官方公告：比 23 步 High 省约 42%，盲测画质大致相当）。模型名与固定设置取自官方前端代码（2026-10-10 核对）。
V5_MEDIUM_OF = {"nai-diffusion-5-full": "nai-diffusion-5-full-medium",
                "nai-diffusion-5-full-inpainting": "nai-diffusion-5-full-medium-inpainting"}
V5_MEDIUM_STEPS = 14
V5_MEDIUM_SAMPLER = "k_euler_ancestral"
V5_MEDIUM_UC_PRESET = 0                 # V5 的预设列表里 heavy 排第 0
V5_MEDIUM_UC = ("lowres, artistic error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, "
                "very displeasing, chromatic aberration, dithering, halftone, screentone, multiple views, logo, "
                "too many watermarks, negative space, blank page")
# Medium 和 High 一样每张记 1 张 V5（10/10 站长：不再打折——折算会让 V5 用量 / 恢复速度的统计口径失真）。


def is_v5_medium(model: str) -> bool:
    return (model or "").strip().lower() in V5_MEDIUM_OF.values()


def medium_normalize(payload: dict) -> Tuple[dict, list[str]]:
    """按官方前端对 Medium 的做法整理参数：步数 / 采样器 / 负面预设固定，自定义负面词清空，rescale 归零。
    不整理的话，客户端照 High 的习惯带自定义负面词或 rescale，上游可能拒绝。"""
    out = dict(payload)
    p = dict(out["parameters"]) if isinstance(out.get("parameters"), dict) else {}
    out["parameters"] = p
    notes: list[str] = []
    if p.get("steps") != V5_MEDIUM_STEPS:
        p["steps"] = V5_MEDIUM_STEPS
    if p.get("sampler") != V5_MEDIUM_SAMPLER:
        p["sampler"] = V5_MEDIUM_SAMPLER
    if (p.get("cfg_rescale") or 0) != 0:
        p["cfg_rescale"] = 0
        notes.append("Medium 不支持 rescale，已设为 0")
    user_uc = str(p.get("negative_prompt") or "").strip()
    if user_uc and user_uc != V5_MEDIUM_UC:
        notes.append("Medium 不支持自定义负面词，已换成官方 Heavy 预设（可在正面提示词里用 -2::xx:: 排除）")
    p["negative_prompt"] = V5_MEDIUM_UC
    p["ucPreset"] = V5_MEDIUM_UC_PRESET
    neg = p.get("v4_negative_prompt")
    if isinstance(neg, dict):
        neg = dict(neg)
        cap = dict(neg["caption"]) if isinstance(neg.get("caption"), dict) else {}
        cap["base_caption"] = V5_MEDIUM_UC
        if isinstance(cap.get("char_captions"), list):
            cap["char_captions"] = [{**c, "char_caption": ""} if isinstance(c, dict) else c
                                    for c in cap["char_captions"]]
        neg["caption"] = cap
        p["v4_negative_prompt"] = neg
    if isinstance(p.get("characterPrompts"), list):
        p["characterPrompts"] = [{**c, "uc": ""} if isinstance(c, dict) else c for c in p["characterPrompts"]]
    return out, notes


def to_medium(payload: dict) -> Tuple[dict, list[str]]:
    """V5 Full（High）→ Medium：节约模式用。省的用量和「High 压到 14 步」差不多，但 Medium 是专为 14 步训练的，画质好得多。"""
    model = str(payload.get("model", "")).strip().lower()
    if model not in V5_MEDIUM_OF:
        return payload, []
    out, notes = medium_normalize({**payload, "model": V5_MEDIUM_OF[model]})
    return out, ["节约模式：V5 Full 已切到官方 Medium 档（14 步，画质接近 High）"] + notes


# ------------------------------------------------------------ 文本钳制 ----

def clamp_text_params(payload: dict, *, max_output_tokens: int,
                      max_input_chars: int) -> Tuple[dict, list[str], Optional[str]]:
    notes: list[str] = []
    out = dict(payload)
    p = dict(out["parameters"]) if isinstance(out.get("parameters"), dict) else {}
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
