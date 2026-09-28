"""Deterministic /register Discord interaction; deliberately bypasses the LLM."""
from __future__ import annotations

import os

import httpx

COMMAND_GUILD = 100000000000000001


async def handle_register(interaction):
    # This is the sole untrusted-user exception to Hermes's normal allowlist.
    if getattr(interaction, "guild_id", None) != COMMAND_GUILD or not getattr(
        getattr(interaction, "user", None), "id", None
    ):
        await interaction.response.send_message("请在指定服务器使用 /register。", ephemeral=True)
        return
    secret = os.getenv("REGISTRATION_BRIDGE_SECRET", "")
    if not secret:
        await interaction.response.send_message("自助注册尚未配置完成。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post("http://127.0.0.1:3003/self-register/intent",
                headers={"Authorization": "Bearer " + secret},
                json={"discord_id": str(interaction.user.id), "guild_id": str(interaction.guild_id)})
        if response.status_code == 200:
            message = "点击以下链接授权核验身份组（10 分钟内有效）：\n" + response.json()["url"]
        elif response.status_code == 403:
            message = response.json().get("detail", "未通过资格检查。")
        else:
            message = "自助注册暂不可用，请稍后再试。"
    except (httpx.HTTPError, ValueError, KeyError):
        message = "自助注册暂不可用，请稍后再试。"
    await interaction.followup.send(message, ephemeral=True)
