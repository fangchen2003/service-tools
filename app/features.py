"""功能开关：站长可在后台全局开关每项上游功能，并按 Key 授权。

全局开关存放在 site_settings(features_global)；每把 Key 的授权存放在 api_keys.features：
NULL 表示沿用旧行为（全局开启的功能都可用）；逗号分隔列表表示只允许其中列出的功能。
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterable, Optional

from . import site_flags

FEATURES: dict[str, str] = {
    "image": "文生图",
    "upscale": "放大",
    "augment": "导演工具（线稿、上色等）",
    "vibe": "Vibe 编码",
    "tags": "标签补全",
    "text": "文本生成 / OpenAI 兼容聊天",
    "voice": "语音合成",
}
GLOBAL_KEY = "features_global"
# 需要消耗 Anlas 的功能：本站只提供免费出图，拒绝时说明原因，免得成员以为是 Key 坏了。
ANLAS_FEATURES = {"vibe": "每次编码参考图约 2 Anlas", "upscale": "按图片大小扣 Anlas", "augment": "按图片大小扣 Anlas"}

# 用量日志 usage_log.kind → 功能。后台按功能统计与筛选日志时使用。
KIND_FEATURE: dict[str, str] = {
    "image": "image", "image_stream": "image",
    "upscale": "upscale", "augment-image": "augment",
    "vibe_encode": "vibe", "tags": "tags",
    "text": "text", "chat": "text", "voice": "voice",
}


def kinds_for(feature: str) -> list[str]:
    return [kind for kind, name in KIND_FEATURE.items() if name == feature]


def parse_list(value: Any) -> Optional[list[str]]:
    """把 Key 上的 features 字段解析为列表；None 表示沿用旧行为。"""
    if value is None:
        return None
    return [name for name in str(value).split(",") if name in FEATURES]


def normalize(names: Iterable[str]) -> list[str]:
    return [name for name in FEATURES if name in set(names)]


def dump(names: Optional[Iterable[str]]) -> Optional[str]:
    return None if names is None else ",".join(normalize(names))


async def global_flags(db) -> dict[str, bool]:
    """全局开关；缺省为全部开启。"""
    flags = {name: True for name in FEATURES}
    getter = getattr(db, "get_setting", None)
    if getter is None:
        return flags
    try:
        raw = await getter(GLOBAL_KEY, None)
        saved = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        saved = {}
    for name in FEATURES:
        if isinstance(saved.get(name), bool):
            flags[name] = saved[name]
    return flags


def key_features(key) -> Optional[list[str]]:
    try:
        return parse_list(key["features"])
    except (KeyError, IndexError):
        return None


async def check(db, key, name: str, settings=None) -> Optional[str]:
    """允许则返回 None，否则返回面向用户的拒绝原因。"""
    try:
        if key["is_admin"]:
            return None
    except (KeyError, IndexError):
        pass
    if not (await global_flags(db)).get(name, True):
        return f"站长暂未开放此功能：{FEATURES[name]}"
    allowed = key_features(key)
    try:
        has_anlas = bool(key["allow_anlas"])
    except (KeyError, IndexError, TypeError):
        has_anlas = False
    if name in ANLAS_FEATURES and has_anlas:
        return None          # 2026-10-10 放开 Anlas：有 Anlas 权限（含自动分配）的成员可以用付费功能，费用按每日 Anlas 上限扣
    if name == "vibe":
        until = await site_flags.get(db, site_flags.GLOBAL_DAILY_ANLAS_UNTIL, settings)
        if until > 0 and time.time() >= until:
            vibe_daily_cap = 0
        else:
            vibe_daily_cap = await site_flags.get(db, site_flags.GLOBAL_DAILY_ANLAS, settings)
        if vibe_daily_cap > 0:
            return None      # 针对 Vibe 开放全站每日 Anlas PVP 抢额度（由 quota_image_check 统一校验全站池）
    if allowed is not None and name not in allowed:
        if name in ANLAS_FEATURES:
            return (f"{FEATURES[name]}会消耗 Anlas（{ANLAS_FEATURES[name]}），本站只提供免费出图，暂不开放。"
                    "请在客户端里关闭这个功能后再生成")
        return f"你的 Key 暂无权使用：{FEATURES[name]}（可向站长申请）"
    return None
