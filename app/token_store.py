"""后台管理的上游 NovelAI Token 存储。

原始 Token 不写进 SQLite（数据库备份里不会有它），而是放在数据目录下一个权限为 600 的独立文件。
文件存在时它就是令牌池的唯一来源（后台“接管”）；不存在时使用 .env 里的 NAI_TOKENS。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

TOKEN_RE = re.compile(r"^pst-[A-Za-z0-9_\-]{16,200}$")


def valid_token(token: str) -> bool:
    return bool(TOKEN_RE.match(token))


def load(path: Path) -> Optional[list[dict]]:
    """返回 [{token, allow_anlas}]；文件不存在、损坏或为空返回 None（回退到 .env）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = [{"token": str(e["token"]), "allow_anlas": bool(e.get("allow_anlas", False))}
                   for e in data.get("tokens", []) if valid_token(str(e.get("token", "")))]
        return entries or None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def save(path: Path, entries: list[dict]) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)        # 残留的旧 .tmp 可能是 0644：O_CREAT 的权限只在新建时生效
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"tokens": entries}, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
