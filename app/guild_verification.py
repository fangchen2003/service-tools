"""Browser-bound Discord role verification; never issues a Key or auto-joins a guild."""
from __future__ import annotations

import base64
from collections import deque
from dataclasses import replace
import hashlib
from html import escape
import os
import re
import secrets
import sqlite3
import time
from urllib.parse import urlencode

import aiosqlite
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .deployment import DeploymentConfig, _https_origin

router = APIRouter(prefix="/self-register")
COOKIE = "__Host-nai-gate-guild-verify"
TTL = 600
MAX_STATES = 10000

HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
           "X-Content-Type-Options": "nosniff",
           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"}


class VerificationError(Exception):
    pass


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _page(message: str, *, success: bool = False, status: int = 403, invite_url: str = ""):
    # Never echo query or Discord data. Invitation is validated deployment config.
    action = (f'<a class="action" href="{escape(invite_url, quote=True)}" rel="noreferrer noopener">加入 Discord 服务器 ↗</a>'
              '<p>加入后使用 <code>/register</code> 领取 Key（以开放状态与每日名额为准）；已有 Key 可使用 <code>/find</code>。</p>'
              if success else '<a class="action" href="/self-register/verify">重新验证</a>')
    html = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Discord 身份验证 · NAI Gate</title><style>'
            'body{margin:0;padding:24px;background:#f7f6f0;color:#253d34;font:15px/1.8 system-ui,sans-serif;display:grid;min-height:90vh;place-items:center}'
            'main{max-width:520px;padding:32px;border:1px solid #dce2d6;border-radius:12px;background:#fffefa;overflow-wrap:anywhere}'
            'h1{font-size:25px;font-weight:500}p{color:#65766b}.action{display:inline-block;background:#205f50;color:white;padding:12px 20px;border-radius:6px;text-decoration:none}'
            'a:focus-visible{outline:3px solid #b56b50;outline-offset:4px}.home{display:block;color:#205f50;margin-top:20px}</style>'
            f'<main><h1>{"验证通过" if success else "暂未完成验证"}</h1><p>{escape(message)}</p>{action}'
            '<a class="home" href="/">返回首页</a></main></html>')
    return HTMLResponse(html, status_code=status, headers=HEADERS)


class GuildVerificationService:
    def __init__(self, db, http: httpx.AsyncClient, *, client_id: str, client_secret: str,
                 redirect_uri: str | None = None, deployment: DeploymentConfig | None = None):
        self.db, self.http = db, http
        self.client_id, self.client_secret = client_id, client_secret
        self.deployment = deployment if deployment is not None else DeploymentConfig.from_env()
        if redirect_uri is not None:
            self.deployment = replace(self.deployment, redirect_uri=redirect_uri)
        self.redirect_uri = self.deployment.redirect_uri
        self._starts: dict[str, deque[float]] = {}

    def _require_configured(self):
        if not self.deployment.guild_verification_ready or not self.client_id or not self.client_secret:
            raise VerificationError("Discord 身份验证尚未配置。")

    def admit_start(self, client: str) -> bool:
        # Use the ASGI trusted-proxy identity, never raw forwarding headers.
        now = time.monotonic()
        self._starts = {k: v for k, v in self._starts.items() if v and v[-1] > now - TTL}
        key = _digest(client)
        if key not in self._starts and len(self._starts) >= MAX_STATES:
            return False
        recent = self._starts.setdefault(key, deque())
        while recent and recent[0] <= now - TTL:
            recent.popleft()
        if len(recent) >= 60 or sum(ts > now - 60 for ts in recent) >= 10:
            return False
        recent.append(now)
        return True

    async def initialize(self):
        async with aiosqlite.connect(self.db.path, timeout=5) as conn:
            await conn.executescript('''
                CREATE TABLE IF NOT EXISTS guild_verification_states (
                    state_hash TEXT PRIMARY KEY, nonce_hash TEXT NOT NULL,
                    verifier TEXT NOT NULL, expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_guild_verification_expiry ON guild_verification_states(expires_at);
                CREATE INDEX IF NOT EXISTS idx_guild_verification_nonce ON guild_verification_states(nonce_hash);
            ''')
            await conn.commit()

    async def begin(self, previous_nonce: str = ""):
        self._require_configured()
        state = "gv." + secrets.token_urlsafe(32)
        nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        async with aiosqlite.connect(self.db.path, timeout=5) as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute("DELETE FROM guild_verification_states WHERE expires_at<=?", (now,))
            if re.fullmatch(r"[A-Za-z0-9_-]{43}", previous_nonce):
                await conn.execute("DELETE FROM guild_verification_states WHERE nonce_hash=?", (_digest(previous_nonce),))
            count = (await (await conn.execute("SELECT count(*) FROM guild_verification_states")).fetchone())[0]
            if count >= MAX_STATES:
                raise VerificationError("验证请求较多，请稍后重试。")
            await conn.execute("INSERT INTO guild_verification_states VALUES(?,?,?,?)",
                               (_digest(state), _digest(nonce), verifier, now + TTL))
            await conn.commit()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        url = "https://discord.com/oauth2/authorize?" + urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri,
            "response_type": "code", "scope": "identify guilds.members.read", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        response = RedirectResponse(url, status_code=302, headers=HEADERS)
        response.set_cookie(COOKIE, nonce, max_age=TTL, httponly=True, secure=True, samesite="lax", path="/")
        return response

    async def finish(self, state: str, nonce: str, code: str, error: str):
        self._require_configured()
        if not re.fullmatch(r"gv\.[A-Za-z0-9_-]{43}", state) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", nonce):
            raise VerificationError("验证链接无效或已过期，请在原浏览器重新发起验证。")
        async with aiosqlite.connect(self.db.path, timeout=5) as conn:
            await conn.execute("BEGIN IMMEDIATE")
            row = await (await conn.execute(
                "SELECT verifier FROM guild_verification_states WHERE state_hash=? AND nonce_hash=? AND expires_at>?",
                (_digest(state), _digest(nonce), time.time()),
            )).fetchone()
            if row is None:
                raise VerificationError("验证链接无效或已过期，请重新发起验证。")
            await conn.execute("DELETE FROM guild_verification_states WHERE state_hash=?", (_digest(state),))
            await conn.commit()
        if error or not code or len(code) > 2048:
            raise VerificationError("Discord 授权未完成，请重新验证。")
        response = await self.http.post("https://discord.com/api/oauth2/token", data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
            "code_verifier": row[0],
        }, timeout=12)
        if response.status_code != 200:
            raise VerificationError("Discord 授权失败，请重新验证。")
        token = response.json()["access_token"]
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise VerificationError("Discord 授权失败，请重新验证。")
        headers = {"Authorization": "Bearer " + token}
        user = await self.http.get("https://discord.com/api/users/@me", headers=headers, timeout=12)
        if user.status_code != 200 or not re.fullmatch(r"[0-9]{1,32}", str(user.json().get("id", ""))):
            raise VerificationError("Discord 账号核验失败，请重新验证。")
        member = await self.http.get(f"https://discord.com/api/users/@me/guilds/{self.deployment.membership_guild}/member",
                                     headers=headers, timeout=12)
        if member.status_code != 200:
            raise VerificationError("未能核验来源服务器的身份组，请确认已加入来源服务器并稍后重试。")
        roles = member.json().get("roles")
        if not isinstance(roles, list) or self.deployment.membership_role not in roles:
            raise VerificationError("未检测到注册要求的指定身份组，暂不能通过验证。")


