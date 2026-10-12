"""Key 来源网段统计：用于发现一把 Key 被多人分享。

只记录请求来源所在的网段（IPv4 /24、IPv6 /48）的加盐哈希和一个打码标签（如 120.235.*.*），
不保存完整 IP；7 天后自动删除。同一 Key 24 小时内出现的不同标签（IPv4 /16）数达到阈值时私信提醒站长；
按 /16 计数是因为 WARP、手机运营商会在同一网络内频繁更换 /24，否则一个人也会误报。
家庭 / 公司 / 手机流量 / 代理切换也会产生不同网段，所以这只是线索，不用于自动封禁。
"""
from __future__ import annotations

import hashlib
import ipaddress
import secrets
import time
from collections import deque
from typing import Optional

WINDOW_SECONDS = 24 * 3600
RETENTION_SECONDS = 7 * 24 * 3600
WRITE_INTERVAL = 300          # 同一 Key + 网段 5 分钟内只写一次库
ALERT_COOLDOWN = 6 * 3600
SALT_SETTING = "key_source_salt"
ALTERNATE_WINDOW = 600     # 10 分钟内 A→B→A 式来回切换 ≥2 次
CLIENT_ALERT = 3           # 24 小时内 ≥3 种客户端名称
ACTIVE_HOURS_ALERT = 20    # 近 24 小时有 ≥20 个小时在用


def _is_test(key) -> bool:
    try:
        return bool(key["is_test"])
    except (KeyError, IndexError, TypeError):
        return False


def network_of(ip: str) -> Optional[tuple[str, str]]:
    """返回 (网段, 打码标签)；无法解析的地址（如测试客户端）返回 None。"""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if addr.version == 4:
        net = ipaddress.ip_network(f"{addr}/24", strict=False)
        a, b = str(addr).split(".")[:2]
        return str(net), f"{a}.{b}.*.*"
    net = ipaddress.ip_network(f"{addr}/48", strict=False)
    head = addr.exploded.split(":")[:2]
    return str(net), ":".join(head) + ":…"


class SourceTracker:
    def __init__(self, db, alerter=None, *, threshold: int = 3):
        self.db, self.alerter, self.threshold = db, alerter, max(0, int(threshold))
        self._salt: Optional[str] = None
        self._recent: dict[tuple[int, str], float] = {}
        # 防转卖信号（只在内存里，重启清空）：网段切换序列、客户端名称、活跃小时
        self._trail: dict[int, deque] = {}
        self._clients: dict[int, dict[str, float]] = {}
        self._hours: dict[int, set] = {}
        self.events: deque = deque(maxlen=2000)   # (时间, key_id, 信号类型)：给自动驾驶 key_guard 判断用
        # 私信站长的网段告警：2026-10-10 起关闭（只看网段会误伤 VPN 用户，提醒和处罚交给 share_guard.py）。
        # 代码保留：防分享校准后可重新打开，作为「单 Key 24h 网段数」的廉价告警。
        self.notify_owner = False

    async def _get_salt(self) -> str:
        if self._salt is None:
            salt = await self.db.get_setting(SALT_SETTING, None)
            if not salt:
                salt = secrets.token_hex(16)
                await self.db.set_setting(SALT_SETTING, salt)
            self._salt = str(salt)
        return self._salt

    def signals(self, key, label: Optional[str], client: str, now: float) -> list[tuple[str, str]]:
        """返回 (告警类型, 说明)：网段交替、客户端名称过多、近 24 小时几乎全天在用。都只是线索。"""
        kid, out = int(key["id"]), []
        if label:
            trail = self._trail.setdefault(kid, deque(maxlen=20))
            if not trail or trail[-1][1] != label:
                trail.append((now, label))
            recent = [lb for t, lb in trail if t >= now - ALTERNATE_WINDOW]
            back_and_forth = sum(1 for i in range(2, len(recent)) if recent[i] == recent[i - 2] != recent[i - 1])
            if back_and_forth >= 2:
                out.append(("alternate", f"10 分钟内在 {len(set(recent))} 个网段之间来回切换（{'→'.join(recent[-5:])}），像是多人同时在用"))
        name = (client or "").strip()[:60]
        if name:
            seen = self._clients.setdefault(kid, {})
            seen[name] = now
            for n in [n for n, t in seen.items() if t < now - WINDOW_SECONDS]:
                seen.pop(n, None)
            if len(seen) >= CLIENT_ALERT:
                out.append(("clients", f"24 小时内用了 {len(seen)} 种不同的客户端"))
        hours = self._hours.setdefault(kid, set())
        hours.add(int(now // 3600))
        for h in [h for h in hours if h < int(now // 3600) - 23]:
            hours.discard(h)
        if len(hours) >= ACTIVE_HOURS_ALERT:
            out.append(("allday", f"近 24 小时里有 {len(hours)} 个小时都在用，作息不像一个人"))
        return out

    async def observe(self, key, ip: str, now: Optional[float] = None, client: str = "") -> None:
        """记录一次成功鉴权的来源网段。管理员 Key 不记录。"""
        if key["is_admin"]:
            return
        # 内部来源 / 官方工作台豁免：不记录私网地址，不污染成员网段
        try:
            addr = ipaddress.ip_address((ip or "").strip())
            if addr.is_private or addr.is_loopback:
                return
        except ValueError:
            pass
        if "workbench" in (client or "").lower():
            return
        now = time.time() if now is None else now
        found = network_of(ip)
        if not _is_test(key):
            # 信号总要记下来喂给自动驾驶——不能因为没配告警渠道（alerter 为空）就让 key_guard 收不到事件
            for kind, text in self.signals(key, found[1] if found else None, client, now):
                self.events.append((now, int(key["id"]), kind))
                if not (self.notify_owner and self.alerter is not None):
                    continue
                self.alerter.notify(f"resale_{kind}_{key['id']}",
                                    f"Key「{key['name']}」{text}。可能被转卖或共享，建议先问一下本人。", cooldown=ALERT_COOLDOWN)
        if found is None:
            return
        net, label = found
        digest = hashlib.sha256(((await self._get_salt()) + net).encode()).hexdigest()[:16]
        slot = (int(key["id"]), digest)
        if now - self._recent.get(slot, 0) < WRITE_INTERVAL:
            return
        self._recent[slot] = now
        if len(self._recent) > 4096:
            cutoff = now - WRITE_INTERVAL
            self._recent = {k: t for k, t in self._recent.items() if t >= cutoff}
        is_new = await self.db.touch_key_source(int(key["id"]), digest, label, now, now - WINDOW_SECONDS)
        if not is_new or not self.threshold or self.alerter is None or not self.notify_owner:
            return
        labels = await self.db.key_source_labels(int(key["id"]), now - WINDOW_SECONDS)
        if len(labels) >= self.threshold:
            self.alerter.notify(
                f"key_sources_{key['id']}",
                f"Key「{key['name']}」24 小时内从 {len(labels)} 个不同网段使用（{'、'.join(labels[:6])}），"
                "可能被分享。也可能只是换了网络 / 代理，建议先问一下本人。",
                cooldown=ALERT_COOLDOWN)
