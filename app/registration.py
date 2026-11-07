"""Discord OAuth self-enrollment for restricted NAI Gate keys (no image requests)."""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import time
from dataclasses import replace
from datetime import datetime, time as wall_time, timedelta
from zoneinfo import ZoneInfo
from typing import Any
from urllib.parse import urlencode

import httpx
import aiosqlite

from .deployment import DeploymentConfig
from .policy import gen_key
from .config import Settings
from .key_defaults import new_key_defaults

KEY_PATTERN = re.compile(r"nai-[A-Za-z0-9_-]{16,128}")
CLEANUP_TIMEOUT = 10


async def _complete_cleanup(operation):
    """Let a short DB cleanup finish despite repeated caller cancellation.

    The inner timeout remains active under the shield; a broken/locked database
    fails visibly instead of holding request shutdown indefinitely.
    """
    task = asyncio.create_task(asyncio.wait_for(operation, CLEANUP_TIMEOUT))
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def registration_status(db, *, connection=None) -> dict[str, Any]:
    """Durable controls and successful enrollments by site-local calendar day.

    Read the enrollment archive, never api_keys: key reclamation is not a new
    enrollment and must not erase either historical totals or today's usage.
    """
    connection = connection if connection is not None else db._db
    rows = await connection.execute_fetchall(
        "SELECT key,value FROM site_settings WHERE key IN (?,?)",
        ("registration_enabled", "registration_daily_limit"))
    settings = {row["key"]: row["value"] for row in rows}
    enabled = settings.get("registration_enabled", "True").lower() in ("true", "1")
    daily_limit = int(settings.get("registration_daily_limit", "0"))
    zone = ZoneInfo(db.tz)
    today = datetime.fromtimestamp(time.time(), zone).date()
    days = [today - timedelta(days=offset) for offset in range(29, -1, -1)]
    # Calendar midnights, not 86400-second offsets (DST days can be 23/25h).
    boundaries = [datetime.combine(day, wall_time.min, zone).timestamp()
                  for day in days + [today + timedelta(days=1)]]
    columns = ["COUNT(*) AS total_count"]
    params = []
    for index in range(30):
        columns.append(f"COALESCE(SUM(CASE WHEN created_at>=? AND created_at<? THEN 1 ELSE 0 END),0) AS d{index}")
        params.extend(boundaries[index:index + 2])
    row = (await connection.execute_fetchall(
        "SELECT " + ",".join(columns) + " FROM discord_registrations", params))[0]
    history = [{"day": day.isoformat(), "count": row[f"d{index}"]}
               for index, day in enumerate(days)]
    return {"enabled": enabled, "daily_limit": daily_limit,
            "today_count": history[-1]["count"], "total_count": row["total_count"],
            "day": today.isoformat(), "timezone": db.tz, "history": history}


class RegistrationError(Exception):
    pass


