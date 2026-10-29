"""Public aggregate statistics use only local SQLite; never generate images."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx

from app import main, public_stats
from app.registration import RegistrationService
from app.config import Settings
from app.state import GateState


class PublicStatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = GateState(Settings(
            data_dir=Path(self.tmp.name), nai_tokens=[], tz="Asia/Shanghai",
            default_daily_v5=777, default_daily_images=888,
            global_daily_v5=150,
        ))
        await self.state.db.connect()
        self.previous_gate = getattr(main.app.state, "gate", None)
        main.app.state.gate = self.state
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://local",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        main.app.state.gate = self.previous_gate
        await self.state.nai.close()
        await self.state.db.close()
        self.tmp.cleanup()

    async def add_generation(self, key_id, images, *, model="nai-diffusion-5-full",
                             kind="image", status="ok", ts=None):
        await self.state.db._db.execute(
            """INSERT INTO usage_log
               (ts,key_id,key_name,kind,model,status,images,detail)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ts if ts is not None else datetime.now().timestamp(), key_id,
             "private-user", kind, model, status, images, "private-prompt"),
        )
        await self.state.db._db.commit()

    async def add_key(self, *, independent=False):
        return await self.state.db.create_key({
            "name": "private-user", "token": "private-key-" + str(independent),
            "daily_images": 100, "daily_v5": 30, "daily_text_tokens": 0,
            "monthly_anlas": 0, "rpm": 5, "image_model_scope": "all",
            "exclude_global_v5": independent,
        })

    async def test_actual_images_include_streams_deleted_and_independent_keys(self):
        shared = await self.add_key()
        independent = await self.add_key(independent=True)
        await self.add_generation(shared["id"], 2,
                                  model="nai-diffusion-5-full-medium")
        await self.add_generation(shared["id"], 3, kind="image_stream",
                                  model="nai-diffusion-5-full-medium-inpainting")
        await self.add_generation(independent["id"], 4, model="nai-v5-full")
        await self.add_generation(None, 1, model="nai-v5")
        await self.add_generation(None, 1, model="nai-diffusion-5")
        for kind in ("upscale", "augment", "encode-vibe", "text"):
            await self.add_generation(shared["id"], 100, kind=kind)
        for status in ("error", "rejected"):
            for kind in ("image", "image_stream"):
                await self.add_generation(shared["id"], 100, kind=kind, status=status)
        for model in ("nai-diffusion-4-5-full", "nai-diffusion-50", "nai-v50", "other"):
            await self.add_generation(shared["id"], 100, model=model)
        await self.state.db.bump_counters(shared["id"], self.state.day(), v5=3, images=5)
        await self.state.db.bump_counters(independent["id"], self.state.day(), v5=4, images=4)
        await self.state.db.reset_daily_image_quota(shared["id"], self.state.day())
        response = await self.client.get("/api/public/stats")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["v5_images_today"], 11)
        self.assertEqual(data["shared_v5_used"], 3)
        self.assertNotEqual(data["v5_images_today"], data["shared_v5_used"])
        await self.state.db.delete_key(shared["id"])
        await self.state.db.delete_key(independent["id"])
        deleted = (await self.client.get("/api/public/stats")).json()
        self.assertEqual(deleted["v5_images_today"], 11)
        self.assertEqual(deleted["shared_v5_used"], 3)

    async def check_calendar_day(self, timezone, instant, expected_day, hours):
        self.state.tz = ZoneInfo(timezone)
        frozen = datetime.fromisoformat(instant).astimezone(self.state.tz)
        start = datetime.combine(frozen.date(), datetime.min.time(), self.state.tz)
        reset = datetime.combine(frozen.date() + timedelta(days=1),
                                 datetime.min.time(), self.state.tz)
        await self.state.db._db.execute("DELETE FROM usage_log")
        for offset, images in ((-0.001, 100), (0, 2),
                               (reset.timestamp() - start.timestamp() - 0.001, 3),
                               (reset.timestamp() - start.timestamp(), 100)):
            await self.add_generation(None, images, ts=start.timestamp() + offset)

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen.astimezone(tz)

        with patch.object(public_stats, "datetime", FrozenDateTime):
            response = await self.client.get("/api/public/stats")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["day"], expected_day)
        self.assertEqual(data["day"], self.state.day(frozen.timestamp()))
        self.assertEqual(data["timezone"], timezone)
        self.assertEqual(data["v5_images_today"], 5)
        self.assertEqual(data["reset_at"], reset.timestamp())
        self.assertEqual((reset.timestamp() - start.timestamp()) / 3600, hours)

    async def test_timezone_day_crosses_utc_midnight(self):
        await self.check_calendar_day("Asia/Shanghai", "2026-10-22T16:30:00+00:00",
                                      "2026-10-23", 24)

    async def test_dst_calendar_days_are_not_assumed_to_be_24_hours(self):
        for instant, day, hours in (("2026-03-08T12:00:00+00:00", "2026-03-08", 23),
                                    ("2026-11-01T12:00:00+00:00", "2026-11-01", 25)):
            with self.subTest(day=day):
                await self.check_calendar_day("America/Los_Angeles", instant, day, hours)

    async def test_runtime_settings_fractional_remaining_and_unlimited(self):
        key = await self.add_key()
        await self.state.db.bump_counters(key["id"], self.state.day(), v5=0.6)
        await self.state.db.set_settings_bulk({
            "global_daily_v5": 6, "v5_medium_multiplier": 0.35,
        })
        data = (await self.client.get("/api/public/stats")).json()
        self.assertEqual(data["shared_v5_limit"], 6)
        self.assertEqual(data["shared_v5_used"], 0.6)
        self.assertEqual(data["shared_v5_remaining"], 5.4)
        self.assertEqual(data["medium_cost"], 0.35)
        await self.state.db.set_setting("global_daily_v5", 0)
        data = (await self.client.get("/api/public/stats")).json()
        self.assertEqual(data["shared_v5_limit"], 0)
        self.assertIsNone(data["shared_v5_remaining"])
        await self.state.db.set_setting("global_daily_v5", 1)
        await self.state.db.bump_counters(key["id"], self.state.day(), v5=0.6)
        self.assertEqual((await self.client.get("/api/public/stats")).json()[
            "shared_v5_remaining"], 0)

    async def test_read_only_no_credentials_no_upstream_and_fixed_public_schema(self):
        key = await self.add_key()
        await self.add_generation(key["id"], 1)
        await self.state.db.set_setting("private-token", "private-setting")
        queries = []
        await self.state.db._db.set_trace_callback(queries.append)

        class ForbiddenUpstream:
            def __getattr__(self, name):
                raise AssertionError("Public stats accessed upstream: " + name)

        outbound = AsyncMock(side_effect=AssertionError("Outbound HTTP forbidden"))
        with patch.object(self.state, "nai", ForbiddenUpstream()), patch.object(
                httpx.AsyncHTTPTransport, "handle_async_request", outbound):
            response = await self.client.get("/api/public/stats")
        outbound.assert_not_called()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()), {
            "day", "timezone", "v5_images_today", "v5_images_scope", "shared_v5_used",
            "shared_v5_limit", "shared_v5_remaining", "shared_v5_unit", "medium_cost",
            "personal_v5_limit", "personal_legacy_limit", "refreshed_at", "reset_at",
        })
        for value in ("private-user", "private-key", "private-prompt", "private-setting",
                      "private-token", "authorization", "key_id", "key_name", "nai_tokens"):
            self.assertNotIn(value, response.text.lower())
        self.assertTrue(queries)
        self.assertTrue(all(query.lstrip().upper().startswith("SELECT") for query in queries))
        self.assertEqual((await self.client.post("/api/public/stats")).status_code, 405)

    async def test_public_personal_limits_match_real_discord_registration(self):
        from app.registration import COMMAND_GUILD, MEMBERSHIP_ROLE

        def discord(request):
            path = request.url.path
            if path == "/api/oauth2/token":
                return httpx.Response(200, json={"access_token": "private-oauth"})
            if path == "/api/users/@me":
                return httpx.Response(200, json={"id": "777"})
            if path.endswith("/member"):
                return httpx.Response(200, json={"roles": [MEMBERSHIP_ROLE]})
            if path == "/api/users/@me/channels":
                return httpx.Response(200, json={"id": "fixture-channel"})
            if path.endswith("/messages"):
                return httpx.Response(200, json={})
            raise AssertionError("Unexpected registration request: " + path)

        async with httpx.AsyncClient(transport=httpx.MockTransport(discord)) as http:
            service = RegistrationService(self.state.db, http, client_id="local-client",
                client_secret="private-client", bot_token="private-bot",
                bridge_secret="private-bridge", redirect_uri="http://local/callback")
            from urllib.parse import parse_qs, urlparse
            link = await service.begin("777", COMMAND_GUILD)
            state = parse_qs(urlparse(link).query)["state"][0]
            self.assertEqual(await service.finish("local-code", state), "sent")
        row = (await self.state.db._db.execute_fetchall(
            "SELECT daily_v5,daily_images FROM api_keys"))[0]
        data = (await self.client.get("/api/public/stats")).json()
        self.assertEqual(data["personal_v5_limit"], row["daily_v5"])
        self.assertEqual(data["personal_legacy_limit"], row["daily_images"])

    async def test_unavailable_state_returns_sanitized_503_and_no_store(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                if missing:
                    del main.app.state.gate
                else:
                    main.app.state.gate = None
                response = await self.client.get("/api/public/stats")
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.json(), {"detail": "Statistics temporarily unavailable"})
        main.app.state.gate = self.state
        with patch.object(self.state.db, "_db", None):
            unconnected = await self.client.get("/api/public/stats")
        self.assertEqual(unconnected.status_code, 503, unconnected.text)
        self.assertEqual(unconnected.headers["cache-control"], "no-store")
        await self.state.db._db.execute("DROP TABLE usage_log")
        unavailable = await self.client.get("/api/public/stats")
        self.assertEqual(unavailable.status_code, 503, unavailable.text)
        self.assertEqual(unavailable.json(), {"detail": "Statistics temporarily unavailable"})
        await self.state.db.close()
        response = await self.client.get("/api/public/stats")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn(str(self.tmp.name), response.text)

    async def test_public_empty_stats_have_registration_limits_and_no_store(self):
        response = await self.client.get("/api/public/stats")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        data = response.json()
        self.assertEqual(data["day"], self.state.day())
        self.assertEqual(data["timezone"], "Asia/Shanghai")
        self.assertEqual(data["v5_images_today"], 0)
        self.assertEqual(data["shared_v5_used"], 0)
        self.assertEqual(data["shared_v5_limit"], 150)
        self.assertEqual(data["shared_v5_remaining"], 150)
        self.assertEqual(data["medium_cost"], 0.6)
        # Discord registration is 30/100, not Settings' admin-key defaults.
        self.assertEqual(data["personal_v5_limit"], 30)
        self.assertEqual(data["personal_legacy_limit"], 100)
        self.assertIsInstance(data["refreshed_at"], (int, float))
        self.assertIsInstance(data["reset_at"], (int, float))
        refreshed = datetime.fromtimestamp(data["refreshed_at"], self.state.tz)
        reset = datetime.fromtimestamp(data["reset_at"], self.state.tz)
        self.assertIsNotNone(refreshed.utcoffset())
        self.assertEqual(reset.hour, 0)
        self.assertGreater(reset, refreshed)
        self.assertEqual(data["v5_images_scope"], "all_keys_including_independent")
        self.assertEqual(data["shared_v5_unit"], "weighted_quota")


if __name__ == "__main__":
    unittest.main()
