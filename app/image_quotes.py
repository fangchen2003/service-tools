"""Strict content-free quotation inputs; execution retains its own image validation."""
import math

from .image_tools import DIRECTOR_TOOLS, UPSCALE_MODEL
from .policy import VIBE_ENCODED_MODELS, validate_reference_counts


def _integer(parameters, name, default=None, *, minimum=0, maximum=None):
    value = parameters.get(name, default)
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"报价参数 {name} 必须是范围内的整数")
    return value


def parse_quote_metadata(query: dict) -> tuple[str, str, dict]:
    if set(query) != {"operation", "model", "parameters"}:
        raise ValueError("报价仅接受 operation、model、parameters 计价元数据")
    operation, model, supplied = query["operation"], query["model"], query["parameters"]
    if not isinstance(operation, str) or operation not in {"generate-image", "upscale", "augment-image", "encode-vibe"}:
        raise ValueError("报价操作无效")
    if not isinstance(model, str) or not model or len(model) > 128 or not isinstance(supplied, dict):
        raise ValueError("报价需要有效的 model 和 parameters")
    if operation == "generate-image":
        allowed = {"mode", "width", "height", "steps", "n_samples", "strength", "inpaint_strength",
                   "sm", "sm_dyn", "upscaled_enhance", "has_controlnet", "vibe_count", "precise_count",
                   "pending_vibe_encoding_count"}
    elif operation == "encode-vibe":
        allowed = set()
    else:
        allowed = {"width", "height"} | ({"req_type"} if operation == "augment-image" else set())
    if set(supplied) - allowed:
        raise ValueError("报价包含不支持的字段；不得发送图片、蒙版、编码或提示词")
    if operation == "encode-vibe":
        if model not in VIBE_ENCODED_MODELS:
            raise ValueError("Vibe 预编码仅支持 V4 / V4.5")
        return operation, model, {}
    p = {name: _integer(supplied, name, minimum=64, maximum=8192) for name in ("width", "height")}
    if operation == "upscale":
        if model != UPSCALE_MODEL:
            raise ValueError("放大仅支持官方当前模型")
    elif operation == "augment-image":
        tool = supplied.get("req_type")
        if not isinstance(tool, str) or tool not in DIRECTOR_TOOLS:
            raise ValueError("不支持的导演工具")
        p["req_type"] = tool
    else:
        mode = supplied.get("mode", "generate")
        if not isinstance(mode, str) or mode not in {"generate", "img2img", "inpaint"}:
            raise ValueError("报价生成模式无效")
        p["mode"] = mode
        p["steps"] = _integer(supplied, "steps", minimum=1)
        p["n_samples"] = _integer(supplied, "n_samples", 1, minimum=1)
        for name in ("strength", "inpaint_strength"):
            value = supplied.get(name, 1)
            if type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value):
                raise ValueError(f"报价参数 {name} 必须是 0 到 1 的有限数值")
            p[name] = value
        for name in ("sm", "sm_dyn", "upscaled_enhance", "has_controlnet"):
            value = supplied.get(name, False)
            if type(value) is not bool:
                raise ValueError(f"报价参数 {name} 必须是布尔值")
            p[name] = value
        for name in ("vibe_count", "precise_count", "pending_vibe_encoding_count"):
            p[name] = _integer(supplied, name, 0, maximum=16)
        problem = validate_reference_counts(model, p["vibe_count"], p["precise_count"])
        if problem:
            raise ValueError(problem)
        pending = p["pending_vibe_encoding_count"]
        if pending > p["vibe_count"] or (pending and model not in VIBE_ENCODED_MODELS):
            raise ValueError("待编码数量须不超过 V4 / V4.5 Vibe 参考数量")
    return operation, model, p
