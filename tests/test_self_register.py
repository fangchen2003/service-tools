"""No billable upstream calls: Discord OAuth enrollment contract."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI

from app.database import Database
from app.registration import RegistrationService, RegistrationError
from app.registration_routes import router


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "gate.sqlite"))
        await self.db.connect()
        self.calls = []
        self.dm_fails = False
        self.roles = ["100000000000000003"]

        def discord(request):
            self.calls.append((request.method, request.url.path))
            if request.url.path == "/api/oauth2/token":
                return httpx.Response(200, json={"access_token": "temporary-user-token"})
            if request.url.path == "/api/users/@me":
                return httpx.Response(200, json={"id": "777"})
            if request.url.path == "/api/users/@me/guilds/100000000000000002/member":
                return httpx.Response(200, json={"roles": self.roles})
            if request.url.path == "/api/users/@me/channels":
                return httpx.Response(200, json={"id": "dm-1"})
            if request.url.path == "/api/channels/dm-1/messages":
                self.dm_body = request.content.decode()
                return httpx.Response(403 if self.dm_fails else 200, json={})
            raise AssertionError(f"Unexpected Discord request {request.method} {request.url}")
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(discord), base_url="https://discord.com")
        self.service = RegistrationService(self.db, self.http, client_id="client-id", client_secret="client-secret",
            bot_token="fake-bot-token", bridge_secret="bridge-secret",
            redirect_uri="https://gate.example.com/self-register/callback")

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.db.close()
        self.tmp.cleanup()

    async def begin(self, user="777", guild="100000000000000001"):
        link = await self.service.begin(user, guild)
        return parse_qs(urlparse(link).query)["state"][0]

    async def test_valid_role_receives_key_and_site_in_dm_once(self):
        state = await self.begin()
        result = await self.service.finish("auth-code", state)
        self.assertEqual(result, "sent")
        self.assertEqual((await self.db._db.execute_fetchall("SELECT count(*) FROM api_keys"))[0][0], 1)
        row = (await self.db._db.execute_fetchall("SELECT daily_v5,daily_images,allow_anlas,allow_img2img,exclude_global_v5,image_model_scope FROM api_keys"))[0]
        self.assertEqual(tuple(row), (50, 100, 0, 0, 0, "all"))
        self.assertEqual((await self.db._db.execute_fetchall("SELECT discord_id FROM discord_registrations"))[0][0], "777")
        self.assertIn(("POST", "/api/channels/dm-1/messages"), self.calls)
        self.assertIn("https://gate.example.com/", self.dm_body)
        self.assertIn("nai-", self.dm_body)
        with self.assertRaises(RegistrationError):
            await self.service.finish("auth-code", state)
        with self.assertRaises(RegistrationError):
            await self.begin()

    async def test_wrong_server_and_role_never_mint(self):
        with self.assertRaises(RegistrationError):
            await self.begin(guild="100000000000000002")
        self.roles = []
        with self.assertRaises(RegistrationError):
            await self.service.finish("auth-code", await self.begin())
        self.assertEqual((await self.db._db.execute_fetchall("SELECT count(*) FROM api_keys"))[0][0], 0)

    async def test_dm_failure_rolls_back_key_and_allows_retry(self):
        self.dm_fails = True
        with self.assertRaises(RegistrationError):
            await self.service.finish("auth-code", await self.begin())
        self.assertEqual((await self.db._db.execute_fetchall("SELECT count(*) FROM api_keys"))[0][0], 0)
        self.assertEqual((await self.db._db.execute_fetchall("SELECT count(*) FROM discord_registrations"))[0][0], 0)
        self.dm_fails = False
        self.assertEqual(await self.service.finish("auth-code", await self.begin()), "sent")

    async def test_oauth_user_mismatch_never_mint(self):
        state = await self.begin(user="778")
        with self.assertRaises(RegistrationError):
            await self.service.finish("auth-code", state)
        self.assertEqual((await self.db._db.execute_fetchall("SELECT count(*) FROM api_keys"))[0][0], 0)

    async def test_http_intent_requires_bridge_secret_and_callback_only_reports_status(self):
        app = FastAPI()
        app.include_router(router)
        app.state.registrar = self.service
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gate.example.com") as client:
            denied = await client.post("/self-register/intent", json={"discord_id": "777", "guild_id": "100000000000000001"})
            self.assertEqual(denied.status_code, 401)
            allowed = await client.post("/self-register/intent", headers={"Authorization": "Bearer bridge-secret"},
                json={"discord_id": "777", "guild_id": "100000000000000001"})
            self.assertEqual(allowed.status_code, 200)
            state = parse_qs(urlparse(allowed.json()["url"]).query)["state"][0]
            done = await client.get("/self-register/callback", params={"code": "auth-code", "state": state})
            self.assertEqual(done.status_code, 200)
            self.assertNotIn("nai-", done.text)
            self.assertEqual(done.headers["cache-control"], "no-store")
            self.assertEqual(done.headers["referrer-policy"], "no-referrer")
