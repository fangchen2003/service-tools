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
        self.dm_messages = []

        def discord(request):
            self.calls.append((request.method, request.url.path))
            if request.url.path == "/api/oauth2/token":
                return httpx.Response(200, json={"access_token": "temporary-user-token"})
            if request.url.path == "/api/users/@me" and request.headers.get("Authorization", "").startswith("Bot "):
                return httpx.Response(200, json={"id": "bot-1"})
            if request.url.path == "/api/users/@me":
                return httpx.Response(200, json={"id": "777"})
            if request.url.path == "/api/users/@me/guilds/100000000000000002/member":
                return httpx.Response(200, json={"roles": self.roles})
            if request.url.path == "/api/users/@me/channels":
                return httpx.Response(200, json={"id": "dm-1"})
            if request.url.path == "/api/channels/dm-1/messages":
                if request.method == "GET":
                    return httpx.Response(200, json=self.dm_messages)
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

    async def test_charge_restores_both_personal_image_quotas_once_per_day(self):
        await self.service.finish("auth-code", await self.begin())
        key_id = (await self.db._db.execute_fetchall(
            "SELECT key_id FROM discord_registrations WHERE discord_id='777'"))[0][0]
        await self.db.bump_counters(key_id, "2026-09-27", images=18, v5=8,
                                    legacy_free_images=10, requests=18)
        self.assertEqual(await self.db.charge_registered_key("777", "2026-09-27"), "charged")
        counter = await self.db.get_counter(key_id, "2026-09-27")
        self.assertEqual((counter["v5"], counter["legacy_free_images"], counter["images"], counter["requests"]),
                         (0, 0, 18, 18))
        self.assertEqual(await self.db.charge_registered_key("777", "2026-09-27"), "already")
        await self.db.bump_counters(key_id, "2026-09-27", images=2, v5=1, legacy_free_images=1)
        counter = await self.db.get_counter(key_id, "2026-09-27")
        self.assertEqual((counter["v5"], counter["legacy_free_images"]), (1, 1))
        self.assertEqual(await self.db.charge_registered_key("777", "2026-09-28"), "charged")

    async def test_charge_concurrent_claims_only_one_and_global_use_unchanged(self):
        await self.service.finish("auth-code", await self.begin())
        key_id = (await self.db._db.execute_fetchall(
            "SELECT key_id FROM discord_registrations WHERE discord_id='777'"))[0][0]
        await self.db.bump_counters(key_id, "2026-09-27", v5=8, legacy_free_images=12)
        results = await asyncio.gather(*(
            self.db.charge_registered_key("777", "2026-09-27") for _ in range(6)
        ))
        self.assertEqual(sorted(results), ["already"] * 5 + ["charged"])
        self.assertEqual(await self.db.day_v5_total("2026-09-27"), 8)
        self.assertEqual((await self.db._db.execute_fetchall(
            "SELECT legacy_free_images,v5 FROM counters WHERE key_id=? AND day=?",
            (key_id, "2026-09-27")))[0][:], (12, 8))
        self.assertEqual((await self.db._db.execute_fetchall(
            "SELECT count(*) FROM discord_daily_charges"))[0][0], 1)

    async def test_charge_endpoint_requires_bridge_and_registered_key(self):
        app = FastAPI()
        app.include_router(router)
        app.state.registrar = self.service
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            body = {"discord_id": "777", "guild_id": "100000000000000001"}
            denied = await client.post("/self-register/charge", json=body)
            self.assertEqual(denied.status_code, 401)
            headers = {"Authorization": "Bearer bridge-secret"}
            absent = await client.post("/self-register/charge", json=body, headers=headers)
            self.assertEqual(absent.status_code, 403)
            await self.service.finish("auth-code", await self.begin())
            ok = await client.post("/self-register/charge", json=body, headers=headers)
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok.json(), {"status": "charged"})
            again = await client.post("/self-register/charge", json=body, headers=headers)
            self.assertEqual(again.json(), {"status": "already"})
            other = await client.post("/self-register/charge", json={**body, "guild_id": "100000000000000002"}, headers=headers)
            self.assertEqual(other.status_code, 403)

    async def test_valid_role_receives_key_and_site_in_dm_once(self):
        state = await self.begin()
        result = await self.service.finish("auth-code", state)
        self.assertEqual(result, "sent")
        self.assertEqual((await self.db._db.execute_fetchall("SELECT count(*) FROM api_keys"))[0][0], 1)
        row = (await self.db._db.execute_fetchall("SELECT daily_v5,daily_images,allow_anlas,allow_img2img,exclude_global_v5,image_model_scope FROM api_keys"))[0]
        self.assertEqual(tuple(row), (30, 100, 0, 0, 0, "all"))
        self.assertEqual((await self.db._db.execute_fetchall("SELECT discord_id FROM discord_registrations"))[0][0], "777")
        self.assertIn(("POST", "/api/channels/dm-1/messages"), self.calls)
        self.assertIn("https://gate.example.com/", self.dm_body)
        self.assertIn("https://gate.example.com/workbench/", self.dm_body)
        self.assertIn("仿 NAI 官方的工作台", self.dm_body)
        self.assertIn("/charge", self.dm_body)
        self.assertIn("/find", self.dm_body)
        self.assertIn("V5 High 30 张", self.dm_body)
        self.assertIn("每天可使用一次", self.dm_body)
        self.assertIn("nai-", self.dm_body)
        with self.assertRaises(RegistrationError):
            await self.service.finish("auth-code", state)
        with self.assertRaises(RegistrationError):
            await self.begin()

    async def test_find_restores_deleted_registration_with_original_key(self):
        await self.service.finish("auth-code", await self.begin())
        registration = (await self.db._db.execute_fetchall(
            "SELECT key_id,token FROM discord_registrations WHERE discord_id='777'"))[0]
        original_key = registration["token"]
        await self.db.delete_key(registration["key_id"])

        self.assertEqual(await self.service.find("777", "100000000000000001"), "restored")
        restored = (await self.db._db.execute_fetchall(
            """SELECT r.key_id,r.token,k.token AS active_token,k.daily_v5 FROM discord_registrations r
               JOIN api_keys k ON k.id=r.key_id WHERE r.discord_id='777'"""))[0]
        self.assertNotEqual(restored["key_id"], registration["key_id"])
        self.assertEqual(restored["token"], original_key)
        self.assertEqual(restored["active_token"], original_key)
        self.assertEqual(restored["daily_v5"], 30)
        self.assertIn(original_key, self.dm_body)

    async def test_rotation_delete_find_never_restores_revoked_token(self):
        await self.service.finish("auth-code", await self.begin())
        registration = await self.db.get_discord_registration("777")
        old_token = registration["active_token"]
        current_token = "nai-rotated-current-fixture-token"
        self.assertTrue(await self.db.rotate_key_token(registration["key_id"], current_token))
        await self.db.delete_key(registration["key_id"])

        self.assertEqual(await self.service.find("777", "100000000000000001"), "restored")
        self.assertIsNone(await self.db.get_key_by_token(old_token))
        self.assertIsNotNone(await self.db.get_key_by_token(current_token))
        self.assertIn(current_token, self.dm_body)
        self.assertNotIn(old_token, self.dm_body)

    async def test_rotation_syncs_recovery_token_atomically(self):
        import sqlite3
        await self.service.finish("auth-code", await self.begin())
        registration = await self.db.get_discord_registration("777")
        key_id, old_token = registration["key_id"], registration["active_token"]
        await self.db._db.execute("""CREATE TRIGGER reject_recovery_update
            BEFORE UPDATE OF token ON discord_registrations
            BEGIN SELECT RAISE(ABORT, 'fixture recovery failure'); END""")
        await self.db._db.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "fixture recovery failure"):
            await self.db.rotate_key_token(key_id, "nai-rotated-current-fixture-token")
        await self.db._db.rollback()
        self.assertEqual((await self.db.get_key(key_id))["token"], old_token)
        self.assertEqual((await self.db.get_discord_registration("777"))["token"], old_token)

    async def test_delete_archives_current_token_even_if_recovery_copy_was_stale(self):
        await self.service.finish("auth-code", await self.begin())
        registration = await self.db.get_discord_registration("777")
        old_token = registration["active_token"]
        current_token = "nai-rotated-current-fixture-token"
        await self.db.rotate_key_token(registration["key_id"], current_token)
        # Simulate a stale archive left by an earlier version before reclamation.
        await self.db._db.execute("UPDATE discord_registrations SET token=? WHERE discord_id='777'",
                                  (old_token,))
        await self.db._db.commit()
        await self.db.delete_key(registration["key_id"])
        self.assertEqual((await self.db.get_discord_registration("777"))["token"], current_token)
        self.assertEqual(await self.service.find("777", "100000000000000001"), "restored")
        self.assertIsNone(await self.db.get_key_by_token(old_token))
        self.assertIsNotNone(await self.db.get_key_by_token(current_token))

    async def test_find_recovers_legacy_deleted_key_from_own_bot_dm(self):
        await self.service.finish("auth-code", await self.begin())
        registration = (await self.db._db.execute_fetchall(
            "SELECT key_id,token FROM discord_registrations WHERE discord_id='777'"))[0]
        original_key = registration["token"]
        await self.db.delete_key(registration["key_id"])
        await self.db._db.execute("UPDATE discord_registrations SET token=NULL WHERE discord_id='777'")
        await self.db._db.commit()
        self.dm_messages = [{"id": "10", "author": {"id": "bot-1"},
                             "content": f"你的 NAI Gate API Key：`{original_key}`\n网址：fixture"}]

        self.assertEqual(await self.service.find("777", "100000000000000001"), "restored")
        restored = (await self.db._db.execute_fetchall(
            "SELECT token FROM discord_registrations WHERE discord_id='777'"))[0][0]
        self.assertEqual(restored, original_key)

    async def test_find_active_key_resends_without_recreating(self):
        await self.service.finish("auth-code", await self.begin())
        before = (await self.db._db.execute_fetchall(
            "SELECT key_id,token FROM discord_registrations WHERE discord_id='777'"))[0]
        self.assertEqual(await self.service.find("777", "100000000000000001"), "active")
        after = (await self.db._db.execute_fetchall(
            "SELECT key_id,token FROM discord_registrations WHERE discord_id='777'"))[0]
        self.assertEqual(tuple(after), tuple(before))
        self.assertIn(before["token"], self.dm_body)

    async def test_find_dm_failure_rolls_back_restoration_but_keeps_archive(self):
        await self.service.finish("auth-code", await self.begin())
        registration = (await self.db._db.execute_fetchall(
            "SELECT key_id,token FROM discord_registrations WHERE discord_id='777'"))[0]
        await self.db.delete_key(registration["key_id"])
        self.dm_fails = True
        with self.assertRaises(RegistrationError):
            await self.service.find("777", "100000000000000001")
        current = (await self.db._db.execute_fetchall(
            "SELECT key_id,token FROM discord_registrations WHERE discord_id='777'"))[0]
        self.assertEqual(current["key_id"], registration["key_id"])
        self.assertEqual(current["token"], registration["token"])
        self.assertEqual((await self.db._db.execute_fetchall("SELECT count(*) FROM api_keys"))[0][0], 0)

    async def test_find_rejects_unregistered_user_and_wrong_command_guild(self):
        with self.assertRaises(RegistrationError):
            await self.service.find("777", "100000000000000001")
        with self.assertRaises(RegistrationError):
            await self.service.find("777", "100000000000000002")

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
