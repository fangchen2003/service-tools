"""Discord browser login for existing keys; never issue or restore an API key."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from urllib.parse import urlencode

import aiosqlite
import httpx
from fastapi import APIRouter, HTTPException, Request
from .body import read_json_body
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.datastructures import MutableHeaders


class SecurityHeadersMiddleware:
    """Include router-level 404/405 and CORS responses in the no-store boundary."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        protected = path == "/self-register/callback" or path == "/workbench-auth" or path.startswith("/workbench-auth/")
        if scope["type"] != "http" or not protected:
            return await self.app(scope, receive, send)

        async def send_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_headers)


router = APIRouter(prefix="/workbench-auth")
WORKBENCH_URL = "https://gate.example.com/workbench/"
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
           "X-Content-Type-Options": "nosniff"}


ERROR_CODES = frozenset({"not_registered", "key_reclaimed", "key_disabled", "key_expired",
                         "auth_failed", "state_invalid", "service_unavailable"})


def redirect_error(code: str):
    if code not in ERROR_CODES:
        code = "auth_failed"
    return RedirectResponse(WORKBENCH_URL + "?login_error=" + code, status_code=302, headers=HEADERS)


OAUTH_COOKIE = "__Host-nai-gate-wb-oauth"
SESSION_COOKIE = "__Host-nai-gate-wb-session"
STATE_TTL = 600
SESSION_TTL = 7 * 86400
MAX_STATES = 10000
MAX_SESSIONS = 50000
MAX_USER_SESSIONS = 20


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class WorkbenchAuthService:
    def __init__(self, db, http: httpx.AsyncClient, *, client_id: str, client_secret: str,
                 bridge_secret: str, redirect_uri: str):
        self.db, self.http = db, http
        self.client_id, self.client_secret = client_id, client_secret
        self.bridge_secret, self.redirect_uri = bridge_secret, redirect_uri

    async def initialize(self):
        async with aiosqlite.connect(self.db.path, timeout=5) as conn:
            await conn.executescript("""
                CREATE TABLE IF NOT EXISTS workbench_oauth_states (
                    state_hash TEXT PRIMARY KEY, nonce_hash TEXT NOT NULL,
                    verifier TEXT NOT NULL, expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_wb_oauth_expiry ON workbench_oauth_states(expires_at);
                CREATE TABLE IF NOT EXISTS workbench_sessions (
                    token_hash TEXT PRIMARY KEY, discord_id TEXT NOT NULL,
                    key_id INTEGER NOT NULL, key_token_hash TEXT NOT NULL,
                    binding_token_hash TEXT NOT NULL, created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_wb_session_expiry ON workbench_sessions(expires_at);
                CREATE INDEX IF NOT EXISTS idx_wb_session_discord ON workbench_sessions(discord_id);
                CREATE INDEX IF NOT EXISTS idx_wb_session_key ON workbench_sessions(key_id);
                CREATE TRIGGER IF NOT EXISTS wb_key_changed
                AFTER UPDATE OF token, enabled, expires_at ON api_keys
                WHEN OLD.token IS NOT NEW.token OR OLD.enabled IS NOT NEW.enabled
                  OR OLD.expires_at IS NOT NEW.expires_at
                BEGIN
                    DELETE FROM workbench_sessions WHERE key_id=OLD.id;
                END;
                CREATE TRIGGER IF NOT EXISTS wb_key_deleted AFTER DELETE ON api_keys
                BEGIN
                    DELETE FROM workbench_sessions WHERE key_id=OLD.id;
                END;
                CREATE TRIGGER IF NOT EXISTS wb_binding_deleted AFTER DELETE ON discord_registrations
                BEGIN
                    DELETE FROM workbench_sessions WHERE discord_id=OLD.discord_id;
                END;
                CREATE TRIGGER IF NOT EXISTS wb_binding_changed
                AFTER UPDATE OF discord_id, key_id, token ON discord_registrations
                WHEN OLD.discord_id IS NOT NEW.discord_id OR OLD.key_id IS NOT NEW.key_id
                  OR OLD.token IS NOT NEW.token
                BEGIN
                    DELETE FROM workbench_sessions WHERE discord_id=OLD.discord_id;
                END;
            """)
            await conn.commit()

    async def begin(self):
        state = "wb." + secrets.token_urlsafe(32)
        nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        async with aiosqlite.connect(self.db.path, timeout=5) as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute("DELETE FROM workbench_oauth_states WHERE expires_at<=?", (now,))
            count = (await (await conn.execute("SELECT count(*) FROM workbench_oauth_states")).fetchone())[0]
            if count >= MAX_STATES:
                raise RuntimeError("state capacity")
            await conn.execute("INSERT INTO workbench_oauth_states VALUES(?,?,?,?)",
                               (digest(state), digest(nonce), verifier, now + STATE_TTL))
            await conn.commit()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        url = "https://discord.com/oauth2/authorize?" + urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri,
            "response_type": "code", "scope": "identify", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        response = RedirectResponse(url, status_code=302, headers=HEADERS)
        response.set_cookie(OAUTH_COOKIE, nonce, max_age=STATE_TTL, httponly=True,
                            secure=True, samesite="lax", path="/")
        return response


    async def consume_state(self, state: str, nonce: str) -> str:
        if not re.fullmatch(r"wb\.[A-Za-z0-9_-]{43}", state) or not valid_token(nonce):
            raise AuthError("state_invalid")
        async with aiosqlite.connect(self.db.path, timeout=5) as conn:
            await conn.execute("BEGIN IMMEDIATE")
            row = await (await conn.execute(
                "SELECT verifier FROM workbench_oauth_states WHERE state_hash=? AND nonce_hash=? AND expires_at>?",
                (digest(state), digest(nonce), time.time()),
            )).fetchone()
            if row is None:
                raise AuthError("state_invalid")
            await conn.execute("DELETE FROM workbench_oauth_states WHERE state_hash=?", (digest(state),))
            await conn.commit()
            return row[0]

    async def finish(self, code: str, state: str, nonce: str, old_session: str, error: str):
        verifier = await self.consume_state(state, nonce)
        if error or not code or len(code) > 2048:
            raise AuthError("auth_failed")
        response = await self.http.post("https://discord.com/api/oauth2/token", data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
            "code_verifier": verifier,
        }, timeout=12)
        if response.status_code != 200:
            raise AuthError("auth_failed")
        token = response.json()["access_token"]
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise AuthError("auth_failed")
        response = await self.http.get("https://discord.com/api/users/@me",
                                       headers={"Authorization": "Bearer " + token}, timeout=12)
        if response.status_code != 200:
            raise AuthError("auth_failed")
        discord_id = response.json()["id"]
        if not isinstance(discord_id, str) or not re.fullmatch(r"[0-9]{1,32}", discord_id):
            raise AuthError("auth_failed")
        session = secrets.token_urlsafe(32)
        now = time.time()
        async with aiosqlite.connect(self.db.path, timeout=5) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")
            row = await (await conn.execute("""SELECT r.key_id, r.token AS binding_token,
                k.token, k.enabled, k.expires_at FROM discord_registrations r
                LEFT JOIN api_keys k ON k.id=r.key_id WHERE r.discord_id=?""", (discord_id,))).fetchone()
            if row is None:
                raise AuthError("not_registered")
            if row["token"] is None:
                raise AuthError("key_reclaimed")
            if not row["enabled"]:
                raise AuthError("key_disabled")
            if row["expires_at"] is not None and row["expires_at"] <= now:
                raise AuthError("key_expired")
            await conn.execute("DELETE FROM workbench_sessions WHERE expires_at<=?", (now,))
            if valid_token(old_session):
                await conn.execute("DELETE FROM workbench_sessions WHERE token_hash=?", (digest(old_session),))
            await conn.execute("""DELETE FROM workbench_sessions WHERE discord_id=? AND token_hash NOT IN (
                SELECT token_hash FROM workbench_sessions WHERE discord_id=?
                ORDER BY created_at DESC, token_hash DESC LIMIT ?)""",
                (discord_id, discord_id, MAX_USER_SESSIONS - 1))
            count = (await (await conn.execute("SELECT count(*) FROM workbench_sessions")).fetchone())[0]
            if count >= MAX_SESSIONS:
                raise AuthError("service_unavailable")
            await conn.execute("INSERT INTO workbench_sessions VALUES(?,?,?,?,?,?,?)",
                (digest(session), discord_id, row["key_id"], digest(row["token"]),
                 digest(row["binding_token"] or ""), now, now + SESSION_TTL))
            await conn.commit()
        return session

    async def revoke(self, session: str):
        if not valid_token(session):
            return
        async with aiosqlite.connect(self.db.path, timeout=5) as conn:
            await conn.execute("DELETE FROM workbench_sessions WHERE token_hash=?", (digest(session),))
            await conn.commit()

    async def resolve(self, session: str):
        if not valid_token(session):
            raise AuthError("auth_failed")
        async with aiosqlite.connect(self.db.path, timeout=5) as conn:
            conn.row_factory = aiosqlite.Row
            row = await (await conn.execute("""SELECT s.*, k.token, k.enabled,
                k.expires_at AS key_expires_at, r.token AS binding_token
                FROM workbench_sessions s
                JOIN discord_registrations r ON r.discord_id=s.discord_id AND r.key_id=s.key_id
                JOIN api_keys k ON k.id=s.key_id WHERE s.token_hash=?""", (digest(session),))).fetchone()
            if row is None or row["expires_at"] <= time.time() or not row["enabled"] or (
                    row["key_expires_at"] is not None and row["key_expires_at"] <= time.time()):
                raise AuthError("auth_failed")
            if not hmac.compare_digest(row["key_token_hash"], digest(row["token"])) or not hmac.compare_digest(
                    row["binding_token_hash"], digest(row["binding_token"] or "")):
                raise AuthError("auth_failed")
            return row["token"]


