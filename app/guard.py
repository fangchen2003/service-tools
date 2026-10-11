"""账号保护与排队（P0 / P1）：每个上游账号的每日 / 每小时出图上限、安静时段、请求间隔抖动、
每把 Key 的排队数、全站排队上限、V4.5 保底与空闲借用。

依据：fccc 的号在日均约 1370、峰值约 2500 张/天、全天不停时被 NovelAI 限制；NovelAI 条款禁止给服务造成
「excessive strain」。所以总量和节奏比单人限额更重要：让上游看到的是一个用得多、但有作息、不并发的正常用户。

所有数值存 site_settings（键名 guard_*），后台「设置 → 账号保护与排队」可改，立即生效；0 表示关闭该项。
"""
from __future__ import annotations

import math
import random
import time
from collections import deque
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

HOUR = 3600
WINDOW_3H = 3 * HOUR
HOURLY_MIN, HOURLY_MAX, HOURLY_STEP = 100, 160, 10     # 上限 ≤ 0.8 × 单线程物理极限 ≈ 205 张/小时     # 每小时上限的自动调整范围（加性增、乘性减）
# 名称: (默认值, 最小, 最大, 说明)
FIELDS: dict[str, tuple[int, int, int, str]] = {
    "account_daily_cap": (1000, 0, 20000, "每个上游账号每天最多出图张数"),
    "daily_cap_fallback": (1500, 0, 20000, "试探期的安全档：上游 1 小时内 ≥3 次 429 或连续 403 冷却时，每日上限自动退回这个数（只降不升；0 = 不自动退）"),
    "daily_burst": (150, 0, 20000, "每日上限按时段放行：0 点先放出这么多张，之后每小时补「每日上限 ÷ 24」张，没用完的留到后面；0 = 不分时段（整天先到先得）"),
    "account_hourly_cap": (150, 0, 240, "每个上游账号每小时最多出图张数（自动调整：上游 429 减半；前一天顶到过上限且没有 429 才 +10，范围 100～160）"),
    "account_3h_cap": (400, 0, 720, "每个上游账号连续 3 小时最多出图张数（防止连续几小时都在冲）"),
    "quiet_start": (0, 0, 23, "安静时段开始（北京时间，整点）；与结束相同 = 不启用（2026-10-10 起不启用：夜里不限速）"),
    "quiet_end": (0, 0, 23, "安静时段结束（北京时间，整点；与开始相同表示不设安静时段）"),
    "quiet_hourly_cap": (20, 0, 240, "安静时段每个账号每小时最多出图张数"),
    "interval_jitter": (5, 0, 30, "两次出图间隔额外随机增加 0～N 秒"),
    "key_image_queue": (1, 0, 3, "每把 Key 在生成中的那张之外，最多再排几张"),
    "queue_per_account": (5, 0, 100, "每个上游账号全站最多同时排几张图"),
    "base_daily_images": (100, 0, 100000, "V4.5 每人每天保底张数；超过后只在全站空闲时放行，直到 Key 的每日上限"),
}
IDLE_SHARE = 0.6
from .reasons import BREAKER, CAP_3H, DAILY_CAP, HOURLY_CAP, PACED, KEY_BUSY, QUEUE_FULL, QUIET_CAP, Reason
from .params import P           # 本小时用量低于上限的 60% 且没人排队，算「空闲」，允许借用


def _key(name: str) -> str:
    return "guard_" + name


