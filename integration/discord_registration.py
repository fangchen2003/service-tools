"""Deterministic /register Discord interaction; deliberately bypasses the LLM."""
from __future__ import annotations

import os

import httpx

def command_guild() -> int | None:
    value = os.getenv("REGISTRATION_COMMAND_GUILD", "").strip()
    return int(value) if value.isascii() and value.isdecimal() and int(value) > 0 else None


def gate_url(path: str) -> str:
    return os.getenv("REGISTRATION_GATE_URL", "http://127.0.0.1:8000").rstrip("/") + path


async def handle_register(interaction):
    # This is the sole untrusted-user exception to Hermes's normal allowlist.
    guild = command_guild()
    if guild is None or getattr(interaction, "guild_id", None) != guild or not getattr(
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
            response = await client.post(gate_url("/self-register/intent"),
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


async def handle_charge(interaction):
    guild = command_guild()
    if guild is None or getattr(interaction, "guild_id", None) != guild or not getattr(
        getattr(interaction, "user", None), "id", None
    ):
        await interaction.response.send_message("请在指定服务器使用 /charge。", ephemeral=True)
        return
    secret = os.getenv("REGISTRATION_BRIDGE_SECRET", "")
    if not secret:
        await interaction.response.send_message("额度重置暂不可用。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(gate_url("/self-register/charge"),
                headers={"Authorization": "Bearer " + secret},
                json={"discord_id": str(interaction.user.id), "guild_id": str(interaction.guild_id)})
        if response.status_code == 200:
            status = response.json()["status"]
            if status == "charged":
                message = "今日个人生图额度已重置：V5 和 V4.5 及以下各恢复一轮。全站额度不变。"
            elif status == "already":
                message = "你今天已经重置过生图额度了，请在北京时间明天 00:00 后再试。"
            else:
                message = "额度重置暂不可用，请稍后再试。"
        elif response.status_code == 403:
            message = response.json().get("detail", "当前账号无法重置额度。")
        else:
            message = "额度重置暂不可用，请稍后再试。"
    except (httpx.HTTPError, ValueError, KeyError):
        message = "额度重置暂不可用，请稍后再试。"
    await interaction.followup.send(message, ephemeral=True)


async def handle_find(interaction):
    guild = command_guild()
    if guild is None or getattr(interaction, "guild_id", None) != guild or not getattr(
        getattr(interaction, "user", None), "id", None
    ):
        await interaction.response.send_message("请在指定服务器使用 /find。", ephemeral=True)
        return
    secret = os.getenv("REGISTRATION_BRIDGE_SECRET", "")
    if not secret:
        await interaction.response.send_message("Key 找回暂不可用。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(gate_url("/self-register/find"),
                headers={"Authorization": "Bearer " + secret},
                json={"discord_id": str(interaction.user.id), "guild_id": str(interaction.guild_id)})
        if response.status_code == 200:
            status = response.json()["status"]
            if status == "restored":
                message = "原 Key 已恢复，并重新发送到你的 Discord 私信。"
            elif status == "active":
                message = "你的 Key 仍然有效，已重新发送到你的 Discord 私信。"
            else:
                message = "Key 找回暂不可用，请稍后再试。"
        elif response.status_code == 403:
            message = response.json().get("detail", "当前账号没有可找回的 Key。")
        else:
            message = "Key 找回暂不可用，请稍后再试。"
    except (httpx.HTTPError, ValueError, KeyError):
        message = "Key 找回暂不可用，请稍后再试。"
    await interaction.followup.send(message, ephemeral=True)


async def handle_vibe(interaction):
    """Claim the caller's daily Vibe allowance without invoking agent tools."""
    guild = command_guild()
    if guild is None or getattr(interaction, "guild_id", None) != guild or not getattr(
        getattr(interaction, "user", None), "id", None
    ):
        await interaction.response.send_message("请在指定服务器使用 /vibe。", ephemeral=True)
        return
    secret = os.getenv("REGISTRATION_BRIDGE_SECRET", "")
    if not secret:
        await interaction.response.send_message("Vibe 领取暂不可用。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    unavailable = "Vibe 领取暂不可用，请稍后再试。"
    message = unavailable
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(
                gate_url("/self-register/vibe"),
                headers={"Authorization": "Bearer " + secret},
                json={"discord_id": str(interaction.user.id), "guild_id": str(interaction.guild_id)},
            )
        if response.status_code == 200:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Invalid Vibe response")
            status, remaining = payload["status"], payload["remaining"]
            if type(remaining) is not int or not 0 <= remaining <= 3:
                raise ValueError("Invalid Vibe remaining count")
            expiry = "北京时间明天 00:00 失效（不累计），届时需重新领取。"
            if status == "claimed" and remaining == 3:
                message = "今日已领取 3 次 Vibe 编码机会。" + expiry
            elif status == "already":
                message = (
                    f"你今天已领取过 Vibe 编码机会，剩余 {remaining} 次；重复领取不会补充。" + expiry
                )
        elif response.status_code == 403:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Invalid Vibe denial")
            detail = payload.get("detail", "请先使用 /register 注册有效 Key。")
            if isinstance(detail, str):
                message = detail
    except (httpx.HTTPError, ValueError, KeyError):
        message = unavailable
    await interaction.followup.send(message, ephemeral=True)