def configured_service(db, http: httpx.AsyncClient) -> WorkbenchAuthService | None:
    client_id, client_secret = os.getenv("DISCORD_CLIENT_ID", ""), os.getenv("DISCORD_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None
    return WorkbenchAuthService(db, http, client_id=client_id, client_secret=client_secret,
        bridge_secret=os.getenv("WORKBENCH_AUTH_BRIDGE_SECRET", ""),
        redirect_uri=os.getenv("DISCORD_REDIRECT_URI", "https://gate.example.com/self-register/callback"))


class AuthError(Exception):
    def __init__(self, code: str):
        self.code = code


def valid_token(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is not None


def clear_cookie(response, name):
    response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="lax")


async def callback(request: Request, code: str, state: str, error: str):
    service = getattr(request.app.state, "workbench_auth", None)
    response = redirect_error("service_unavailable")
    if service is not None:
        try:
            session = await service.finish(code, state, request.cookies.get(OAUTH_COOKIE, ""),
                                           request.cookies.get(SESSION_COOKIE, ""), error)
            response = RedirectResponse(WORKBENCH_URL, status_code=302, headers=HEADERS)
            response.set_cookie(SESSION_COOKIE, session, max_age=SESSION_TTL,
                                httponly=True, secure=True, samesite="lax", path="/")
        except AuthError as exc:
            response = redirect_error(exc.code)
        except httpx.HTTPError:
            response = redirect_error("service_unavailable")
        except (ValueError, KeyError, TypeError):
            response = redirect_error("auth_failed")
        except Exception:
            response = redirect_error("service_unavailable")
    clear_cookie(response, OAUTH_COOKIE)
    return response


@router.get("/session")
async def session_status(request: Request):
    service = getattr(request.app.state, "workbench_auth", None)
    if service is None:
        return JSONResponse({"error": {"code": "service_unavailable"}}, status_code=503, headers=HEADERS)
    try:
        await service.resolve(request.cookies.get(SESSION_COOKIE, ""))
    except AuthError:
        response = JSONResponse({"authenticated": False}, status_code=401, headers=HEADERS)
        clear_cookie(response, SESSION_COOKIE)
        return response
    except Exception:
        return JSONResponse({"error": {"code": "service_unavailable"}}, status_code=503, headers=HEADERS)
    return JSONResponse({"authenticated": True}, headers=HEADERS)


def failure(code: str, status: int):
    return JSONResponse({"error": {"code": code}}, status_code=status, headers=HEADERS)


@router.post("/logout")
async def logout(request: Request):
    if request.headers.get("Origin") != "https://gate.example.com" or request.headers.get("X-Workbench-CSRF") != "1":
        return failure("auth_failed", 403)
    service = getattr(request.app.state, "workbench_auth", None)
    session = request.cookies.get(SESSION_COOKIE, "")
    if session:
        if service is None:
            return failure("service_unavailable", 503)
        try:
            await service.revoke(session)
        except Exception:
            return failure("service_unavailable", 503)
    response = JSONResponse({"authenticated": False}, headers=HEADERS)
    clear_cookie(response, SESSION_COOKIE)
    return response


@router.post("/resolve")
async def private_resolve(request: Request):
    service = getattr(request.app.state, "workbench_auth", None)
    secret = service.bridge_secret if service is not None else ""
    given = request.headers.get("Authorization", "")
    if not secret or not hmac.compare_digest(given.encode(), ("Bearer " + secret).encode()):
        return failure("auth_failed", 401)
    try:
        body = await read_json_body(request, 4096)
    except HTTPException as exc:
        return failure("invalid_request", exc.status_code)
    if set(body) != {"session_token"} or not valid_token(body["session_token"]):
        return failure("invalid_request", 400)
    try:
        key = await service.resolve(body["session_token"])
    except AuthError:
        return failure("auth_failed", 401)
    except Exception:
        return failure("service_unavailable", 503)
    return JSONResponse({"key": key}, headers=HEADERS)


@router.get("/discord/start")
async def start(request: Request):
    service = getattr(request.app.state, "workbench_auth", None)
    if service is None:
        return redirect_error("service_unavailable")
    try:
        return await service.begin()
    except Exception:
        return redirect_error("service_unavailable")