def configured_service(db, http):
    deployment = DeploymentConfig.from_env()
    if not deployment.guild_verification_ready or not all(os.getenv(name) for name in ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET")):
        return None
    return GuildVerificationService(db, http, client_id=os.environ["DISCORD_CLIENT_ID"],
        client_secret=os.environ["DISCORD_CLIENT_SECRET"],
        deployment=deployment)


@router.get("/verify")
async def begin(request: Request):
    service = getattr(request.app.state, "guild_verification", None)
    if service is None:
        return _page("Discord 身份验证服务暂不可用，请稍后重试。", status=503)
    try:
        service._require_configured()
    except VerificationError:
        return _page("Discord 身份验证尚未配置，请联系管理员。", status=503)
    origin = _https_origin(service.redirect_uri)
    if origin is None:
        return _page("Discord 回调地址配置不可用，请联系管理员。", status=503)
    # TLS is terminated by the reverse proxy; the internal ASGI scheme may be http.
    # Compare browser-normalized external hosts (IDNA, IPv6 and numeric IPv4),
    # while preserving the exact configured redirect_uri in both OAuth requests.
    request_origin = _https_origin("https://" + request.url.netloc)
    if request_origin != origin:
        # Establish the host-only nonce at the canonical callback origin first.
        return RedirectResponse(origin + "/self-register/verify", status_code=302, headers=HEADERS)
    if not service.admit_start(request.client.host if request.client else "unknown"):
        response = _page("验证请求过于频繁，请一分钟后重试。", status=429)
        response.headers["Retry-After"] = "60"
        return response
    try:
        return await service.begin(request.cookies.get(COOKIE, ""))
    except (VerificationError, sqlite3.Error):
        return _page("验证请求暂时不可用，请稍后重试。", status=503)


async def callback(request: Request, code: str, state: str, error: str):
    service = getattr(request.app.state, "guild_verification", None)
    if service is None:
        response = _page("Discord 身份验证服务暂不可用，请稍后重试。", status=503)
    else:
        try:
            await service.finish(state, request.cookies.get(COOKIE, ""), code, error)
            response = _page("已确认你拥有与注册相同要求的身份组。点击下方按钮接受邀请并加入服务器。",
                             success=True, status=200, invite_url=service.deployment.discord_invite_url)
        except VerificationError as exc:
            response = _page(str(exc))
        except (httpx.HTTPError, KeyError, ValueError, TypeError, AttributeError, sqlite3.Error):
            response = _page("Discord 验证暂时不可用，请稍后重新验证。")
    response.delete_cookie(COOKIE, httponly=True, secure=True, samesite="lax", path="/")
    return response
