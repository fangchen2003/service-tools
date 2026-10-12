"""私密参数：大家一起用真实数据调出来的参数只放在服务器的 data/private_params.json（data/ 不进 git）。

代码是开源的，代码里写的数值只是「公开的起点」；服务器上的文件里有同名键时，以文件为准。
文件修改后自动重新读取（按修改时间），不用重启。后台「模块」页会标出哪些参数被私密值覆盖了，但值只在后台可见。

用法：P("share.warn", 30) —— 第一个参数是键名，第二个是公开起点值。键名按模块分组：
  capacity.*  容量    allocation.*  分配    share.*  诚信（防分享）
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {}
_MTIME = -1.0
_DEFAULTS: dict[str, Any] = {}       # 代码里登记过的起点值（后台对照用）


def path() -> str:
    return os.path.join(os.getenv("DATA_DIR", "data"), "private_params.json")


def _load() -> dict[str, Any]:
    global _CACHE, _MTIME
    p = path()
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        _CACHE, _MTIME = {}, -1.0
        return _CACHE
    if mtime != _MTIME:
        with _LOCK:
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                _CACHE = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                _CACHE = {}          # 文件坏了就退回起点值，不能让服务挂掉
            _MTIME = mtime
    return _CACHE


def P(name: str, default: Any) -> Any:
    """读参数：私密文件里有就用私密值（类型跟起点值一致），否则用起点值。"""
    _DEFAULTS.setdefault(name, default)
    v = _load().get(name, default)
    try:
        if isinstance(default, bool):
            return bool(v)
        if isinstance(default, int):
            return int(v)
        if isinstance(default, float):
            return float(v)
    except (TypeError, ValueError):
        return default
    return v


def overridden(name: str) -> bool:
    return name in _load()


def snapshot() -> dict[str, dict[str, Any]]:
    data = _load()
    keys = sorted(set(_DEFAULTS) | set(data))
    return {k: {"default": _DEFAULTS.get(k), "value": data.get(k, _DEFAULTS.get(k)), "private": k in data} for k in keys}


def update(values: dict[str, Any]) -> None:
    """后台 / 回测确认后写入私密值（原子替换文件）。值为 None 表示删除覆盖、回到起点值。"""
    with _LOCK:
        data = dict(_load())
        for k, v in values.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        d = os.path.dirname(path()) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".params-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path())
    global _MTIME
    _MTIME = -1.0
