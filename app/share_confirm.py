"""网络异常确认（10-11 站长）：被防分享 / 多网段检测标记过的成员，在首页回答「是不是在用 VPN / 代理」。

不发私信：成员登录首页时自己看到问题，回答存起来，后台和检测记录放在一起核对误判率。
只问一次；之后又有新的检测记录（距上次回答超过 7 天）才再问。"""
from __future__ import annotations

import time
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS share_confirm (
    key_id INTEGER PRIMARY KEY,
    answer TEXT NOT NULL,           -- vpn / devices / others
    note TEXT NOT NULL DEFAULT '',
    answered_at REAL NOT NULL,
    flagged_at REAL NOT NULL DEFAULT 0   -- 回答时最近一条检测记录的时间
);
"""

ANSWERS = {
    "vpn": "只有我一个人用，开了 VPN / 代理（节点会自动切换）",
    "devices": "只有我一个人用，在几台设备或几个网络之间切换",
    "others": "有别人也在用这把 Key",
}
LOOKBACK = 14 * 86400      # 看最近 14 天的检测记录
REASK_AFTER = 7 * 86400    # 回答过以后，新的检测记录要晚于回答 7 天才再问
NETS_ALERT = 3             # 24 小时内 ≥ 3 个网段也算异常（和后台成员页的提示一致）


_READY: set[int] = set()      # 已建表的连接（executescript 会隐式提交，不能每次请求都跑）


async def ensure(db) -> None:
    if id(db._db) in _READY:
        return
    await db._db.executescript(SCHEMA)
    _READY.add(id(db._db))


async def _flagged_at(db, key_id: int, now: float) -> float:
    """这把 Key 最近一次被标记的时间；没被标记返回 0。"""
    r = await db._db.execute_fetchall(
        "SELECT MAX(ts) FROM share_evidence WHERE key_id=? AND ts>=? AND points>0", (key_id, now - LOOKBACK))
    ev = float(r[0][0] or 0)
    nets = await db._db.execute_fetchall(
        "SELECT COUNT(*), MAX(last_seen) FROM key_sources WHERE key_id=? AND last_seen>=?", (key_id, now - 86400))
    if int(nets[0][0] or 0) >= NETS_ALERT:
        ev = max(ev, float(nets[0][1] or 0))
    return ev


async def pending(db, key_id: int, now: Optional[float] = None) -> bool:
    """首页要不要问这个成员。"""
    now = time.time() if now is None else now
    await ensure(db)
    flagged = await _flagged_at(db, key_id, now)
    if not flagged:
        return False
    row = await db._db.execute_fetchall("SELECT answered_at FROM share_confirm WHERE key_id=?", (key_id,))
    return not row or flagged > float(row[0][0]) + REASK_AFTER


async def record(db, key_id: int, answer: str, note: str = "", now: Optional[float] = None) -> bool:
    """记下回答；和上次一样则不重写，返回是否有变化（调用方据此决定要不要写操作日志）。"""
    if answer not in ANSWERS:
        raise ValueError("answer")
    now = time.time() if now is None else now
    await ensure(db)
    prev = await db._db.execute_fetchall("SELECT answer, note FROM share_confirm WHERE key_id=?", (key_id,))
    if prev and prev[0][0] == answer and prev[0][1] == note.strip()[:200]:
        return False
    flagged = await _flagged_at(db, key_id, now)
    await db._db.execute(
        "INSERT OR REPLACE INTO share_confirm(key_id, answer, note, answered_at, flagged_at) VALUES (?,?,?,?,?)",
        (key_id, answer, note.strip()[:200], now, flagged))
    await db._db.commit()
    return True


async def answers(db) -> dict[int, dict[str, Any]]:
    """后台用：每把 Key 的回答。"""
    await ensure(db)
    rows = await db._db.execute_fetchall("SELECT key_id, answer, note, answered_at, flagged_at FROM share_confirm")
    return {int(r[0]): {"answer": r[1], "label": ANSWERS.get(r[1], r[1]), "note": r[2],
                        "answered_at": r[3], "flagged_at": r[4]} for r in rows}


async def flagged_keys(db, now: Optional[float] = None) -> list[int]:
    """最近 14 天被标记过的 Key（发公告前看人数、后台核对用）。"""
    now = time.time() if now is None else now
    a = {int(r[0]) for r in await db._db.execute_fetchall(
        "SELECT DISTINCT key_id FROM share_evidence WHERE ts>=? AND points>0", (now - LOOKBACK,))}
    b = {int(r[0]) for r in await db._db.execute_fetchall(
        "SELECT key_id FROM key_sources WHERE last_seen>=? GROUP BY key_id HAVING COUNT(*)>=?", (now - 86400, NETS_ALERT))}
    return sorted(a | b)