class RegistrationService:
    def __init__(self, db, http: httpx.AsyncClient, *, client_id: str, client_secret: str,
                 bot_token: str, bridge_secret: str, redirect_uri: str | None = None,
                 deployment: DeploymentConfig | None = None, settings=None):
        self.db, self.http = db, http
        self.client_id, self.client_secret = client_id, client_secret
        self.bot_token, self.bridge_secret = bot_token, bridge_secret
        self.deployment = deployment if deployment is not None else DeploymentConfig.from_env()
        if redirect_uri is not None:
            self.deployment = replace(self.deployment, redirect_uri=redirect_uri)
        self.redirect_uri = self.deployment.redirect_uri
        self.command_guild = self.deployment.command_guild
        self.settings = settings if settings is not None else Settings()
        self.pending: dict[str, tuple[str, float]] = {}
        self.lock = asyncio.Lock()

    def _require_configured(self):
        if not self.deployment.registration_ready or not all((
                self.client_id, self.client_secret, self.bot_token, self.bridge_secret)):
            raise RegistrationError("自助注册尚未配置。")

    async def _ensure_allowed(self, *, connection=None) -> None:
        status = await registration_status(self.db, connection=connection)
        if not status["enabled"]:
            raise RegistrationError("注册领取已暂停，请稍后再试。")
        if status["daily_limit"] and status["today_count"] >= status["daily_limit"]:
            raise RegistrationError("今日注册领取名额已满，请明日再试。")

    async def begin(self, user_id: str, guild_id: str) -> str:
        self._require_configured()
        if not self.command_guild or guild_id != self.command_guild or not user_id.isdecimal():
            raise RegistrationError("请在指定服务器使用 /register。")
        await self._ensure_allowed()
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

    def _key_message(self, key: str, *, restored: bool = False, key_policy=None, timezone="Asia/Shanghai") -> str:
        policy = dict(key_policy) if key_policy is not None else new_key_defaults(self.settings)
        quota = lambda name: str(policy[name]) if policy[name] > 0 else "不限"
        legacy_only = policy["image_model_scope"] != "all"
        models = "仅 V4.5 及以下" if legacy_only else "V5、V4.5 及以下"
        limits = ("" if legacy_only else f"V5 High {quota('daily_v5')} 张；") + f"V4.5 及以下 {quota('daily_images')} 张"
        paid = ("已开通 Anlas：允许付费规格及官方 V5 免费额度耗尽后的付费生成；Anlas 不设日/月预算。"
                if policy["allow_anlas"] else
                "未开通 Anlas，仅支持免费规格；超尺寸、步数、多张及 SMEA 自动调整，V5 耗尽不回退付费。付费权限仅由管理员单独开启。")
        expiry = (datetime.fromtimestamp(policy["expires_at"], ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M") + f"（{timezone}）"
                  if policy.get("expires_at") else "不设到期时间")
        prefix = "你的原 NAI Gate API Key 已恢复：" if restored else "你的 NAI Gate API Key："
        return (f"{prefix}`{key}`\n网址：{self.deployment.site_url}\n仿 NAI 官方的工作台：{self.deployment.workbench_url}\n"
                f"模型权限：{models}。\n每日额度：{limits}。\n此 Key {paid}\n"
                f"图生图权限：{'已开启（仍受全站开关限制）' if policy['allow_img2img'] else '未开启'}；每分钟请求上限：{policy['rpm']}；有效期：{expiry}。\n"
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
        self._require_configured()
        if not self.command_guild or guild_id != self.command_guild or not user_id.isdecimal():
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
            # Shield the short restore transaction so cancellation cannot lose
            # its committed key id before compensation knows what to undo.
            restore = asyncio.create_task(self.db.restore_discord_key(
                user_id, str(key), defaults=new_key_defaults(self.settings)))
            try:
                status, key_id, old_key_id = await asyncio.shield(restore)
                if status == "not_registered":
                    raise RegistrationError("这个 Discord 账号尚未领取过 Key，请使用 /register。")
                current = await self.db.get_key(key_id)
                if current is None:
                    raise RegistrationError("Key 状态已变化，请重新使用 /find。")
                await self._discord("POST", f"/channels/{channel['id']}/messages",
                    bearer="Bot " + self.bot_token,
                    json={"content": self._key_message(str(current['token']), restored=status == "restored", key_policy=current, timezone=self.db.tz),
                          "allowed_mentions": {"parse": []}})
            except BaseException:
                await _complete_cleanup(self._rollback_restore(user_id, restore))
                raise
            return status

    async def _rollback_restore(self, user_id, restore):
        try:
            status, key_id, old_key_id = await restore
        except Exception:
            return  # The isolated restore transaction already rolled back.
        if status == "restored":
            await self.db.rollback_discord_key_restore(user_id, key_id, old_key_id)

    async def finish(self, code: str, state: str) -> str:
        self._require_configured()
        pending = self.pending.pop(state, None)
        if not pending or pending[1] <= time.time() or not code:
            raise RegistrationError("授权链接无效或已过期，请重新使用 /register。")
        expected_id = pending[0]
        # The state is one-use; no OAuth token is persisted.
        async with self.lock:
            await self._ensure_allowed()
            if (await self.db._db.execute_fetchall(
                "SELECT 1 FROM discord_registrations WHERE discord_id=?", (expected_id,)
            )):
                raise RegistrationError("这个 Discord 账号已经领取过 Key。")
        # OAuth/Discord membership checks do not hold issuance's lock: an admin
        # can pause while Discord is slow. Pending authorizations reserve nothing.
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
            member = await self._discord("GET", f"/users/@me/guilds/{self.deployment.membership_guild}/member",
                                         bearer="Bearer " + token)
            if self.deployment.membership_role not in member.get("roles", []):
                raise RegistrationError("未检测到指定身份组，无法领取 Key。")
            async with self.lock:
                await self._ensure_allowed()
                if (await self.db._db.execute_fetchall(
                    "SELECT 1 FROM discord_registrations WHERE discord_id=?", (expected_id,)
                )):
                    raise RegistrationError("这个 Discord 账号已经领取过 Key。")
                channel = await self._discord("POST", "/users/@me/channels",
                    bearer="Bot " + self.bot_token, json={"recipient_id": expected_id})
                key = gen_key("nai")
                try:
                    # A short isolated writer transaction serializes admission with
                    # every instance and the admin policy PUT. No Discord HTTP or
                    # shared image-accounting connection runs inside it.
                    async with self.db._open_connection() as conn:
                        conn.row_factory = aiosqlite.Row
                        try:
                            await conn.execute("BEGIN IMMEDIATE")
                            await self._ensure_allowed(connection=conn)
                            if await conn.execute_fetchall(
                                "SELECT 1 FROM discord_registrations WHERE discord_id=?", (expected_id,)
                            ):
                                raise RegistrationError("这个 Discord 账号已经领取过 Key。")
                            row = await self.db._insert_key(conn, {
                                "name": "Discord:" + expected_id, "token": key,
                                **new_key_defaults(self.settings),
                            })
                            await conn.execute(
                                "INSERT INTO discord_registrations(discord_id,key_id,token,created_at) VALUES (?,?,?,?)",
                                (expected_id, row["id"], key, time.time()))
                            await conn.commit()
                        except BaseException:
                            await conn.rollback()
                            raise
                    await self._discord("POST", f"/channels/{channel['id']}/messages",
                        bearer="Bot " + self.bot_token,
                        json={"content": self._key_message(key, key_policy=row, timezone=self.db.tz),
                              "allowed_mentions": {"parse": []}})
                except BaseException:
                    # Also covers an ambiguous commit acknowledgement: cleanup is
                    # scoped to this operation's credential, never another winner.
                    await _complete_cleanup(self.db.rollback_discord_registration(expected_id, key))
                    raise
                return "sent"
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise RegistrationError("Discord 服务暂时不可用，请稍后重试。") from exc


def configured_service(db, http: httpx.AsyncClient, *, settings=None) -> RegistrationService | None:
    names = ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_BOT_TOKEN", "REGISTRATION_BRIDGE_SECRET")
    deployment = DeploymentConfig.from_env()
    if not deployment.registration_ready or not all(os.getenv(name) for name in names):
        return None
    return RegistrationService(db, http, client_id=os.environ[names[0]], client_secret=os.environ[names[1]],
        bot_token=os.environ[names[2]], bridge_secret=os.environ[names[3]],
        deployment=deployment, settings=settings)