class Guard:
    def __init__(self, db=None, tz: str = "Asia/Shanghai"):
        self.db = db
        self.tz = ZoneInfo(tz)
        self.values: dict[str, int] = {name: spec[0] for name, spec in FIELDS.items()}
        self._starts: dict[str, deque] = {}
        self.image_inflight: dict[int, int] = {}
        self.entries: list[dict] = []        # 进行中的出图：排队 / 生成，按进入时间排序
        self._seq = 0
        self.breaker_until = 0.0             # 全站熔断到期时间（自动驾驶 breaker 触发；内存态，重启即清）
        self._breaker_reason = ""

    # ---------- 设置 ----------
    async def load(self) -> None:
        if self.db is None:
            return
        for name, (default, low, high, _) in FIELDS.items():
            try:
                raw = await self.db.get_setting(_key(name), None)
                if raw is not None:
                    self.values[name] = max(low, min(high, int(float(raw))))
            except (TypeError, ValueError):
                self.values[name] = default

    async def save(self, body: dict) -> dict[str, int]:
        updates: dict[str, int] = {}
        for name, value in body.items():
            if name not in FIELDS:
                continue
            _, low, high, label = FIELDS[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
                raise ValueError(f"{label} 必须是整数")
            if not low <= int(value) <= high:
                raise ValueError(f"{label} 必须在 {low}～{high} 之间")
            updates[name] = int(value)
        if updates and self.db is not None:
            await self.db.set_settings_bulk({_key(k): v for k, v in updates.items()})
        self.values.update(updates)
        return dict(self.values)

    def describe(self) -> dict[str, Any]:
        return {"values": dict(self.values),
                "fields": {k: {"default": v[0], "min": v[1], "max": v[2], "label": v[3]} for k, v in FIELDS.items()},
                "quiet_now": self.in_quiet(), "hourly_cap_now": self.hourly_cap()}

    # ---------- 安静时段与每小时上限 ----------
    def in_quiet(self, now: Optional[float] = None) -> bool:
        start, end = self.values["quiet_start"], self.values["quiet_end"]
        if start == end:
            return False
        hour = datetime.fromtimestamp(time.time() if now is None else now, self.tz).hour
        return start <= hour < end if start < end else (hour >= start or hour < end)

    def hourly_cap(self, now: Optional[float] = None) -> int:
        return self.values["quiet_hourly_cap"] if self.in_quiet(now) else self.values["account_hourly_cap"]

    def record_start(self, token_id: str, now: Optional[float] = None) -> None:
        q = self._starts.setdefault(token_id, deque())
        q.append(time.time() if now is None else now)

    async def seed_hour(self, db, token_ids: list[str], now: Optional[float] = None) -> int:
        """启动时从用量日志补回最近 3 小时的出图，避免重启（部署）把每小时计数清零、绕过上限。
        日志里没有记是哪个上游账号，所以每个账号都按全站数量计（偏保守）。2026-10-10 00 点连部署 4 次，实际出了 85 张 > 80。"""
        now = time.time() if now is None else now
        rows = await db._db.execute_fetchall(
            "SELECT ts, images FROM usage_log WHERE ts>? AND status='ok' AND kind LIKE 'image%' AND images>0 ORDER BY ts",
            (now - WINDOW_3H,))
        stamps = [float(ts) for ts, n in rows for _ in range(int(n))]
        for tid in token_ids:
            q = self._starts.setdefault(tid, deque())
            merged = sorted(set(q) | set(stamps)) if q else stamps
            self._starts[tid] = deque(merged)
        return len(stamps)

    def _window(self, token_id: str, now: float, span: float) -> list[float]:
        q = self._starts.get(token_id)
        if not q:
            return []
        while q and q[0] <= now - WINDOW_3H:
            q.popleft()
        return [t for t in q if t > now - span]

    def hour_count(self, token_id: str, now: Optional[float] = None) -> int:
        return len(self._window(token_id, time.time() if now is None else now, HOUR))

    def count_3h(self, token_id: str, now: Optional[float] = None) -> int:
        return len(self._window(token_id, time.time() if now is None else now, WINDOW_3H))

    def minutes_until_free(self, token_id: str, now: Optional[float] = None) -> int:
        """到计数降回上限以下要等多久：超了 n 张就要等第 n+1 早的那张滑出 60 分钟窗口（不是最早那一张）。"""
        now = time.time() if now is None else now
        q = self._window(token_id, now, HOUR)
        if not q:
            return 1
        cap = self.hourly_cap(now)
        idx = max(0, min(len(q) - 1, len(q) - cap)) if cap else 0
        return max(1, math.ceil((q[idx] + HOUR - now) / 60))

    def minutes_until_free_3h(self, token_id: str, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        q = self._window(token_id, now, WINDOW_3H)
        cap = self.values["account_3h_cap"]
        if not q or not cap:
            return 1
        idx = max(0, min(len(q) - 1, len(q) - cap))
        return max(1, math.ceil((q[idx] + WINDOW_3H - now) / 60))

    # ---------- 每小时上限自动调整（AIMD：上游 429 减半，平稳一天 +10）----------
    async def on_upstream_429(self, now: Optional[float] = None) -> Optional[tuple[int, int]]:
        """上游限流是我们唯一能拿到的「红线」信号：立刻把每小时上限减半（不低于 100），返回 (旧, 新)。"""
        now = time.time() if now is None else now
        old = self.values["account_hourly_cap"]
        # min(old, …)：上限已经被手动调到下限以下时，429 只能维持或降低，绝不能反而把它抬回下限
        new = min(old, max(P("capacity.hourly_min", HOURLY_MIN), int(old * P("capacity.hourly_decrease", 0.5))))
        await self._set_adaptive(new, now, last_429=now)
        return (old, new) if new != old else None

    async def fall_back_daily(self, why: str, now: Optional[float] = None) -> Optional[tuple[int, int]]:
        """每日上限试探期的保险丝（2026-10-11 站长定 1700，安全档 1500）：上游出现限流 / 封控信号时退回安全档。
        只降不升，退回后要站长手动再调高。返回 (旧, 新)；没变化返回 None。"""
        now = time.time() if now is None else now
        old, safe = self.values["account_daily_cap"], self.values["daily_cap_fallback"]
        if not safe or not old or old <= safe:
            return None
        self.values["account_daily_cap"] = safe
        if self.db is not None:
            await self.db.set_settings_bulk({_key("account_daily_cap"): safe, "guard_daily_fallback_at": now,
                                             "guard_daily_fallback_why": why[:200]})
        return old, safe

    def note_upstream_429(self, now: Optional[float] = None) -> int:
        """记一次上游 429，返回最近 1 小时内的次数（内存，重启清零）。"""
        now = time.time() if now is None else now
        seen = [t for t in getattr(self, "_upstream_429", []) if now - t < HOUR]
        # 同一次限流里几个在途请求会同时收到 429：60 秒内只算一次（Sonnet 审查 P1，避免一次就误退档）
        if not seen or now - seen[-1] >= 60:
            seen.append(now)
        self._upstream_429 = seen
        return len(seen)

    async def adapt_daily(self, now: Optional[float] = None) -> Optional[tuple[int, int]]:
        """每 24 小时最多一次：过去 24 小时没有上游 429 → +10（不超过 200）。"""
        if self.db is None:
            return None
        now = time.time() if now is None else now
        last_429 = float(await self.db.get_setting("guard_last_upstream_429", 0) or 0)
        last_step = float(await self.db.get_setting("guard_hourly_adapted_at", 0) or 0)
        if not last_step:                     # 第一次运行只开始计时：上线当天不能算「平稳了一天」
            await self.db.set_setting("guard_hourly_adapted_at", now)
            return None
        if now - last_step < 86400 or now - last_429 < 86400:
            return None
        # 只有上限真的「顶到过」才有信息：需求没碰到上限时，没有 429 不能说明上限可以更高（TCP 只在窗口用满时加窗）
        hit = await self.db._db.execute_fetchall(
            "SELECT 1 FROM usage_log u LEFT JOIN api_keys k ON k.id=u.key_id WHERE u.ts>? "
            "AND COALESCE(k.is_test,0)=0 AND COALESCE(k.is_admin,0)=0 "
            "AND u.reason=? LIMIT 1", (now - 86400, HOURLY_CAP))
        first = (await self.db._db.execute_fetchall("SELECT MIN(ts) FROM usage_log"))[0][0]
        covered = (now - max(now - 86400, float(first or now))) / 3600
        if not hit or covered < 20:          # 没顶到过上限（没信息），或过去 24 小时数据不完整：不加
            await self.db.set_setting("guard_hourly_adapted_at", now)
            return None
        old = self.values["account_hourly_cap"]
        new = min(P("capacity.hourly_max", HOURLY_MAX), old + P("capacity.hourly_step", HOURLY_STEP))
        await self._set_adaptive(new, now)
        return (old, new) if new != old else None

    async def _set_adaptive(self, value: int, now: float, last_429: Optional[float] = None) -> None:
        self.values["account_hourly_cap"] = value
        if self.db is None:
            return
        data = {_key("account_hourly_cap"): value, "guard_hourly_adapted_at": now}
        if last_429 is not None:
            data["guard_last_upstream_429"] = last_429
        await self.db.set_settings_bulk(data)

    def jitter(self) -> float:
        span = self.values["interval_jitter"]
        return random.uniform(0, span) if span else 0.0

    def trip_breaker(self, seconds: float, reason: str = "", now: Optional[float] = None) -> None:
        """全站熔断：在 seconds 秒内，所有上游账号都拒接新的出图任务（自动驾驶 breaker 用）。到点自动恢复。"""
        now = time.time() if now is None else now
        self.breaker_until = now + max(0.0, seconds)
        self._breaker_reason = reason or f"上游连续出错，已暂停出图 {int(seconds)} 秒，到点自动恢复"

    # ---------- 每日上限按时段放行（令牌桶：0 点有 burst 张，每小时补 daily/24，当天总量不超过 daily）----------
    def _day_elapsed(self, now: float) -> float:
        local = datetime.fromtimestamp(now, self.tz)
        return now - local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def paced_allowance(self, now: Optional[float] = None) -> float:
        """到现在为止，这个账号今天最多可以出多少张。"""
        now = time.time() if now is None else now
        daily, burst = self.values["account_daily_cap"], self.values["daily_burst"]
        return min(float(daily), burst + daily * self._day_elapsed(now) / 86400)

    def minutes_until_paced(self, used: float, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        daily, burst = self.values["account_daily_cap"], self.values["daily_burst"]
        at = (used + 1 - burst) * 86400 / daily if daily else 0
        return max(1, math.ceil((at - self._day_elapsed(now)) / 60))

    async def paced_headroom(self, db, token_ids: list[str], day: str, now: Optional[float] = None) -> float:
        """所有账号「按时段已放出、还没用掉」的张数之和；不分时段时返回无穷大。"""
        if not self.values["daily_burst"] or not self.values["account_daily_cap"]:
            return float("inf")
        allow = self.paced_allowance(now)
        total = 0.0
        for tid in token_ids:
            c = await db.get_upstream_counter(tid, day)
            total += max(0.0, allow - c.get("units", c["images"]))
        return total

    async def token_block_reason(self, db, token_id: str, day: str, now: Optional[float] = None) -> Optional[str]:
        """这个上游账号现在不能再接新的出图任务时，返回给成员看的原因。"""
        now = time.time() if now is None else now
        if self.breaker_until > now:
            return Reason(self._breaker_reason or f"上游连续出错，已暂停出图，约 {int(self.breaker_until - now)} 秒后自动恢复",
                          BREAKER)
        daily = self.values["account_daily_cap"]
        if daily:
            c = await db.get_upstream_counter(token_id, day)
            used = c.get("units", c["images"])          # 按算力折算：节约模式 14 步的图算半张
            if used >= daily:
                return Reason(f"本站今天的出图总量已达上限（每个账号 {daily} 张/天，按算力折算，用来保护上游账号），明天 0 点恢复", DAILY_CAP)
            if self.values["daily_burst"] and used >= self.paced_allowance(now):
                wait = self.minutes_until_paced(used, now)
                return Reason(f"全站出图按时段放行（每天 {daily} 张平均分到 24 小时，保证晚来的人也有图出），"
                              f"这个时段的份额已用完，约 {wait} 分钟后有空位", PACED)
        cap = self.hourly_cap(now)
        if cap and self.hour_count(token_id, now) >= cap:
            wait = self.minutes_until_free(token_id, now)
            if self.in_quiet(now):
                return Reason(f"现在是安静时段（{self.values['quiet_start']}:00–{self.values['quiet_end']}:00），"
                              f"出图放慢到每小时 {cap} 张，约 {wait} 分钟后有空位", QUIET_CAP)
            return Reason(f"本小时出图量已达上限（每小时 {cap} 张，用来保护上游账号），约 {wait} 分钟后有空位", HOURLY_CAP)
        cap3 = self.values["account_3h_cap"]
        if cap3 and self.count_3h(token_id, now) >= cap3:
            wait = self.minutes_until_free_3h(token_id, now)
            return Reason(f"最近 3 小时出图量已达上限（{cap3} 张，用来保护上游账号），约 {wait} 分钟后有空位", CAP_3H)
        return None

    # ---------- 排队（P1） ----------
    def admit_image(self, key_id: int, accounts: int) -> Optional[str]:
        """每把 Key 同时生成 1 张、最多再排 key_image_queue 张；全站排队总数有上限。返回拒绝原因或 None。"""
        mine = self.image_inflight.get(key_id, 0)
        extra = self.values["key_image_queue"]
        if mine >= 1 + extra:
            more = f"、最多再排 {extra} 张" if extra else ""
            return Reason(f"你的上一张图还没出完：每把 Key 同时只生成 1 张{more}，请等前面的完成后再发", KEY_BUSY)
        per = self.values["queue_per_account"]
        if per and sum(self.image_inflight.values()) >= per * max(1, accounts):
            return Reason(f"当前排队的人太多（全站最多同时排 {per * max(1, accounts)} 张），请稍后再试", QUEUE_FULL)
        self.image_inflight[key_id] = mine + 1
        self._seq += 1
        self.entries.append({"id": self._seq, "key": key_id, "since": time.time(), "running": False})
        return None

    def mark_running(self, key_id: int, entry_id: Optional[int] = None) -> None:
        """这把 Key 的一张排队中的图开始发往上游（实时架构图用）；给了 entry_id 就标记那一张。"""
        for e in self.entries:
            if e["key"] == key_id and not e["running"] and (entry_id is None or e["id"] == entry_id):
                e["running"] = True
                return

    def waiting_keys(self) -> list[int]:
        """当前排队中（未开始生成）的 key_id，按到达先后；公平调度影子对比用。"""
        return [e["key"] for e in sorted((e for e in self.entries if not e["running"]), key=lambda e: e["since"])]

    def release_image(self, key_id: int, entry_id: Optional[int] = None) -> None:
        n = self.image_inflight.get(key_id, 0) - 1
        if n > 0:
            self.image_inflight[key_id] = n
        else:
            self.image_inflight.pop(key_id, None)
        mine = [e for e in self.entries if e["key"] == key_id]
        if entry_id is not None:      # 按条目编号移除：排队中的那张被取消时，不能误删正在生成的那张
            done = next((e for e in mine if e["id"] == entry_id), None)
        else:                         # 旧调用：先移除正在生成的那张，否则移除最早的
            done = next((e for e in mine if e["running"]), mine[0] if mine else None)
        if done is not None:
            self.entries.remove(done)

    def queue_view(self, key_id: Optional[int] = None) -> dict:
        """匿名的排队视图：waiting / running 数量；给定 key_id 时附上这把 Key 的图所处位置（1 起）。"""
        waiting = sorted((e for e in self.entries if not e["running"]), key=lambda e: e["since"])
        running = [e for e in self.entries if e["running"]]
        out: dict[str, Any] = {"waiting": len(waiting), "running": len(running)}
        if key_id is not None:
            out["mine"] = ([{"state": "running"} for e in running if e["key"] == key_id]
                           + [{"state": "waiting", "position": i + 1}
                              for i, e in enumerate(waiting) if e["key"] == key_id])
        return out

    # ---------- 保底与借用 ----------
    def site_idle(self, key_id: int, token_ids: list[str], now: Optional[float] = None) -> bool:
        """没有其他成员在排队，且本小时用量低于上限的 60%：允许超出保底继续用。"""
        if any(n > 0 for k, n in self.image_inflight.items() if k != key_id):
            return False
        cap = self.hourly_cap(now)
        if not cap or not token_ids:
            return True
        used = sum(self.hour_count(t, now) for t in token_ids)
        return used < P("capacity.idle_share", IDLE_SHARE) * cap * len(token_ids)
