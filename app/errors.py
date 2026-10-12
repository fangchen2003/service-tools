"""Bug 追踪：把所有「不该发生」的错误按特征归并记下来，一个都不漏。

来源包括：接口未捕获的异常（500）、上游 5xx、后台任务（维护、Anlas 分配、上游表现检查……）失败、
记账写入失败、成员在图片返回前断开、首页 / 后台网页的脚本报错。

同一个 bug 反复出现只占一行（count 累加），所以表不会膨胀；第一次出现、或站长标记「已处理」后又出现（复发），
会私信站长一次。完整堆栈只打印到容器日志并存进 detail，只有后台能看；不记录 Key、提示词或 IP。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
import traceback
from typing import Any, Callable, Optional

LEVELS = ("error", "warn")
DETAIL_MAX = 4000
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_NUM = re.compile(r"\d+(\.\d+)?")


def _app_frame(exc: BaseException) -> str:
    """最里层属于本项目代码的那一帧：file.py:函数，用来区分「同一种异常、不同位置」。"""
    where = ""
    for frame in traceback.extract_tb(exc.__traceback__):
        if os.path.abspath(frame.filename).startswith(_APP_DIR):
            where = f"{os.path.basename(frame.filename)}:{frame.name}"
    return where


def signature(source: str, exc: Optional[BaseException] = None, title: str = "") -> str:
    if exc is not None:
        basis = f"{source}|{type(exc).__name__}|{_app_frame(exc)}"
    else:
        basis = f"{source}|{_NUM.sub('#', title)[:200]}"
    return hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()[:12]


class Tracker:
    def __init__(self, db=None, notify: Optional[Callable[[str, str, float], Any]] = None):
        self.db = db
        self.notify = notify
        self._printed: dict[str, float] = {}       # 同一个 bug 的完整堆栈 10 分钟内只打印一次
        self._tasks: set = set()
        self.recent: list[dict] = []                # 写库失败时仍可在内存里看到最近 50 条
        self._lock: Optional[asyncio.Lock] = None   # 同一个 bug 同时出现多次时，读-改-写要串行

    def capture(self, source: str, exc: Optional[BaseException] = None, *, title: str = "",
                detail: str = "", path: str = "", key_id: Optional[int] = None, rid: str = "",
                level: str = "error") -> str:
        """记录一次错误；永远不会抛异常，也不会阻塞调用方。返回错误特征码。"""
        try:
            level = level if level in LEVELS else "error"
            if exc is not None:
                title = title or f"{type(exc).__name__}: {str(exc)[:200]}"
                stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                detail = (detail + "\n" if detail else "") + stack
            sig = signature(source, exc, title)
            now = time.time()
            print(f"[bug] {level} {source} sig={sig} rid={rid or '-'} {title[:160]}", flush=True)
            if exc is not None and now - self._printed.get(sig, 0) > 600:
                self._printed[sig] = now
                print(detail[-DETAIL_MAX:], flush=True)
            event = {"sig": sig, "source": source[:60], "level": level, "title": title[:300],
                     "detail": detail[-DETAIL_MAX:], "path": path[:200], "key_id": key_id, "rid": rid[:16], "ts": now}
            self.recent = (self.recent + [event])[-50:]
            if self.db is not None:
                try:
                    task = asyncio.get_running_loop().create_task(self._store(event))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                except RuntimeError:
                    pass                               # 没有事件循环（同步测试），只保留内存记录
            return sig
        except Exception:                              # 追踪器自己出错也不能影响业务
            print("[bug] tracker failed", flush=True)
            return ""

    async def _store(self, e: dict) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            await self._store_locked(e)

    async def _store_locked(self, e: dict) -> None:
        try:
            db = self.db._db
            row = await (await db.execute("SELECT count, resolved_at FROM error_events WHERE sig=?", (e["sig"],))).fetchone()
            if row is None:
                await db.execute(
                    """INSERT INTO error_events (sig, source, level, title, detail, count, first_seen, last_seen,
                                                 last_rid, last_key, last_path) VALUES (?,?,?,?,?,1,?,?,?,?,?)""",
                    (e["sig"], e["source"], e["level"], e["title"], e["detail"], e["ts"], e["ts"],
                     e["rid"], e["key_id"], e["path"]))
                state = "new"
            else:
                await db.execute(
                    """UPDATE error_events SET count=count+1, last_seen=?, title=?, detail=?, last_rid=?,
                       last_key=?, last_path=?, resolved_at=NULL WHERE sig=?""",
                    (e["ts"], e["title"], e["detail"], e["rid"], e["key_id"], e["path"], e["sig"]))
                state = "regressed" if row[1] is not None else ""
            await db.commit()
            if state and e["level"] == "error" and self.notify is not None:
                head = "🐞 新 bug" if state == "new" else "🐞 bug 复发"
                where = f" · 请求 {e['rid']}" if e["rid"] else ""
                self.notify(f"bug_{e['sig']}", f"{head}（{e['source']}）：{e['title'][:200]}{where}。详情见后台「Bug 追踪」。", 3600)
        except Exception as exc:
            print(f"[bug] store failed: {type(exc).__name__}", flush=True)

    async def drain(self) -> None:
        """测试用：等所有写库任务完成。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def list(self, include_resolved: bool = False, limit: int = 100) -> list[dict]:
        where = "" if include_resolved else " WHERE resolved_at IS NULL"
        order = "ORDER BY (resolved_at IS NOT NULL), last_seen DESC" if include_resolved else "ORDER BY last_seen DESC"
        rows = await (await self.db._db.execute(
            f"""SELECT sig, source, level, title, detail, count, first_seen, last_seen, last_rid, last_key,
                       last_path, resolved_at FROM error_events{where} {order} LIMIT ?""",
            (max(1, min(int(limit), 500)),))).fetchall()
        keys = ("sig", "source", "level", "title", "detail", "count", "first_seen", "last_seen", "last_rid",
                "last_key", "last_path", "resolved_at")
        return [dict(zip(keys, r)) for r in rows]

    async def open_count(self) -> int:
        row = await (await self.db._db.execute("SELECT COUNT(*) FROM error_events WHERE resolved_at IS NULL")).fetchone()
        return int(row[0]) if row else 0

    async def resolve(self, sig: str) -> bool:
        row = await (await self.db._db.execute("SELECT resolved_at FROM error_events WHERE sig=?", (sig,))).fetchone()
        if row is None:
            return False
        if row[0] is None:
            await self.db._db.execute(
                "UPDATE error_events SET resolved_at=? WHERE sig=?", (time.time(), sig))
            await self.db._db.commit()
        return True

    async def resolve_all(self) -> int:
        cur = await self.db._db.execute(
            "UPDATE error_events SET resolved_at=? WHERE resolved_at IS NULL", (time.time(),))
        await self.db._db.commit()
        return cur.rowcount

    async def purge(self, before: float) -> None:
        """已处理且很久没再出现的记录清掉。"""
        await self.db._db.execute("DELETE FROM error_events WHERE resolved_at IS NOT NULL AND last_seen<?", (before,))
        await self.db._db.commit()
