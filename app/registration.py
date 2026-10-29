"""Discord OAuth self-enrollment for restricted NAI Gate keys (no image requests)."""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from .policy import gen_key

COMMAND_GUILD = "100000000000000001"
MEMBERSHIP_GUILD = "100000000000000002"
MEMBERSHIP_ROLE = "100000000000000003"
SITE_URL = "https://gate.example.com/"
# Keep the Discord-registered callback during the parallel-domain migration.
LEGACY_OAUTH_REDIRECT_URI = "https://gate.example.com/self-register/callback"
WORKBENCH_URL = "https://gate.example.com/workbench/"
KEY_PATTERN = re.compile(r"nai-[A-Za-z0-9_-]{16,128}")


class RegistrationError(Exception):
    pass


class RegistrationService:
    def __init__(self, db, http: httpx.AsyncClient, *, client_id: str, client_secret: str,
                 bot_token: str, bridge_secret: str, redirect_uri: str):
        self.db, self.http = db, http
        self.client_id, self.client_secret = client_id, client_secret
        self.bot_token, self.bridge_secret = bot_token, bridge_secret
        self.redirect_uri = redirect_uri
        self.pending: dict[str, tuple[str, float]] = {}
        self.lock = asyncio.Lock()

    async def begin(self, user_id: str, guild_id: str) -> str:
        if guild_id != COMMAND_GUILD or not user_id.isdecimal():
            raise RegistrationError("请在指定服务器使用 /register。")
        if (await self.db._db.execute_fetchall(
            "SELECT 1 FROM discord_registrations WHERE discord_id=?", (user_id,)
        )):
            raise RegistrationError("这个 Discord 账号已经领取过 Key。")
        self.pending = {k: v for k, v in self.pending.items() if v[1] > time.time()}
        if sum(u == user_id for u, _ in self.pending.values()) >= 2:
            raise RegistrationError("授权链接已发送，请先完成授权或稍后重试。")
        state = secrets.token_urlsafe(32)
        self.pending[state] = (user_id, time.time() + 600)
        return "https://discord.com/oauth2/authorize?" + urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri,
            "response_type": "code", "scope": "identify guilds.members.read", "state": state,
        })

    async def _discord(self, method: str, path: str, *, bearer: str, **kwargs) -> Any:
        response = await self.http.request(method, "https://discord.com/api" + path,
                                           headers={"Authorization": bearer}, timeout=12, **kwargs)
        if response.status_code >= 400:
            raise RegistrationError("Discord 身份核验或私信失败，请检查授权和私信设置后重试。")
        return response.json()

    @staticmethod
    def _key_message(key: str, *, restored: bool = False) -> str:
        prefix = "你的原 NAI Gate API Key 已恢复：" if restored else "你的 NAI Gate API Key："
        return (f"{prefix}`{key}`\n网址：{SITE_URL}\n仿 NAI 官方的工作台：{WORKBENCH_URL}\n"
                "每日额度：V5 High 30 张；V4.5 及以下 100 张。\n"
                "普通生图仅支持免费规格，超尺寸、步数、多张及 SMEA 会自动调整；不能安全转为免费的功能拒绝，V5 耗尽不会回退付费。\n"
                "使用 `/vibe` 上传参考图并编码为可导入工作台的 Vibe 文件；每天最多成功编码 3 次，北京时间零点恢复。已编码 Vibe 可重复使用，V4/V4.5 每次生图最多使用 4 个。\n"
                "额度用完后，可在命令服务器使用 `/charge`，每天可使用一次，重置个人 V5 和 V4.5 及以下额度各一轮（不影响全站额度）。\n"
                "若 Key 因连续三天未使用被回收，可在命令服务器使用 `/find` 找回原 Key。\n"
                "请勿公开分享此 Key。")

    async def _find_key_in_dm(self, channel_id: str) -> str | None:
        bot = await self._discord("GET", "/users/@me", bearer="Bot " + self.bot_token)
        before = ""
        for _ in range(5):
            params: dict[str, Any] = {"limit": 100}
            if before:
                params["before"] = before
            messages = await self._discord("GET", f"/channels/{channel_id}/messages",
                                           bearer="Bot " + self.bot_token, params=params)
            if not isinstance(messages, list) or not messages:
                return None
            for message in messages:
                if str(message.get("author", {}).get("id")) != str(bot.get("id")):
                    continue
                content = str(message.get("content", ""))
                match = re.search(r"你的 NAI Gate API Key：`(nai-[A-Za-z0-9_-]{16,128})`", content)
                if match:
                    return match.group(1)
            before = str(messages[-1].get("id", ""))
            if not before or len(messages) < 100:
                return None
        return None

    async def find(self, user_id: str, guild_id: str) -> str:
        if guild_id != COMMAND_GUILD or not user_id.isdecimal():
            raise RegistrationError("请在指定服务器使用 /find。")
        async with self.lock:
            registration = await self.db.get_discord_registration(user_id)
            if registration is None:
                raise RegistrationError("这个 Discord 账号尚未领取过 Key，请使用 /register。")
            channel = await self._discord("POST", "/users/@me/channels",
                bearer="Bot " + self.bot_token, json={"recipient_id": user_id})
            key = registration["active_token"] or registration["token"]
            if not key:
                key = await self._find_key_in_dm(channel["id"])
            if not key or KEY_PATTERN.fullmatch(str(key)) is None:
                raise RegistrationError("未能找到原 Key，请联系管理员处理。")
            status, key_id, old_key_id = await self.db.restore_discord_key(user_id, str(key))
            if status == "not_registered":
                raise RegistrationError("这个 Discord 账号尚未领取过 Key，请使用 /register。")
            try:
                await self._discord("POST", f"/channels/{channel['id']}/messages",
                    bearer="Bot " + self.bot_token,
                    json={"content": self._key_message(str(key), restored=status == "restored"),
                          "allowed_mentions": {"parse": []}})
            except Exception:
                if status == "restored":
                    await self.db.rollback_discord_key_restore(user_id, key_id, old_key_id)
                raise
            return status

    async def finish(self, code: str, state: str) -> str:
        pending = self.pending.pop(state, None)
        if not pending or pending[1] <= time.time() or not code:
            raise RegistrationError("授权链接无效或已过期，请重新使用 /register。")
        expected_id = pending[0]
        # The state is one-use; no OAuth token is persisted.
        async with self.lock:
            if (await self.db._db.execute_fetchall(
                "SELECT 1 FROM discord_registrations WHERE discord_id=?", (expected_id,)
            )):
                raise RegistrationError("这个 Discord 账号已经领取过 Key。")
            try:
                response = await self.http.post("https://discord.com/api/oauth2/token", data={
                    "client_id": self.client_id, "client_secret": self.client_secret,
                    "grant_type": "authorization_code", "code": code,
                    "redirect_uri": self.redirect_uri,
                }, timeout=12)
                if response.status_code != 200:
                    raise RegistrationError("Discord 授权失败，请重新使用 /register。")
                token = response.json()["access_token"]
                user = await self._discord("GET", "/users/@me", bearer="Bearer " + token)
                if str(user.get("id")) != expected_id:
                    raise RegistrationError("授权的 Discord 账号与命令发起者不一致。")
                member = await self._discord("GET", f"/users/@me/guilds/{MEMBERSHIP_GUILD}/member",
                                             bearer="Bearer " + token)
                if MEMBERSHIP_ROLE not in member.get("roles", []):
                    raise RegistrationError("未检测到指定身份组，无法领取 Key。")
                channel = await self._discord("POST", "/users/@me/channels",
                    bearer="Bot " + self.bot_token, json={"recipient_id": expected_id})
                key = gen_key("nai")
                row = await self.db.create_key({
                    "name": "Discord:" + expected_id, "token": key,
                    "daily_images": 100, "daily_v5": 30, "daily_anlas": 0,
                    "monthly_anlas": 0, "daily_text_tokens": 0, "rpm": 5,
                    "allow_anlas": False, "allow_img2img": False,
                    "exclude_global_v5": False, "image_model_scope": "all",
                    "expires_at": None,
                })
                await self.db._db.execute(
                    "INSERT INTO discord_registrations(discord_id,key_id,token,created_at) VALUES (?,?,?,?)",
                    (expected_id, row["id"], key, time.time()))
                await self.db._db.commit()
                try:
                    await self._discord("POST", f"/channels/{channel['id']}/messages",
                        bearer="Bot " + self.bot_token,
                        json={"content": self._key_message(key),
                              "allowed_mentions": {"parse": []}})
                except Exception:
                    await self.db._db.execute("DELETE FROM discord_registrations WHERE discord_id=?", (expected_id,))
                    await self.db.delete_key(row["id"])
                    raise
                return "sent"
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                raise RegistrationError("Discord 服务暂时不可用，请稍后重试。") from exc


def configured_service(db, http: httpx.AsyncClient) -> RegistrationService | None:
    names = ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_BOT_TOKEN", "REGISTRATION_BRIDGE_SECRET")
    if not all(os.getenv(name) for name in names):
        return None
    return RegistrationService(db, http, client_id=os.environ[names[0]], client_secret=os.environ[names[1]],
        bot_token=os.environ[names[2]], bridge_secret=os.environ[names[3]],
        redirect_uri=os.getenv("DISCORD_REDIRECT_URI", LEGACY_OAUTH_REDIRECT_URI))
