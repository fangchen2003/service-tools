"""Registration controls use temporary SQLite and fake Discord only."""
import asyncio
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI

from app import registration
from test_registration_configuration import DEPLOYMENT
from app.database import Database


class RegistrationControlsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "gate.sqlite")
        self.db = Database(self.path, tz="America/New_York")
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def status(self):
        helper = getattr(registration, "registration_status", None)
        self.assertIsNotNone(helper, "registration_status helper is missing")
        return await helper(self.db)

    async def seed(self, user, timestamp):
        key = await self.db.create_key({"name": "fixture", "token": "nai-fixture-" + user,
            "daily_images": 100, "monthly_anlas": 0, "daily_text_tokens": 0, "rpm": 5})
        await self.db._db.execute(
            "INSERT INTO discord_registrations(discord_id,key_id,token,created_at) VALUES(?,?,?,?)",
            (user, key["id"], key["token"], timestamp))
        await self.db._db.commit()
        return key["id"]

    async def admin_client(self, service=None):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec("app.registration_admin"),
                             "registration admin router is missing")
        from app.admin import COOKIE, _sign
        from app.registration_admin import router
        app = FastAPI()
        app.include_router(router)
        app.state.gate = SimpleNamespace(db=self.db, settings=SimpleNamespace(secret_key="fixture-secret"))
        app.state.registrar = service
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://local")
        payload = str(int(time.time()) + 3600)
        cookie = payload + "." + _sign("fixture-secret", payload)
        client.cookies.set(COOKIE, cookie)
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_admin_get_put_and_settings_survive_reconnect(self):
        client = await self.admin_client()
        response = await client.get("/admin/api/registration")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(set(data), {"enabled", "configured", "daily_limit", "today_count",
                                    "total_count", "day", "timezone", "history", "csrf_token"})
        self.assertEqual((data["enabled"], data["configured"], data["daily_limit"]), (True, False, 0))
        self.assertEqual(response.headers["cache-control"], "no-store")
        from app.admin import COOKIE, _sign
        self.assertEqual(data["csrf_token"], _sign("fixture-secret", "registration:" + client.cookies[COOKIE]))
        updated = await client.put("/admin/api/registration", json={"enabled": False, "daily_limit": 7},
            headers={"x-nai-admin-csrf": data["csrf_token"]})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual((updated.json()["enabled"], updated.json()["daily_limit"]), (False, 7))
        self.assertEqual(set(updated.json()), set(data))
        await self.db.close()
        self.db = Database(self.path, tz="America/New_York")
        await self.db.connect()
        status = await self.status()
        self.assertEqual((status["enabled"], status["daily_limit"]), (False, 7))

    async def test_admin_requires_auth_session_csrf_and_json(self):
        client = await self.admin_client()
        token = (await client.get("/admin/api/registration")).json()["csrf_token"]
        body = {"enabled": False, "daily_limit": 9}
        for headers in ({}, {"x-nai-admin-csrf": "wrong"}):
            denied = await client.put("/admin/api/registration", json=body, headers=headers)
            self.assertEqual(denied.status_code, 403)
        from app.admin import COOKIE, _sign
        # An authentic second session cannot replay the first session's token.
        payload = str(int(time.time()) + 7200)
        client.cookies.set(COOKIE, payload + "." + _sign("fixture-secret", payload))
        denied = await client.put("/admin/api/registration", json=body,
                                  headers={"x-nai-admin-csrf": token})
        self.assertEqual(denied.status_code, 403)
        token = (await client.get("/admin/api/registration")).json()["csrf_token"]
        denied = await client.put("/admin/api/registration", content='{"enabled":false,"daily_limit":9}',
                                  headers={"x-nai-admin-csrf": token, "content-type": "text/plain"})
        self.assertEqual(denied.status_code, 415)
        client.cookies.clear()
        self.assertEqual((await client.get("/admin/api/registration")).status_code, 401)
        self.assertEqual((await client.put("/admin/api/registration", json=body)).status_code, 401)
        self.assertEqual((await self.status())["enabled"], True)

    async def test_admin_validates_explicit_fields_without_partial_writes(self):
        client = await self.admin_client()
        token = (await client.get("/admin/api/registration")).json()["csrf_token"]
        valid = {"enabled": False, "daily_limit": 4}
        invalid = [dict(valid, extra=True), {}, {"enabled": True}, {"daily_limit": 0},
                   dict(valid, enabled=1), dict(valid, enabled="false"), dict(valid, enabled=None)]
        invalid += [dict(valid, daily_limit=value) for value in
                    (True, False, -1, 100001, 1.0, "1", None)]
        for body in invalid:
            with self.subTest(body=body):
                response = await client.put("/admin/api/registration", json=body,
                                           headers={"x-nai-admin-csrf": token})
                self.assertEqual(response.status_code, 422)
                status = await self.status()
                self.assertEqual((status["enabled"], status["daily_limit"]), (True, 0))
        for limit in (100000, 0):
            response = await client.put("/admin/api/registration", json={"enabled": True, "daily_limit": limit},
                headers={"x-nai-admin-csrf": token, "content-type": "application/json; charset=utf-8"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["daily_limit"], limit)

    async def test_admin_bulk_storage_rolls_back_if_second_setting_fails(self):
        import sqlite3
        client = await self.admin_client()
        token = (await client.get("/admin/api/registration")).json()["csrf_token"]
        await self.db.set_settings_bulk({"registration_enabled": True, "registration_daily_limit": 2})
        await self.db._db.execute("""CREATE TRIGGER reject_limit BEFORE INSERT ON site_settings
            WHEN NEW.key='registration_daily_limit'
            BEGIN SELECT RAISE(ABORT, 'fixture rejected limit'); END""")
        await self.db._db.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "fixture rejected limit"):
            await client.put("/admin/api/registration", json={"enabled": False, "daily_limit": 3},
                             headers={"x-nai-admin-csrf": token})
        # A later shared-connection commit must not accidentally save half a PUT.
        await self.db.set_setting("unrelated_fixture", "1")
        status = await self.status()
        self.assertEqual((status["enabled"], status["daily_limit"]), (True, 2))

    async def service(self):
        from urllib.parse import parse_qs
        self.discord_calls = []
        self.dm_fails = False
        self.role_hook = None
        self.dm_hook = None

        async def discord(request):
            path = request.url.path
            self.discord_calls.append(path)
            if path == "/api/oauth2/token":
                user = parse_qs(request.content.decode())["code"][0]
                return httpx.Response(200, json={"access_token": user})
            if path == "/api/users/@me":
                return httpx.Response(200, json={"id": request.headers["authorization"].split()[1]})
            if path.endswith("/member"):
                if self.role_hook:
                    await self.role_hook()
                return httpx.Response(200, json={"roles": [DEPLOYMENT.membership_role]})
            if path == "/api/users/@me/channels":
                return httpx.Response(200, json={"id": "dm"})
            if path == "/api/channels/dm/messages":
                if self.dm_hook:
                    await self.dm_hook()
                return httpx.Response(403 if self.dm_fails else 200, json={})
            raise AssertionError(f"Unexpected external request: {path}")

        http = httpx.AsyncClient(transport=httpx.MockTransport(discord))
        self.addAsyncCleanup(http.aclose)
        return registration.RegistrationService(self.db, http, deployment=DEPLOYMENT, client_id="fixture", client_secret="fixture",
            bot_token="fixture", bridge_secret="fixture", redirect_uri="https://local/callback")

    async def begin(self, service, user="777"):
        from urllib.parse import parse_qs, urlparse
        url = await service.begin(user, DEPLOYMENT.command_guild)
        return parse_qs(urlparse(url).query)["state"][0]

    async def test_paused_begin_and_preexisting_oauth_pending_cannot_issue(self):
        service = await self.service()
        state = await self.begin(service)
        await self.db.set_setting("registration_enabled", False)
        with self.assertRaises(registration.RegistrationError):
            await self.begin(service, "778")
        with self.assertRaises(registration.RegistrationError):
            await service.finish("777", state)
        self.assertEqual(self.discord_calls, [])
        self.assertEqual((await self.status())["total_count"], 0)
        await self.db.set_setting("registration_enabled", True)
        # Pausing consumed the old, one-use OAuth state.
        with self.assertRaises(registration.RegistrationError):
            await service.finish("777", state)
        self.assertEqual(await service.finish("777", await self.begin(service)), "sent")

    async def test_daily_cap_concurrent_finish_pending_does_not_reserve_and_deleted_counts(self):
        service = await self.service()
        await self.db.set_setting("registration_daily_limit", 1)
        users = [str(user) for user in range(100, 106)]
        states = [await self.begin(service, user) for user in users]
        self.assertEqual((await self.status())["today_count"], 0)
        results = await asyncio.gather(*(service.finish(user, state)
            for user, state in zip(users, states)), return_exceptions=True)
        self.assertEqual(results.count("sent"), 1)
        self.assertEqual(sum(isinstance(result, registration.RegistrationError) for result in results), 5)
        status = await self.status()
        self.assertEqual((status["today_count"], status["total_count"]), (1, 1))
        key_id = (await self.db._db.execute_fetchall("SELECT key_id FROM discord_registrations"))[0][0]
        await self.db.delete_key(key_id)
        with self.assertRaises(registration.RegistrationError):
            await self.begin(service, "999")
        # Tomorrow's local calendar day has fresh capacity without removing history.
        tomorrow = datetime.now(ZoneInfo(self.db.tz)).date() + timedelta(days=1)
        clock = datetime.combine(tomorrow, datetime.min.time(), ZoneInfo(self.db.tz)).timestamp()
        with patch("app.registration.time.time", return_value=clock):
            self.assertTrue(await self.begin(service, "999"))
            self.assertEqual((await self.status())["today_count"], 0)

    async def test_admin_pause_during_discord_role_check_prevents_issuance(self):
        service = await self.service()
        client = await self.admin_client(service)
        before = (await client.get("/admin/api/registration")).json()
        self.assertTrue(before["configured"])
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold_role():
            entered.set()
            await release.wait()

        self.role_hook = hold_role
        task = asyncio.create_task(service.finish("777", await self.begin(service)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            response = await asyncio.wait_for(client.put("/admin/api/registration",
                json={"enabled": False, "daily_limit": 0},
                headers={"x-nai-admin-csrf": before["csrf_token"]}), 2)
            self.assertEqual(response.status_code, 200)
        finally:
            release.set()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
        self.assertIsInstance(result, registration.RegistrationError)
        self.assertEqual((await self.status())["total_count"], 0)
        self.assertNotIn("/api/users/@me/channels", self.discord_calls)

    async def test_admin_put_waits_for_in_progress_key_issuance(self):
        service = await self.service()
        client = await self.admin_client(service)
        token = (await client.get("/admin/api/registration")).json()["csrf_token"]
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold_dm():
            entered.set()
            await release.wait()

        self.dm_hook = hold_dm
        finish = asyncio.create_task(service.finish("777", await self.begin(service)))
        update = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            update = asyncio.create_task(client.put("/admin/api/registration",
                json={"enabled": False, "daily_limit": 1}, headers={"x-nai-admin-csrf": token}))
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(update), 0.1)
        finally:
            release.set()
            self.assertEqual(await asyncio.wait_for(finish, 2), "sent")
            if update is not None:
                response = await asyncio.wait_for(update, 2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["enabled"], response.json()["today_count"]), (False, 1))

    async def test_dm_failure_does_not_consume_daily_capacity(self):
        service = await self.service()
        await self.db.set_setting("registration_daily_limit", 1)
        state = await self.begin(service)
        other = await self.begin(service, "778")
        self.dm_fails = True
        with self.assertRaises(registration.RegistrationError):
            await service.finish("777", state)
        status = await self.status()
        self.assertEqual((status["today_count"], status["total_count"]), (0, 0))
        self.assertTrue(all(item["count"] == 0 for item in status["history"]))
        self.assertEqual((await self.db._db.execute_fetchall("SELECT COUNT(*) FROM api_keys"))[0][0], 0)
        self.dm_fails = False
        self.assertEqual(await service.finish("778", other), "sent")
        self.assertEqual((await self.status())["today_count"], 1)

    async def test_lower_limit_during_oauth_is_rechecked(self):
        service = await self.service()
        client = await self.admin_client(service)
        token = (await client.get("/admin/api/registration")).json()["csrf_token"]
        await self.seed("100", time.time())
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold_role():
            entered.set()
            await release.wait()

        self.role_hook = hold_role
        finish = asyncio.create_task(service.finish("777", await self.begin(service)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            response = await asyncio.wait_for(client.put("/admin/api/registration",
                json={"enabled": True, "daily_limit": 1}, headers={"x-nai-admin-csrf": token}), 2)
            self.assertEqual(response.status_code, 200)
        finally:
            release.set()
            result = (await asyncio.gather(finish, return_exceptions=True))[0]
        self.assertIsInstance(result, registration.RegistrationError)
        self.assertEqual((await self.status())["total_count"], 1)
        self.assertNotIn("/api/users/@me/channels", self.discord_calls)

    async def test_duplicate_concurrent_oauth_only_issues_one_key(self):
        service = await self.service()
        states = [await self.begin(service) for _ in range(2)]
        # Ensure both OAuth checks start before either enters final issuance.
        entered, release = asyncio.Event(), asyncio.Event()
        arrivals = 0

        async def barrier():
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                entered.set()
            await release.wait()

        self.role_hook = barrier
        tasks = [asyncio.create_task(service.finish("777", state)) for state in states]
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(results.count("sent"), 1)
        self.assertEqual(sum(isinstance(item, registration.RegistrationError) for item in results), 1)
        self.assertEqual((await self.status())["total_count"], 1)

    async def test_find_and_charge_are_not_blocked_by_controls(self):
        service = await self.service()
        await service.finish("777", await self.begin(service))
        before = await self.status()
        await self.db.set_settings_bulk({"registration_enabled": False, "registration_daily_limit": 1})
        self.assertEqual(await service.find("777", DEPLOYMENT.command_guild), "active")
        self.assertEqual(await self.db.charge_registered_key("777", before["day"]), "charged")
        after = await self.status()
        self.assertEqual((after["total_count"], after["today_count"]), (1, 1))

    async def test_status_defaults_history_timezone_and_deleted_key(self):
        # DST starts on this date: a fixed 86400-second window is incorrect.
        now = datetime(2026, 3, 8, 23, 59, tzinfo=ZoneInfo(self.db.tz))
        start = datetime(2026, 3, 8, tzinfo=ZoneInfo(self.db.tz)).timestamp()
        await self.seed("1", start - 0.1)
        deleted = await self.seed("2", start)
        await self.seed("3", now.timestamp())
        await self.seed("4", datetime(2026, 3, 9, tzinfo=ZoneInfo(self.db.tz)).timestamp())
        await self.seed("5", (now - timedelta(days=40)).timestamp())
        await self.db.delete_key(deleted)
        with patch("app.registration.time.time", return_value=now.timestamp()):
            status = await self.status()
        self.assertEqual(status["enabled"], True)
        self.assertEqual(status["daily_limit"], 0)
        self.assertEqual(status["today_count"], 2)
        self.assertEqual(status["total_count"], 5)
        self.assertEqual(status["day"], "2026-03-08")
        self.assertEqual(status["timezone"], self.db.tz)
        self.assertEqual(len(status["history"]), 30)
        self.assertEqual(status["history"][0], {"day": "2026-02-07", "count": 0})
        self.assertEqual(status["history"][-2:], [
            {"day": "2026-03-07", "count": 1}, {"day": "2026-03-08", "count": 2}])
