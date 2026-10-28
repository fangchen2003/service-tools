"""Administrator-editable service configuration, excluding login settings."""

from __future__ import annotations

import os
from typing import Any


# Type and bounds match the corresponding Key API or runtime consumer.
CONFIGURATION_FIELDS = {
    "key_inactivity_delete_days": (int, 0, 3650),
    "allow_img2img": (bool,),
    "safe_clamp": (bool,),
    "max_pixels": (int, 4096, 16777216),
    "max_steps": (int, 1, 28),
    "default_daily_images": (int, 0, 1000000),
    "default_daily_anlas": (float, 0, 100000),
    "default_daily_v5": (int, 0, 100000),
    "default_monthly_anlas": (float, 0, 100000),
    "default_daily_text_tokens": (int, 0, 100000000),
    "default_rpm": (int, 1, 600),
    "default_expires_days": (int, 0, 3650),
    "default_image_model_scope": (str, "legacy", "all"),
    "default_allow_anlas": (bool,),
    "default_allow_img2img": (bool,),
    "default_exclude_global_v5": (bool,),
    "global_concurrency": (int, 1, 32),
    "key_concurrency": (int, 1, 32),
    "max_text_output_tokens": (int, 1, 65536),
    "max_input_chars": (int, 1, 1000000),
}


def initial_sources() -> dict[str, str]:
    sources = {}
    for name in CONFIGURATION_FIELDS:
        env_name = "DEFAULT_KEY_EXPIRES_DAYS" if name == "default_expires_days" else name.upper()
        raw = os.environ.get(env_name)
        kind = CONFIGURATION_FIELDS[name][0]
        configured = raw is not None
        if configured and kind in (int, float):
            try:
                kind(raw.strip())
            except (TypeError, ValueError):
                configured = False
        elif configured and kind is str:
            configured = raw in CONFIGURATION_FIELDS[name][1:]
        sources[name] = "environment" if configured else "default"
    return sources


def validate_configuration(values: Any) -> dict[str, Any]:
    if not isinstance(values, dict) or not values or set(values) - CONFIGURATION_FIELDS.keys():
        raise ValueError("包含未知或空的服务配置")
    result = {}
    for name, value in values.items():
        kind, *bounds = CONFIGURATION_FIELDS[name]
        if kind is bool:
            valid = type(value) is bool
        elif kind is str:
            valid = type(value) is str and value in bounds
        else:
            valid = (type(value) in ((int, float) if kind is float else (int,))
                     and bounds[0] <= value <= bounds[1])
        if not valid:
            if kind in (int, float):
                suffix = "整数" if kind is int else "数值"
                raise ValueError(f"{name} 必须为 {bounds[0]}～{bounds[1]} 的{suffix}")
            raise ValueError(f"{name} 的值无效")
        result[name] = float(value) if kind is float else value
    return result
