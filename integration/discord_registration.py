"""Deterministic /register Discord interaction; deliberately bypasses the LLM."""
from __future__ import annotations

import os

import httpx

COMMAND_GUILD = 1480185480048808009


async def handle_register(interaction, invite_key: str = ""):
    # This is the sole untrusted-user exception to Hermes's normal allowlist.
    guild = int(os.getenv("DISCORD_GUILD_ID") or COMMAND_GUILD)
    if getattr(interaction, "guild_id", None) != guild or not getattr(
        getattr(interaction, "user", None), "id", None
    ):
        await interaction.response.send_message("请在指定服务器使用 /register。", ephemeral=True)
        return
    secret = os.getenv("REGISTRATION_BRIDGE_SECRET", "")
    if not secret:
        await interaction.response.send_message("领 Key 功能尚未配置完成。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    user = interaction.user
    try:
        backend = os.getenv("REGISTRATION_BACKEND_URL", "http://127.0.0.1:3003").rstrip("/")
        # 直发：斜杠命令已由 Discord 验明身份，不再走 OAuth 授权，Key 只在本人可见的临时消息里给。
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(backend + "/self-register/issue",
                headers={"Authorization": "Bearer " + secret},
                json={"discord_id": str(user.id), "guild_id": str(interaction.guild_id),
                      "name": str(getattr(user, "name", "") or "")[:80],
                      "username": str(getattr(user, "name", "") or "")[:80],
                      "global_name": str(getattr(user, "global_name", "") or getattr(user, "display_name", "") or "")[:80],
                      "avatar": str(getattr(getattr(user, "avatar", None), "key", "") or "")[:120],
                      "invite_key": invite_key[:80]})
        if response.status_code == 200:
            message = response.json().get("message") or "领取成功。"
        elif response.status_code == 403:
            message = response.json().get("detail", "未通过资格检查。")
        else:
            message = "领 Key 暂不可用，请稍后再试。"
    except (httpx.HTTPError, ValueError, KeyError):
        message = "领 Key 暂不可用，请稍后再试。"
    await interaction.followup.send(message, ephemeral=True)
