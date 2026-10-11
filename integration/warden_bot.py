"""燕双鹰：猫头鹰公益站的执法 bot（10-11 站长）。

和奶妹分工：奶妹管领 Key、额度、反馈；燕双鹰只管规矩和处罚告知。
零特权 Intent、不接命令、不发私信；只保持在线，消息由运维脚本经 REST 发到公告频道。"""
from __future__ import annotations

import os

import discord

TOKEN = os.environ["WARDEN_BOT_TOKEN"]


class Warden(discord.Client):
    async def on_ready(self) -> None:
        await self.change_presence(status=discord.Status.online,
                                   activity=discord.Activity(type=discord.ActivityType.watching, name="一人一 Key"))
        print(f"[warden] online as {self.user}", flush=True)


if __name__ == "__main__":
    Warden(intents=discord.Intents.none()).run(TOKEN, log_handler=None)
