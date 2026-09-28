"""Discord OAuth self-enrollment for restricted NAI Gate keys (no image requests)."""
from __future__ import annotations

import asyncio
import os
import secrets
import time
from urllib.parse import urlencode

import httpx

from .policy import gen_key

COMMAND_GUILD = "100000000000000001"
MEMBERSHIP_GUILD = "100000000000000002"
MEMBERSHIP_ROLE = "100000000000000003"
SITE_URL = "https://gate.example.com/"


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

    async def _discord(self, method: str, path: str, *, bearer: str, **kwargs) -> dict:
        response = await self.http.request(method, "https://discord.com/api" + path,
                                           headers={"Authorization": bearer}, timeout=12, **kwargs)
        if response.status_code >= 400:
            raise RegistrationError("Discord 身份核验或私信失败，请检查授权和私信设置后重试。")
        return response.json()

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
                    "daily_images": 100, "daily_v5": 50, "daily_anlas": 0,
                    "monthly_anlas": 0, "daily_text_tokens": 0, "rpm": 5,
                    "allow_anlas": False, "allow_img2img": False,
                    "exclude_global_v5": False, "image_model_scope": "all",
                    "expires_at": None,
                })
                await self.db._db.execute(
                    "INSERT INTO discord_registrations(discord_id,key_id,created_at) VALUES (?,?,?)",
                    (expected_id, row["id"], time.time()))
                await self.db._db.commit()
                try:
                    await self._discord("POST", f"/channels/{channel['id']}/messages",
                        bearer="Bot " + self.bot_token,
                        json={"content": f"你的 NAI Gate API Key：`{key}`\n网址：{SITE_URL}\n每日额度：V5 50 张；V4.5 及以下 100 张。请勿公开分享此 Key。",
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
        redirect_uri=SITE_URL + "self-register/callback")
