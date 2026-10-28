"""Private on-disk credentials for accounts managed from the admin panel."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile


class UpstreamManagementError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def credential(value: str) -> str:
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise UpstreamManagementError(422, "Token 必须是单行文本")
    value = value.strip()
    if not value or not value.isascii() or len(value) > 8192 or any(c.isspace() for c in value):
        raise UpstreamManagementError(422, "Token 必须是无空白的 ASCII 文本，长度为 1～8192 个字符")
    return value


def policy(v5_daily_limit, image_concurrency, allow_anlas, enabled) -> None:
    if type(v5_daily_limit) is not int or not 0 <= v5_daily_limit <= 100000:
        raise UpstreamManagementError(422, "上游 V5 日限额必须是 0～100000 的整数")
    if type(image_concurrency) is not int or not 1 <= image_concurrency <= 4:
        raise UpstreamManagementError(422, "上游图片并发必须是 1～4 的整数")
    if type(allow_anlas) is not bool or type(enabled) is not bool:
        raise UpstreamManagementError(422, "账号开关必须是布尔值")


def read_tokens(path: Path) -> list[dict] | None:
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or type(data.get("version")) is not int
                or data["version"] != 1 or not isinstance(data.get("tokens"), list)):
            raise ValueError
        ids, secrets = set(), set()
        for row in data["tokens"]:
            if not isinstance(row, dict) or set(row) != {
                "token_id", "token", "v5_daily_limit", "image_concurrency", "allow_anlas", "enabled"
            }:
                raise ValueError
            identity = row["token_id"]
            if not isinstance(identity, str) or not re.fullmatch(r"token-[0-9a-f]{16,32}", identity):
                raise ValueError
            value = credential(row["token"])
            policy(row["v5_daily_limit"], row["image_concurrency"], row["allow_anlas"], row["enabled"])
            if identity in ids or value in secrets:
                raise ValueError
            ids.add(identity)
            secrets.add(value)
            row["token"] = value
        os.chmod(path, 0o600)
        return data["tokens"]
    except (OSError, ValueError, TypeError, UpstreamManagementError):
        raise UpstreamManagementError(503, "上游账号配置无法读取，请检查受保护的账号配置文件") from None


def write_tokens(path: Path, rows: list[dict]) -> None:
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".upstream-", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump({"version": 1, "tokens": rows}, stream, ensure_ascii=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise UpstreamManagementError(503, "上游账号配置未能保存，当前账号保持不变") from None
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
