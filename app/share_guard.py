"""防分享：证据 + 风险分 + 自动处罚（share_guard_mode：未设置时默认 observe；线上为 observe，校准前只记录不处罚）。

站长原则：Key 只给本人用，「做慈善眼里容不得沙子」。但一个人也会换网络、用两台设备，
所以每条信号都要求「一个人很难做到」的模式，单次换网络不加分。

━━ 证据（每条写进 share_evidence，后台可溯源）━━
  成员大多走 VPN / 代理，代理按请求轮换出口时，一个人也会在几分钟内出现好几个网络（10-09 回测：原神高手 2 小时 4 个境外节点、
  kfzbrlm 用 WARP），所以「网络不同」单独不算数，必须同时出现「不同的客户端指纹」（完整 User-Agent：系统 + 浏览器 + 版本）。
  overlap      +35  上一张还在生成 / 排队，另一个网络、另一个客户端又发来请求 —— 两台设备在两个地方同时用。
  concurrent   +35  30 分钟内 ≥ 3 个网络在 90 秒内来回切换（≥ 2 次），并且来自 ≥ 2 种客户端指纹。
  alternate    +15  10 分钟内 A→B→A 来回切换 ≥ 2 次，并且来自 ≥ 2 种客户端指纹。
  multi_device +20  24 小时内 ≥ 3 种操作系统（Windows / Mac / Linux / Android / iOS）。
  many_clients +20  24 小时内 ≥ 4 种客户端指纹、≥ 2 种系统。
  allday       +15  近 24 小时里有 ≥ 20 个小时在用（作息不像一个人）。
  habits       +35  2 小时内同一把 Key 交替出现两套完全不同的「出图习惯」（参数签名不同、提示词固定部分几乎不重合），
                    并且两套习惯来自不同网络或不同客户端。每个人都有固定的画师串 / jailbreak / 质量词 / 采样器步数 CFG /
                    负面词，换 VPN 改不掉；换了一套画师串的人是「先 A 后 B」，不会 A、B、A、B 来回交替。
                    只有习惯交替、网络和客户端都相同 → 只记录不计分（可能是一个人开了两个对话）。
  同一类证据每把 Key 30 分钟（multi_device / many_clients / allday 为 24 小时）最多记一次。

  三重滤网（2026-10-10 盘点后修正）：multi_device / many_clients / allday 只看 UA 或作息，是「辅助证据」——
  只有最近 72 小时内出现过「网络 + 客户端」的强证据（overlap / concurrent / alternate）才计分，否则只记录、0 分。
  （统计审查指出：强证据之间高度相关、加分没有校准，误判率未知 —— 2026-10-10 起默认只观察，校准前不执行。）

━━ 风险分 ━━
  所有证据分数按半衰期 48 小时衰减后相加。正常换网络的人分数会自己降下来。

━━ 处罚（逐级）━━
  ≥ 30  私信提醒本人（Key 仅限本人使用，再出现会暂停），24 小时最多一次。
  ≥ 60  暂停 24 小时（72 小时内最多一次），私信说明原因和恢复时间。
  ≥ 100 重置 Key：旧 Key 立刻失效，新 Key 私信本人 —— Key 被别人拿走的话，本人不受影响，别人用不了了。
        记一次「违规」，分数清零。
  违规 ≥ 3 次：停用并禁止再领取，私信站长。
  每一步都写操作日志并私信站长；站长可在后台清零（误判时）。测试 Key、站长 Key 不参与。
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from collections import deque
from typing import Any, Awaitable, Callable, Optional

POINTS = {"habits": 35, "overlap": 35, "concurrent": 35, "alternate": 15, "multi_device": 20, "many_clients": 20, "allday": 15}
KIND_NAMES = {"habits": "两套出图习惯交替", "overlap": "两地同时出图", "concurrent": "多地同时使用", "alternate": "网络来回切换", "multi_device": "多种设备",
              "many_clients": "客户端过多", "allday": "全天在用", "action": "处罚"}
HALF_LIFE = 48 * 3600
WARN, PAUSE, RESET = 30, 60, 100
PAUSE_SECONDS = 24 * 3600
PAUSE_COOLDOWN = 72 * 3600
WARN_COOLDOWN = 24 * 3600
STRIKES_BAN = 3
FAST_SWITCH = 90
CONCURRENT_WINDOW = 1800
DEDUPE = {"habits": 3600, "overlap": 1800, "concurrent": 1800, "alternate": 1800, "multi_device": 86400, "many_clients": 86400,
          "allday": 86400}
STRONG = ("habits", "overlap", "concurrent", "alternate")
CONFIRM_WINDOW = 72 * 3600     # 辅助证据只在 72 小时内有过强证据时计分
OVERLAP_WINDOW = 180          # 「上一张还没完」只看最近 3 分钟内的请求
MODE_SETTING = "share_guard_mode"          # enforce / observe（默认）/ off；读写走 site_flags.SHARE_MODE

Notify = Callable[[int, str], Awaitable[None]]

# 运行时一律经 P() 读取：服务器 data/private_params.json 里的私密值优先，上面的常量只是公开起点（见 params.py）
from .params import P


def _pts(kind: str) -> float:
    return P(f"share.points.{kind}", POINTS[kind])


def os_family(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "android" in ua:
        return "Android"
    if "iphone" in ua or "ipad" in ua or "ios" in ua:
        return "iOS"
    if "windows" in ua:
        return "Windows"
    if "mac os" in ua or "macintosh" in ua:
        return "Mac"
    if "linux" in ua or "x11" in ua:
        return "Linux"
    return ""                                  # 服务器端客户端（node-fetch、python 等）不算设备


def _flag(key, name: str) -> bool:
    try:
        return bool(key[name])
    except (KeyError, IndexError, TypeError):
        return False


def decayed(score: float, since: float, now: float) -> float:
    return score * math.pow(0.5, max(0.0, now - since) / P("share.half_life", HALF_LIFE))


# ---------- 出图习惯指纹（只在内存里保留哈希，不存提示词原文）----------
HABIT_WINDOW = 2 * 3600
_WEIGHT = re.compile(r"^-?\d+(\.\d+)?::|::$|[{}\[\]()]")


def _tokens(text: str) -> set[str]:
    out = set()
    for raw in re.split(r"[,\n|]", text or ""):
        t = _WEIGHT.sub("", raw.strip().lower()).strip(" :.")
        if 2 <= len(t) <= 60:
            out.add(hashlib.sha1(t.encode()).hexdigest()[:10])
    return out


def habit_fingerprint(body: dict) -> tuple[str, frozenset]:
    """(参数签名, 提示词词条哈希集合)。参数签名 = 采样器 / 步数 / CFG / 噪声表 / 负面词预设 / 质量词开关 / 负面词。"""
    p = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}
    texts = [body.get("input") or "", p.get("prompt") or ""]
    cap = (p.get("v4_prompt") or {}).get("caption") if isinstance(p.get("v4_prompt"), dict) else None
    if isinstance(cap, dict):
        texts.append(cap.get("base_caption") or "")
        for c in cap.get("char_captions") or []:
            if isinstance(c, dict):
                texts.append(c.get("char_caption") or "")
    neg = p.get("negative_prompt") or ""
    ncap = (p.get("v4_negative_prompt") or {}).get("caption") if isinstance(p.get("v4_negative_prompt"), dict) else None
    if isinstance(ncap, dict):
        neg += "|" + (ncap.get("base_caption") or "")
    try:
        scale = round(float(p.get("scale") or 0), 1)
    except (TypeError, ValueError):
        scale = 0
    sig = "|".join(str(x) for x in (p.get("sampler"), p.get("steps"), scale, p.get("noise_schedule"),
                                   p.get("ucPreset"), p.get("qualityToggle"),
                                   hashlib.sha1(neg.strip().lower().encode()).hexdigest()[:8]))
    toks = set()
    for t in texts:
        toks |= _tokens(t)
    return hashlib.sha1(sig.encode()).hexdigest()[:10], frozenset(toks)


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


class _Habit:
    """一套出图习惯：参数签名 + 「固定部分」（出现在一半以上请求里的词条）。"""
    def __init__(self, sig: str, toks: frozenset):
        self.sig, self.n, self.counts = sig, 0, {}
        self.add(toks)

    def add(self, toks: frozenset) -> None:
        self.n += 1
        for t in toks:
            self.counts[t] = self.counts.get(t, 0) + 1

    def core(self) -> set:
        need = max(1, self.n * 0.5)
        return {t for t, c in self.counts.items() if c >= need}


class ShareGuard:
    def __init__(self, db):
        self.db = db
        self._trail: dict[int, deque] = {}       # key_id → (ts, 网络标签, 地址族, 系统)
        self._hours: dict[int, set] = {}
        self._last: dict[tuple[int, str], float] = {}
        self.paused: dict[int, float] = {}
        self.pause_reasons: dict[int, str] = {}    # key_id → 给成员看的暂停原因（非防分享的暂停，如自动驾驶限流）
        self._habits: dict[int, deque] = {}        # key_id → 暂停到期时间（启动时从库里读）

    async def load(self) -> None:
        rows = await self.db._db.execute_fetchall(
            "SELECT key_id, paused_until, pause_reason FROM share_state WHERE paused_until > ?", (time.time(),))
        self.paused = {int(r[0]): float(r[1]) for r in rows}
        self.pause_reasons = {int(r[0]): r[2] for r in rows if r[2]}

    def paused_until(self, key_id: int, now: Optional[float] = None) -> float:
        until = self.paused.get(int(key_id), 0.0)
        return until if until > (time.time() if now is None else now) else 0.0

    async def mode(self) -> str:
        """未设置或值不对时按 observe 处理（fail-closed）：库重建 / 恢复后不会突然开始处罚成员。"""
        from . import site_flags
        return await site_flags.get(self.db, site_flags.SHARE_MODE)

    async def pause_key(self, key_id: int, seconds: float, reason: str, now: Optional[float] = None) -> bool:
        """非防分享的 Key 暂停（如自动驾驶对死循环重试限流）。复用 paused_until 闸门和 main.py 的拦截，
        但不动风险分 / 违规次数 / paused_ts，所以不影响防分享计分。已在暂停中则不重复，返回是否新暂停。"""
        now = time.time() if now is None else now
        kid = int(key_id)
        if self.paused.get(kid, 0.0) > now:
            return False
        s = await self._state(kid)
        s["paused_until"] = now + max(0.0, seconds)
        s["pause_reason"] = reason
        await self._save(kid, s)
        self.paused[kid] = s["paused_until"]
        self.pause_reasons[kid] = reason
        return True

    # ---------- 信号 ----------
    def signals(self, key_id: int, label: str, family: int, ua: str, now: float, busy: bool = False) -> list[tuple[str, str]]:
        """busy：这把 Key 此刻还有图在生成或排队。"""
        osf, fp = os_family(ua), (ua or "").strip()[:80]
        trail = self._trail.setdefault(key_id, deque(maxlen=300))
        prev = trail[-1] if trail else None
        trail.append((now, label, family, osf, fp))
        while trail and trail[0][0] < now - 86400:
            trail.popleft()
        hours = self._hours.setdefault(key_id, set())
        hours.add(int(now // 3600))
        for h in [h for h in hours if h < int(now // 3600) - 23]:
            hours.discard(h)
        out: list[tuple[str, str]] = []
        if busy and prev and now - prev[0] <= P("share.overlap_window", OVERLAP_WINDOW) and prev[1] != label and prev[4] and fp and prev[4] != fp:
            out.append(("overlap", f"上一张还没完成，{label}（{osf or '未知系统'}）又发来请求，"
                                   f"上一张来自 {prev[1]}（{prev[3] or '未知系统'}）"))
        same = [(t, lb, f) for t, lb, fam, _, f in trail if fam == family and lb]
        recent = [x for x in same if x[0] >= now - P("share.concurrent_window", CONCURRENT_WINDOW)]
        nets = {lb for _, lb, _ in recent}
        fps = {f for _, _, f in recent if f}
        fast = [1 for (ta, a, _), (tb, b, _) in zip(recent, recent[1:]) if a != b and tb - ta <= P("share.fast_switch", FAST_SWITCH)]
        if len(nets) >= P("share.concurrent_nets", 3) and len(fast) >= P("share.concurrent_fast", 2) and len(fps) >= 2:
            out.append(("concurrent", f"30 分钟内 {len(nets)} 个网络、{len(fps)} 种客户端交替在用"
                                      f"（{'、'.join(sorted(nets)[:5])}）"))
        win = [(lb, f) for t, lb, f in same if t >= now - 600]
        seq = [lb for i, (lb, _) in enumerate(win) if i == 0 or lb != win[i - 1][0]]
        flips = sum(1 for i in range(2, len(seq)) if seq[i] == seq[i - 2] != seq[i - 1])
        if flips >= 2 and len({f for _, f in win if f}) >= 2:
            out.append(("alternate", f"10 分钟内来回切换 {flips} 次（{'→'.join(seq[-5:])}），客户端也不同"))
        oses = {o for _, _, _, o, _ in trail if o}
        allfp = {f for _, _, _, _, f in trail if f}
        if len(oses) >= P("share.multi_device_os", 3):
            out.append(("multi_device", f"24 小时内 {len(oses)} 种系统（{'、'.join(sorted(oses))}）"))
        if len(allfp) >= P("share.many_clients", 4) and len(oses) >= 2:
            out.append(("many_clients", f"24 小时内 {len(allfp)} 种客户端、{len(oses)} 种系统"))
        if len(hours) >= P("share.allday_hours", 20):
            out.append(("allday", f"近 24 小时有 {len(hours)} 个小时在用"))
        fresh = []
        for kind, text in out:
            if now - self._last.get((key_id, kind), 0) >= P(f"share.dedupe.{kind}", DEDUPE[kind]):
                self._last[(key_id, kind)] = now
                fresh.append((kind, text))
        return fresh

    # ---------- 记账与处罚 ----------
    async def _state(self, key_id: int) -> dict[str, Any]:
        rows = await self.db._db.execute_fetchall(
            "SELECT score, score_ts, strikes, paused_until, warned_ts, paused_ts, pause_reason FROM share_state WHERE key_id=?",
            (key_id,))
        if not rows:
            return {"score": 0.0, "score_ts": 0.0, "strikes": 0, "paused_until": 0.0, "warned_ts": 0.0, "paused_ts": 0.0,
                    "pause_reason": ""}
        r = rows[0]
        return {"score": float(r[0]), "score_ts": float(r[1]), "strikes": int(r[2]), "paused_until": float(r[3]),
                "warned_ts": float(r[4]), "paused_ts": float(r[5]), "pause_reason": r[6] or ""}

    async def _save(self, key_id: int, s: dict[str, Any]) -> None:
        await self.db._db.execute(
            "INSERT OR REPLACE INTO share_state(key_id, score, score_ts, strikes, paused_until, warned_ts, paused_ts, pause_reason) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (key_id, s["score"], s["score_ts"], s["strikes"], s["paused_until"], s["warned_ts"], s["paused_ts"],
             s.get("pause_reason", "")))

    async def _evidence(self, key_id: int, kind: str, points: float, detail: str, now: float) -> None:
        await self.db._db.execute("INSERT INTO share_evidence(ts, key_id, kind, points, detail) VALUES (?,?,?,?,?)",
                                  (now, key_id, kind, points, detail[:300]))

    async def observe(self, key, ip_label: Optional[str], family: int, user_agent: str, *,
                      now: Optional[float] = None, busy: bool = False, member: Optional[Notify] = None,
                      admin: Optional[Callable[[str], None]] = None,
                      reset: Optional[Callable[[int], Awaitable[Optional[str]]]] = None,
                      ban: Optional[Callable[[int], Awaitable[None]]] = None) -> Optional[str]:
        """记录一次请求；返回执行的处罚（warn / pause / reset / ban）或 None。"""
        if _flag(key, "is_admin") or _flag(key, "is_test") or not ip_label:
            return None
        mode = await self.mode()
        if mode == "off":
            return None
        now = time.time() if now is None else now
        kid = int(key["id"])
        found = self.signals(kid, ip_label, family, user_agent, now, busy)
        if not found:
            return None
        return await self._apply(key, found, now, member=member, admin=admin, reset=reset, ban=ban)

    async def _apply(self, key, found: list[tuple[str, str]], now: float, *, member: Optional[Notify] = None,
                     admin: Optional[Callable[[str], None]] = None,
                     reset: Optional[Callable[[int], Awaitable[Optional[str]]]] = None,
                     ban: Optional[Callable[[int], Awaitable[None]]] = None) -> Optional[str]:
        """证据计分（三重滤网）→ 风险分 → 逐级处罚。"""
        mode = await self.mode()
        if mode == "off":
            return None
        kid = int(key["id"])
        s = await self._state(kid)
        if s["strikes"] >= P("share.strikes_ban", STRIKES_BAN):          # 已停用，不再重复处罚
            return None
        score = decayed(s["score"], s["score_ts"], now)
        strong_recent = any(k in STRONG for k, _ in found) or bool(await self.db._db.execute_fetchall(
            f"SELECT 1 FROM share_evidence WHERE key_id=? AND ts>=? AND points>0 AND kind IN ({','.join('?' * len(STRONG))}) LIMIT 1",
            (kid, now - P("share.confirm_window", CONFIRM_WINDOW), *STRONG)))
        counted = []
        for kind, text in found:
            pts = _pts(kind) if (kind in STRONG or strong_recent) else 0
            score += pts
            if pts:
                counted.append((kind, text))
            await self._evidence(kid, kind, pts, text if pts else text + "（辅助证据，没有强证据印证，不计分）", now)
        if not counted:
            await self.db._db.commit()
            return None
        found = counted
        s["score"], s["score_ts"] = score, now
        name = key["name"]
        why = "；".join(t for _, t in found)
        action = None
        # 逐级处罚（2026-10-10 统计审查后修正）：每次最多升一级，重置前必须先暂停过、暂停前必须先提醒过（7 天内）。
        # 否则一次请求触发多条相关证据就能从 0 分直接跳到重置。
        recent = now - 7 * 86400
        warned_before = s["warned_ts"] >= recent
        paused_before = s["paused_ts"] >= recent
        if mode == "enforce":
            if score >= P("share.reset", RESET) and paused_before and now >= s["paused_until"]:
                s["strikes"] += 1
                s["score"] = 0.0
                if s["strikes"] >= P("share.strikes_ban", STRIKES_BAN) and ban is not None:
                    action = "ban"
                    await self._evidence(kid, "action", 0, f"第 {s['strikes']} 次违规：停用并禁止再领取", now)
                    await self._save(kid, s)
                    await self.db._db.commit()
                    await ban(kid)
                    if admin:
                        admin(f"🚫 防分享：「{name}」第 {s['strikes']} 次违规，已停用并禁止再领取。证据：{why}")
                    return action
                action = "reset"
                await self._evidence(kid, "action", 0, f"重置 Key（第 {s['strikes']} 次违规）", now)
                await self._save(kid, s)
                await self.db._db.commit()
                token = await reset(kid) if reset is not None else None
                if member and token:
                    await member(kid, f"检测到你的 Key 被多人同时使用（{why}）。Key 仅限本人使用，已为你重置，旧 Key 立即失效。"
                                      f"新的 Key：`{token}`\n这是第 {s['strikes']} 次，累计 {STRIKES_BAN} 次将停用并禁止再领取。"
                                      "如果是误判，请联系站长。")
                if admin:
                    admin(f"🔁 防分享：「{name}」风险分达到 {RESET}，已重置 Key（第 {s['strikes']} 次）。证据：{why}")
                return action
            if (score >= P("share.pause", PAUSE) and warned_before
                    and now - s["paused_ts"] >= P("share.pause_cooldown", PAUSE_COOLDOWN)):
                action = "pause"
                s["paused_until"], s["paused_ts"] = now + P("share.pause_seconds", PAUSE_SECONDS), now
                s["pause_reason"] = ""
                self.paused[kid] = s["paused_until"]
                self.pause_reasons.pop(kid, None)
                await self._evidence(kid, "action", 0, "暂停 24 小时", now)
                if member:
                    await member(kid, f"检测到你的 Key 可能被多人使用（{why}），已暂停 24 小时，"
                                      f"{time.strftime('%m-%d %H:%M', time.localtime(s['paused_until']))} 自动恢复。"
                                      "Key 仅限本人使用；再出现会重置 Key。如果是误判，请联系站长。")
                if admin:
                    admin(f"⏸ 防分享：「{name}」风险分 {score:.0f}，已暂停 24 小时。证据：{why}")
            elif score >= P("share.warn", WARN) and now - s["warned_ts"] >= P("share.warn_cooldown", WARN_COOLDOWN):
                action = "warn"
                s["warned_ts"] = now
                await self._evidence(kid, "action", 0, "私信提醒", now)
                if member:
                    await member(kid, f"提醒：检测到你的 Key 使用情况异常（{why}）。Key 仅限本人使用，"
                                      "请不要分享给别人；继续出现会暂停或重置 Key。如果只是换了网络，可以忽略这条。")
                if admin:
                    admin(f"⚠ 防分享：「{name}」风险分 {score:.0f}，已私信提醒。证据：{why}")
        elif admin and score >= P("share.warn", WARN):
            admin(f"👀 防分享（观察）：「{name}」风险分 {score:.0f}。证据：{why}")
        await self._save(kid, s)
        await self.db._db.commit()
        return action

    def habit_signal(self, key_id: int, sig: str, toks: frozenset, label: str, ua: str, now: float) -> Optional[tuple[str, str, bool]]:
        """返回 (kind, 说明, 是否有网络/客户端差异) 或 None。"""
        hist = self._habits.setdefault(key_id, deque(maxlen=120))
        hist.append((now, sig, toks, label or "", (ua or "")[:80]))
        while hist and hist[0][0] < now - P("share.habit_window", HABIT_WINDOW):
            hist.popleft()
        if len(hist) < 6:
            return None
        habits: list[_Habit] = []
        seq, where = [], {}
        for _, sg, tk, lb, fp in hist:              # 在线聚类：参数签名相同或和固定部分足够像就归为同一套习惯
            for i, h in enumerate(habits):
                if h.sig == sg or _jaccard(set(tk), h.core()) >= P("share.habit_same", 0.3):
                    h.add(tk)
                    break
            else:
                habits.append(_Habit(sg, tk))
                i = len(habits) - 1
            seq.append(i)
            where.setdefault(i, set()).add((lb, fp))
        big = [i for i, h in enumerate(habits) if h.n >= 3]
        if len(big) < 2:
            return None
        a, b = big[0], big[1]
        if habits[a].sig == habits[b].sig or _jaccard(habits[a].core(), habits[b].core()) >= P("share.habit_distinct", 0.15):
            return None
        ab = [x for x in seq if x in (a, b)]
        switches = sum(1 for x, y in zip(ab, ab[1:]) if x != y)
        if switches < P("share.habit_switches", 3):                             # 先 A 后 B 是换了画师串；A、B、A、B 才是两个人
            return None
        la, lb_ = {x[0] for x in where[a]}, {x[0] for x in where[b]}
        fa, fb = {x[1] for x in where[a]}, {x[1] for x in where[b]}
        differs = not (la & lb_) or not (fa & fb)
        return ("habits", f"2 小时内两套出图习惯来回交替 {switches} 次（各 {habits[a].n} / {habits[b].n} 张，"
                          f"参数和提示词固定部分都不同）" + ("，且来自不同网络或客户端" if differs else "，网络和客户端相同"), differs)

    async def observe_habit(self, key, body: dict, label: Optional[str], user_agent: str, *,
                            now: Optional[float] = None, **callbacks) -> Optional[str]:
        """出图请求的习惯指纹；有差异的交替算强证据，走和 observe 一样的计分与处罚。"""
        if _flag(key, "is_admin") or _flag(key, "is_test"):
            return None
        now = time.time() if now is None else now
        kid = int(key["id"])
        sig, toks = habit_fingerprint(body)
        try:                                  # 回测用的特征（只存哈希）；写失败不影响判断
            await self.db._db.execute(
                "INSERT INTO req_features(ts, key_id, src, fp, os, sig, toks, busy) VALUES (?,?,?,?,?,?,?,?)",
                (now, kid, label or "", hashlib.sha1((user_agent or "").encode()).hexdigest()[:10],
                 os_family(user_agent), sig, " ".join(sorted(toks)[:80]), int(bool(callbacks.pop("busy", False)))))
            await self.db._db.commit()
        except Exception:
            pass
        found = self.habit_signal(kid, sig, toks, label or "", user_agent, now)
        if not found or now - self._last.get((kid, "habits"), 0) < P("share.dedupe.habits", DEDUPE["habits"]):
            return None
        self._last[(kid, "habits")] = now
        kind, text, differs = found
        if not differs:
            await self._evidence(kid, kind, 0, text + "（只记录：可能是一个人开了两个对话）", now)
            await self.db._db.commit()
            return None
        return await self._apply(key, [(kind, text)], now, **callbacks)

    async def clear(self, key_id: int) -> None:
        """站长判定误判：清零分数、解除防分享暂停、违规次数归零（证据保留）。
        自动驾驶等非防分享的暂停（有 pause_reason）不是误判对象，保留到期。"""
        kid, now = int(key_id), time.time()
        s = await self._state(kid)
        if s["pause_reason"] and s["paused_until"] > now:
            await self._save(kid, {**s, "score": 0.0, "score_ts": now, "strikes": 0, "warned_ts": 0.0, "paused_ts": 0.0})
        else:
            self.paused.pop(kid, None)
            self.pause_reasons.pop(kid, None)
            await self.db._db.execute("DELETE FROM share_state WHERE key_id=?", (key_id,))
        await self._evidence(int(key_id), "action", 0, "站长清零（误判）", time.time())
        await self.db._db.commit()

    async def report(self, now: Optional[float] = None) -> list[dict[str, Any]]:
        """后台：有分数或证据的 Key，按当前风险分排序。"""
        now = time.time() if now is None else now
        rows = await self.db._db.execute_fetchall(
            "SELECT s.key_id, COALESCE(k.name, '#' || s.key_id), s.score, s.score_ts, s.strikes, s.paused_until "
            "FROM share_state s LEFT JOIN api_keys k ON k.id=s.key_id")
        out = []
        for kid, name, score, ts, strikes, paused in rows:
            ev = await self.db._db.execute_fetchall(
                "SELECT ts, kind, points, detail FROM share_evidence WHERE key_id=? ORDER BY ts DESC LIMIT 20", (kid,))
            out.append({"key_id": kid, "name": name, "score": round(decayed(score, ts, now), 1), "strikes": strikes,
                        "paused_until": paused if paused > now else 0,
                        "evidence": [{"ts": e[0], "kind": e[1], "label": KIND_NAMES.get(e[1], e[1]), "points": e[2],
                                      "detail": e[3]} for e in ev]})
        return sorted(out, key=lambda r: (-r["strikes"], -r["score"]))

    async def evidence(self, key_id: int, limit: int = 30) -> list[dict[str, Any]]:
        ev = await self.db._db.execute_fetchall(
            "SELECT ts, kind, points, detail FROM share_evidence WHERE key_id=? ORDER BY ts DESC LIMIT ?", (key_id, limit))
        return [{"ts": e[0], "kind": e[1], "label": KIND_NAMES.get(e[1], e[1]), "points": e[2], "detail": e[3]} for e in ev]
