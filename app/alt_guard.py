"""疑似同一人（小号）识别：只观察、不处罚，结果在后台成员页显示为「疑似同一人」自动标签。

社区里查不出两个 Discord 账号是不是同一个人，只能看两把 Key 的使用行为。对每一对 Key 打分：

  信号                                   分数     说明
  用完一把马上换另一把（handoff）        每次 25（最多 50）
        A 因个人上限被拒（每日 / 保底 / V5 用完），10 分钟内 B 开始出图，且 B 之前 1 小时没动静
  来回交替（switch）                     ≥2 次 → 15，≥4 次 → 20
        两边请求按时间排在一起，A 刚用完 B 就接上（间隔 ≤ 3 分钟），反复出现
  同网段同时出图（concurrent）           1 次 → 10，≥2 次 → 20
        同一分钟两把都在出图、来源粗网段相同——绕过「每把 Key 同时只出 1 张」
  少见网段相同（rare_net）               30
        /24 网段 72 小时内只有这两把 Key 用过；Cloudflare WARP 这类多人共用的出口不算
  少见客户端完全相同（rare_client）      15
        同一个客户端标识 72 小时内只有这两把 Key 用过（常见浏览器版本很多人一样，不算）
  同一个少见出口（same_src）             10
  出图参数签名相同（same_sig）           25
        req_features.sig（采样器 / 步数等）72 小时内只有 ≤ 3 把 Key 用过；用同一份预设的不同人也可能相同
        来源粗网段（/16）不是公共出口，且 72 小时内只有 ≤ 3 把 Key 用过

先找候选：两把 Key 必须共用一个「指纹」——少见的 /24 网段、少见的客户端标识，或同一个非公共出口的粗网段
（≤ 3 把 Key 用过）。只在候选对里看行为。70 个人同时在用时，「有人撞上限、别人刚好上线」「请求交错」
天天都会巧合发生（2026-10-10 线上试跑：不先找候选时 10 对里几乎全是陌生人）。
连续的上限拒绝算一次（30 分钟内），客户端死循环重试不会刷出很多次「换号」。
总分 ≥ 50 且至少有一条「行为」信号（handoff / switch / concurrent）才标记——只靠网段相同可能是室友、
同学、同一个校园网，不能算。站长 / 测试 Key 不参与。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

WINDOW = 3 * 86400
HANDOFF_GAP = 600             # A 被拒后 10 分钟内 B 开始
HANDOFF_QUIET = 3600          # B 在这之前 1 小时没有请求
SWITCH_GAP = 180              # A→B 间隔 ≤ 3 分钟算一次交替
FLAG_SCORE = 50
EPISODE_GAP = 1800            # 30 分钟内的连续上限拒绝算同一次
SRC_RARE = 3                  # 同一个粗网段（/16）最多 3 把 Key 用过才算「指纹」
# 多人共用的出口（Cloudflare WARP / iCloud 专用代理等）：网段相同不说明是同一个人
SHARED_PREFIXES = ("104.28.", "104.22.", "104.16.", "104.17.", "172.64.", "172.68.", "172.69.", "162.158.")
# 个人上限被拒（不是全站排队 / 每小时上限那种所有人都会遇到的）
PERSONAL_LIMIT = ("%已达今日%", "%保底%", "%V5 额度%")


def _is_shared(label: str) -> bool:
    return any(label.startswith(p) for p in SHARED_PREFIXES)


async def scan(db, now: float) -> list[dict[str, Any]]:
    """返回疑似同一人的 Key 对（按分数从高到低）。纯读库，不改任何东西。"""
    since = now - WINDOW
    staff = {r[0] for r in await db._db.execute_fetchall("SELECT id FROM api_keys WHERE is_test=1 OR is_admin=1")}
    names = {r[0]: r[1] for r in await db._db.execute_fetchall("SELECT id, name FROM api_keys")}

    # 少见网段：72 小时内只有 ≤ 2 把 Key 用过的 /24
    net_keys: dict[str, set[int]] = defaultdict(set)
    for kid, net, label in await db._db.execute_fetchall(
            "SELECT key_id, net_hash, label FROM key_sources WHERE last_seen>=?", (since,)):
        if kid not in staff and not _is_shared(label or ""):
            net_keys[net].add(kid)
    # 时间线与客户端
    rows = await db._db.execute_fetchall(
        "SELECT key_id, ts, status, client, src, "
        + "(" + " OR ".join("detail LIKE ?" for _ in PERSONAL_LIMIT) + ") AS limited "
        "FROM usage_log WHERE ts>=? AND key_id IS NOT NULL AND kind LIKE 'image%' ORDER BY ts",
        (*PERSONAL_LIMIT, since))
    rows = [r for r in rows if r[0] not in staff]
    client_keys: dict[str, set[int]] = defaultdict(set)
    for kid, _ts, _st, client, _src, _lim in rows:
        if client:
            client_keys[client].add(kid)

    pairs: dict[tuple[int, int], dict[str, Any]] = defaultdict(lambda: {"signals": {}, "score": 0})

    def add(a: int, b: int, name: str, value) -> None:
        key = (a, b) if a < b else (b, a)
        pairs[key]["signals"][name] = value

    for kids in net_keys.values():
        if len(kids) == 2:
            a, b = sorted(kids)
            add(a, b, "rare_net", True)
    for kids in client_keys.values():
        if len(kids) == 2:
            a, b = sorted(kids)
            add(a, b, "rare_client", True)

    # 粗网段（/16）指纹：非公共出口、且只有 ≤ SRC_RARE 把 Key 用过
    src_keys: dict[str, set[int]] = defaultdict(set)
    for kid, _ts, _st, _c, src, _l in rows:
        if src and not _is_shared(src):
            src_keys[src].add(kid)
    for kids in src_keys.values():
        if 2 <= len(kids) <= SRC_RARE:
            ks = sorted(kids)
            for i, a in enumerate(ks):
                for b in ks[i + 1:]:
                    add(a, b, "same_src", True)

    # 出图参数习惯特征（req_features）：少见参数签名（≤ 3 把 Key）视为强特征
    sig_keys: dict[str, set[int]] = defaultdict(set)
    try:
        feat_rows = await db._db.execute_fetchall(
            "SELECT key_id, sig FROM req_features WHERE ts>=? AND key_id IS NOT NULL", (since,))
        feat_rows = [r for r in feat_rows if r[0] not in staff]
        for kid, sig_val in feat_rows:
            if sig_val:
                sig_keys[sig_val].add(kid)
        for kids in sig_keys.values():
            if 2 <= len(kids) <= SRC_RARE:
                ks = sorted(kids)
                for i, a in enumerate(ks):
                    for b in ks[i + 1:]:
                        add(a, b, "same_sig", True)
    except Exception:
        pass

    candidates = set(pairs)            # 只有共用指纹的 Key 对才看行为

    # 行为：handoff（按「撞上限」次数，不按拒绝条数）、交替、同网段同时出图
    by_key: dict[int, list[float]] = defaultdict(list)
    for kid, ts, *_ in rows:
        by_key[kid].append(ts)
    episodes: dict[int, list[float]] = defaultdict(list)
    for kid, ts, status, _c, _s, limited in rows:
        if status == "rejected" and limited and (not episodes[kid] or ts - episodes[kid][-1] > EPISODE_GAP):
            episodes[kid].append(ts)
    handoff: dict[tuple[int, int], int] = defaultdict(int)
    for a, b in candidates:
        for x, y in ((a, b), (b, a)):
            for ts in episodes.get(x, []):
                stamps = by_key.get(y, [])
                nxt = next((t for t in stamps if ts < t <= ts + HANDOFF_GAP), None)
                if nxt is not None and not any(nxt - HANDOFF_QUIET <= t < nxt for t in stamps):
                    handoff[(a, b)] += 1
    for key, n in handoff.items():
        add(*key, "handoff", n)

    # 两两时序交替：只聚焦候选对自身的时间线，避免被全局并发第三人冲断相邻性
    switch: dict[tuple[int, int], int] = defaultdict(int)
    for a, b in candidates:
        ab = [(kid, ts) for kid, ts, *_ in rows if kid in (a, b)]
        for (k1, t1), (k2, t2) in zip(ab, ab[1:]):
            if k1 != k2 and t2 - t1 <= SWITCH_GAP:
                switch[(a, b)] += 1

    concurrent: dict[tuple[int, int], int] = defaultdict(int)
    minute: dict[tuple[int, str], set[int]] = defaultdict(set)
    for kid, ts, status, _c, src, _l in rows:
        if status == "ok" and src and not _is_shared(src):
            minute[(int(ts // 60), src)].add(kid)
    for kids in minute.values():
        ks = sorted(kids)
        for i, a in enumerate(ks):
            for b in ks[i + 1:]:
                if (a, b) in candidates:
                    concurrent[(a, b)] += 1

    out = []
    for key in candidates:
        a, b = key
        sig = dict(pairs[key]["signals"])
        if switch.get(key, 0) >= 2:
            sig["switch"] = switch[key]
        if concurrent.get(key, 0) >= 1:
            sig["concurrent"] = concurrent[key]
        score = (min(50, 25 * sig.get("handoff", 0))
                 + (20 if sig.get("switch", 0) >= 4 else (15 if sig.get("switch", 0) >= 2 else 0))
                 + (20 if sig.get("concurrent", 0) >= 2 else (10 if sig.get("concurrent", 0) >= 1 else 0))
                 + (30 if sig.get("rare_net") else 0)
                 + (25 if sig.get("same_sig") else 0)
                 + (15 if sig.get("rare_client") else 0)
                 + (10 if sig.get("same_src") else 0))
        behavioral = any(s in sig for s in ("handoff", "switch", "concurrent"))
        if score >= FLAG_SCORE and behavioral:
            out.append({"keys": [a, b], "names": [names.get(a, f"#{a}"), names.get(b, f"#{b}")],
                        "score": score, "signals": sig})
    return sorted(out, key=lambda p: -p["score"])


def describe(signals: dict[str, Any]) -> str:
    parts = []
    if signals.get("handoff"):
        parts.append(f"用完一把马上换另一把 {signals['handoff']} 次")
    if signals.get("switch"):
        parts.append(f"来回交替 {signals['switch']} 次")
    if signals.get("concurrent"):
        parts.append(f"同网段同时出图 {signals['concurrent']} 次")
    if signals.get("same_sig"):
        parts.append("出图习惯完全相同")
    if signals.get("rare_net"):
        parts.append("少见网段相同")
    if signals.get("rare_client"):
        parts.append("少见客户端相同")
    if signals.get("same_src"):
        parts.append("同一个少见出口")
    return "、".join(parts)
