"""V5 effort policy and fixed-point site quota units (not upstream billing)."""
from decimal import Decimal, ROUND_HALF_UP
import copy

SCALE = 100
DEFAULTS = {
    "v5_auto_medium": True,
    "v5_medium_threshold": 20,
    "v5_medium_multiplier": 0.60,
}
MEDIUM_MODEL = "nai-diffusion-5-full-medium"
MEDIUM_MODELS = {MEDIUM_MODEL, MEDIUM_MODEL + "-inpainting"}
# Official V5 Heavy UC preset, public client checked 2026-10-10.
HEAVY_UC = ("lowres, artistic error, film grain, scan artifacts, worst quality, bad quality, "
            "jpeg artifacts, very displeasing, chromatic aberration, dithering, halftone, "
            "screentone, multiple views, logo, too many watermarks, negative space, blank page")


def units(value):
    return int((Decimal(str(value)) * SCALE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


async def settings(db):
    result = {}
    for name, default in DEFAULTS.items():
        value = await db.get_setting(name, default)
        if name == "v5_auto_medium":
            result[name] = str(value).lower() in {"true", "1"}
        else:
            result[name] = float(value)
    return result


def normalize_medium(payload):
    """Match the official Medium model's fixed settings; never rewrite prompt text."""
    out = copy.deepcopy(payload)
    p = out.setdefault("parameters", {})
    p.update(steps=14, sampler="k_euler_ancestral", noise_schedule="karras",
             negative_prompt=HEAVY_UC)
    if "v4_negative_prompt" in p:
        negative = p["v4_negative_prompt"]
        if not isinstance(negative, dict):
            raise ValueError("v4_negative_prompt 必须是对象")
        caption = negative.setdefault("caption", {})
        if not isinstance(caption, dict) or not isinstance(caption.get("char_captions", []), list):
            raise ValueError("负面角色提示词格式无效")
        caption["base_caption"] = HEAVY_UC
        for character in caption.get("char_captions", []):
            if not isinstance(character, dict):
                raise ValueError("负面角色提示词格式无效")
            character["char_caption"] = ""
    # Browser-only aliases must not reach an upstream that does not support them.
    for name in ("rescale", "cfg_rescale", "uc", "uc_preset", "ucPreset", "ucPresetId",
                 "negativePrompt", "effort", "extra_passthrough_testing",
                 "skip_cfg_above_sigma", "dynamic_thresholding", "sm", "sm_dyn"):
        p.pop(name, None)
    for character in p.get("characterPrompts", []):
        if not isinstance(character, dict):
            raise ValueError("角色提示词格式无效")
        character["uc"] = ""
    out.pop("effort", None)
    return out
