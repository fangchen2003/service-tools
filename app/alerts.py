"""站长告警：把异常事件推送到 Discord（私信 / 频道 / Webhook 任选）。

只发送事件类型和一句话说明，不包含 Key、提示词或用户内容。同类事件按冷却时间去重，避免刷屏。
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import httpx

DISCORD_API = "https://discord.com/api"

# 只写日志、不私信站长的事件（站长 10/10：「没啥大问题就没必要私信我，这个更像日志」）。
# 重启通知、上游短时波动 / 恢复、耗时变化、每小时上限自动下调都属于这类；监控照样能从日志看到。
QUIET_KINDS = {"startup", "upstream_degraded", "upstream_recovered", "guard_hourly_down"}
QUIET_PREFIXES = ("perf_",)


def is_quiet(kind: str) -> bool:
    return kind in QUIET_KINDS or kind.startswith(QUIET_PREFIXES)


class Alerter:
    def __init__(self, *, bot_token: str = "", user_id: str = "", channel_id: str = "",
                 webhook_url: str = "", site: str = ""):
        self.bot_token, self.user_id = bot_token, user_id.strip()
        self.channel_id, self.webhook_url = channel_id.strip(), webhook_url.strip()
        self.site = site
        self._last: dict[str, float] = {}
        self._dm_channel: Optional[str] = None
        self._tasks: set[asyncio.Task] = set()
        self.sent = 0

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url or (self.bot_token and (self.user_id or self.channel_id)))

    def notify(self, kind: str, message: str, *, cooldown: float = 900) -> None:
        """Fire-and-forget；可从同步或异步代码调用。同一 kind 在冷却期内只发一次。"""
        if is_quiet(kind):
            print(f"[alert-quiet] {kind}: {message}", flush=True)
            return
        if not self.configured:
            return
        now = time.monotonic()
        if now - self._last.get(kind, -1e9) < cooldown:
            return
        self._last[kind] = now
        try:
            task = asyncio.get_running_loop().create_task(self._send(f"⚠ **猫头鹰公益站告警**（{kind}）\n{message}"))
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def post(self, text: str) -> None:
        """发送一条不带告警前缀的普通通知（如给成员的公告）；无冷却。"""
        if not self.configured:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._send(text))
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, text: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                if self.webhook_url:
                    response = await client.post(self.webhook_url, json={"content": text[:1900],
                                                 "allowed_mentions": {"parse": []}})
                else:
                    headers = {"Authorization": "Bot " + self.bot_token}
                    channel = self.channel_id
                    if not channel:
                        if not self._dm_channel:
                            created = await client.post(DISCORD_API + "/users/@me/channels",
                                                        headers=headers, json={"recipient_id": self.user_id})
                            created.raise_for_status()
                            self._dm_channel = created.json()["id"]
                        channel = self._dm_channel
                    response = await client.post(f"{DISCORD_API}/channels/{channel}/messages", headers=headers,
                                                 json={"content": text[:1900], "allowed_mentions": {"parse": []}})
                response.raise_for_status()
                self.sent += 1
        except Exception as exc:  # 告警失败不能影响服务
            print(f"[warn] alert delivery failed: {type(exc).__name__}")


def announcer_from_settings(settings) -> Alerter:
    """成员公告通道：用同一个机器人发到公告频道（ANNOUNCE_CHANNEL_ID）。"""
    return Alerter(bot_token=settings.discord_bot_token, channel_id=settings.announce_channel_id,
                   site=settings.site_url)


def from_settings(settings) -> Alerter:
    return Alerter(bot_token=settings.discord_bot_token, user_id=settings.alert_user_id,
                   channel_id=settings.alert_channel_id, webhook_url=settings.alert_webhook_url,
                   site=settings.site_url)
