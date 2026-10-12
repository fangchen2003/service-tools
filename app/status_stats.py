"""成员接口（/ai/、/user/、/v1/）返回的 HTTP 状态码分布：后台总览「状态码分布」用。

请求结束时在内存里按「小时 + 状态码」计数，维护循环每 5 分钟写进 status_hourly；读的时候把还没写进去的也算上。
没发出响应就断开的请求记为 499（客户端提前断开），抛异常的记为 500。
"""
from __future__ import annotations

import time
from collections import Counter
from typing import Any, Optional

PATHS = ("/ai/", "/user/", "/v1/", "/nai/")
_PENDING: Counter = Counter()          # (小时起点, 状态码) → 次数

MEANING = {
    200: "成功", 400: "参数不对（尺寸、步数、不支持的功能等）", 401: "Key 错误、缺失或已失效",
    402: "额度用完（每日 / V5 / Anlas）", 403: "功能没开放（Vibe、放大、图生图、文本等）", 404: "地址填错了",
    405: "请求方法不对（多半是地址填错）", 409: "请求冲突", 413: "请求太大", 422: "参数格式不对",
    429: "太频繁 / 排队满 / 每小时上限", 499: "客户端提前断开", 500: "网关出错", 502: "上游出错",
    503: "上游不可用或冷却中", 504: "上游超时",
}


def tracked(path: str) -> bool:
    return path.startswith(PATHS)


def record(code: int, now: Optional[float] = None) -> None:
    hour = int((time.time() if now is None else now) // 3600) * 3600
    _PENDING[(hour, int(code))] += 1


async def flush(db) -> None:
    items = list(_PENDING.items())
    if not items:
        return
    _PENDING.clear()
    try:
        await db._db.executemany(
            "INSERT INTO status_hourly(hour, code, n) VALUES (?,?,?) "
            "ON CONFLICT(hour, code) DO UPDATE SET n=n+excluded.n", [(h, c, n) for (h, c), n in items])
        await db._db.commit()
    except Exception:
        for k, n in items:                 # 写失败：放回去下次再写
            _PENDING[k] += n
        raise


async def distribution(db, hours: int, now: Optional[float] = None, since: Optional[float] = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    if since is None:
        since = int((now - hours * 3600) // 3600) * 3600          # 按整点统计：会多算开头那个小时的一部分
    else:
        since = int(since)
    total: Counter = Counter()
    for code, n in await db._db.execute_fetchall(
            "SELECT code, SUM(n) FROM status_hourly WHERE hour>=? GROUP BY code", (since,)):
        total[int(code)] += int(n)
    for (h, c), n in _PENDING.items():
        if h >= since:
            total[c] += n
    all_n = sum(total.values())
    codes = [{"code": c, "n": n, "pct": round(n / all_n * 100, 1) if all_n else 0.0,
              "meaning": MEANING.get(c, "")} for c, n in sorted(total.items())]
    return {"hours": hours, "since": since, "total": all_n, "codes": codes}
