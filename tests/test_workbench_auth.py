"""Browser login contract; all Discord traffic is mocked, no NAI requests."""
import asyncio
import base64
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI

from app.database import Database
from app.registration_routes import router as registration_router
from app import workbench_auth as auth


OAUTH_COOKIE = "__Host-nai-gate-wb-oauth"
SESSION_COOKIE = "__Host-nai-gate-wb-session"


class WorkbenchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "gate.sqlite"))
        await self.db.connect()
        self.calls = []
        self.user_id = "777"
        self.fail_discord = False

        def discord(request):
            self.calls.append(request)
            if self.fail_discord:
                raise httpx.ConnectError("private-exception-do-not-echo", request=request)
            if request.url == "https://discord.com/api/oauth2/token":
                return httpx.Response(200, json={"access_token": "private-oauth-token"})
            if request.url == "https://discord.com/api/users/@me":
                self.assertEqual(request.headers["authorization"], "Bearer private-oauth-token")
                return httpx.Response(200, json={"id": self.user_id})
            raise AssertionError("Unexpected network destination")
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(discord))
        self.app = FastAPI()
        self.app.include_router(auth.router)
        self.app.include_router(registration_router)
        self.service = auth.WorkbenchAuthService(self.db, self.http, client_id="client-id",
            client_secret="private-client-secret", bridge_secret="private-bridge-secret",
            redirect_uri="https://gate.example.com/self-register/callback")
        await self.service.initialize()
        self.app.state.workbench_auth = self.service
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                       base_url="https://gate.example.com")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.http.aclose()
        await self.db.close()
        self.tmp.cleanup()

    async def begin(self, client=None):
        client = client or self.client
        response = await client.get("/workbench-auth/discord/start?return_to=https://evil.test")
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response.headers["location"]).query)
        self.assertIn("state", query)
        return query, response

    async def registered_key(self):
        key = await self.db.create_key({"name": "test Discord", "token": "nai-test-existing-key-0123456789",
            "daily_images": 100, "daily_v5": 30, "monthly_anlas": 0,
            "daily_text_tokens": 0, "rpm": 5})
        await self.db._db.execute("INSERT INTO discord_registrations VALUES(?,?,?,?)",
                                 ("777", key["id"], key["token"], time.time()))
        await self.db._db.commit()
        return key

    async def finish(self, state, **params):
        return await self.client.get("/self-register/callback", params={"state": state,
            "code": "private-code", **params})

    async def snapshot(self):
        return {table: [tuple(r) for r in await self.db._db.execute_fetchall("SELECT * FROM " + table)]
                for table in ("api_keys", "discord_registrations", "counters", "usage_log",
                              "daily_quota_offsets", "discord_daily_charges", "discord_daily_vibes")}

    async def test_existing_key_login_persists_opaque_session_without_mutating_key_or_quota(self):
        key = await self.registered_key()
        await self.db.bump_counters(key["id"], "2026-10-25", v5=12, images=4)
        before = await self.snapshot()
        query, _ = await self.begin()
        state = query["state"][0]
        response = await self.finish(state)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "https://gate.example.com/workbench/")
        cookie = response.headers.get_list("set-cookie")
        self.assertTrue(any(SESSION_COOKIE in c and "HttpOnly" in c and "Secure" in c
                            and "SameSite=lax" in c and "Path=/" in c and "Max-Age=604800" in c for c in cookie))
        session = self.client.cookies.get(SESSION_COOKIE)
        self.assertEqual(len(base64.urlsafe_b64decode(session + "=")), 32)
        rows = await self.db._db.execute_fetchall("SELECT * FROM workbench_sessions")
        self.assertEqual(len(rows), 1)
        self.assertNotIn(session, dict(rows[0]).values())
        self.assertNotIn(key["token"], dict(rows[0]).values())
        self.assertEqual((await self.client.get("/workbench-auth/session")).json(), {"authenticated": True})
        self.assertEqual(await self.snapshot(), before)
        self.assertEqual([r.url.path for r in self.calls], ["/api/oauth2/token", "/api/users/@me"])
        exchange = parse_qs(self.calls[0].content.decode())
        challenge = base64.urlsafe_b64encode(hashlib.sha256(exchange["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, query["code_challenge"][0])
        self.assertEqual(exchange["redirect_uri"], query["redirect_uri"])
        self.assertNotIn(key["token"], str(response.headers) + response.text)
        self.assertIsNone(self.client.cookies.get(OAUTH_COOKIE))

    async def assert_login_error(self, expected):
        before = await self.snapshot()
        query, _ = await self.begin()
        response = await self.finish(query["state"][0])
        self.assertEqual(response.headers["location"],
                         "https://gate.example.com/workbench/?login_error=" + expected)
        self.assertIsNone(self.client.cookies.get(SESSION_COOKIE))
        self.assertEqual(await self.snapshot(), before)

    async def test_unregistered_discord_is_rejected_without_key_creation(self):
        await self.assert_login_error("not_registered")

    async def test_reclaimed_key_is_rejected_without_restore(self):
        key = await self.registered_key()
        await self.db.delete_key(key["id"])
        await self.assert_login_error("key_reclaimed")

    async def test_disabled_key_is_rejected_without_reenable(self):
        key = await self.registered_key()
        await self.db.update_key(key["id"], {"enabled": False})
        await self.assert_login_error("key_disabled")

    async def test_expired_key_is_rejected_without_renewal(self):
        key = await self.registered_key()
        await self.db.update_key(key["id"], {"expires_at": time.time() - 1})
        await self.assert_login_error("key_expired")

    async def login(self):
        query, _ = await self.begin()
        response = await self.finish(query["state"][0])
        self.assertEqual(response.headers["location"], "https://gate.example.com/workbench/")
        return self.client.cookies.get(SESSION_COOKIE)

    async def test_private_resolve_requires_separate_secret_and_only_then_returns_original_key(self):
        key = await self.registered_key()
        token = await self.login()
        body = {"session_token": token}
        for secret in (None, "", "Bearer wrong", "Bearer registration-bridge-secret", "Bearer ü"):
            headers = {} if secret is None else {"Authorization": secret}
            # Non-ASCII header bytes exercise fail-closed handling too.
            if "ü" in (secret or ""):
                headers = {b"authorization": secret.encode("utf-8")}
            response = await self.client.post("/workbench-auth/resolve", json=body, headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertNotIn(key["token"], response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
        headers = {"Authorization": "Bearer private-bridge-secret"}
        response = await self.client.post("/workbench-auth/resolve", json=body, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"key": key["token"]})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual((await self.client.get("/workbench-auth/session")).json(), {"authenticated": True})

    async def test_private_resolve_strict_json_shape_types_and_4096_byte_limit(self):
        headers = {"Authorization": "Bearer private-bridge-secret"}
        for data in ({}, {"session_token": None}, {"session_token": 1}, {"session_token": True},
                     {"session_token": []}, {"session_token": "x"}, {"session_token": "x" * 44},
                     {"session_token": "x" * 43, "extra": 1}, ["x"], "bad"):
            response = await self.client.post("/workbench-auth/resolve", json=data, headers=headers)
            self.assertEqual(response.status_code, 400, repr(data))
        response = await self.client.post("/workbench-auth/resolve", content=b"x" * 4097, headers=headers)
        self.assertEqual(response.status_code, 413)
        response = await self.client.post("/workbench-auth/resolve", content=b"{", headers=headers)
        self.assertEqual(response.status_code, 400)
        # A malformed/oversized body must not bypass bridge authentication.
        response = await self.client.post("/workbench-auth/resolve", content=b"x" * 4097)
        self.assertEqual(response.status_code, 401)
        response = await self.client.post("/workbench-auth/resolve", json={"session_token": "x" * 43}, headers=headers)
        self.assertEqual(response.status_code, 401)

    async def test_logout_requires_exact_origin_csrf_and_post_then_revokes_idempotently(self):
        await self.registered_key()
        token = await self.login()
        path = "/workbench-auth/logout"
        for headers in ({}, {"Origin": "https://evil.test", "X-Workbench-CSRF": "1"},
                        {"Origin": "https://gate.example.com"}, {"Origin": "https://gate.example.com", "X-Workbench-CSRF": "0"},
                        {"Origin": "https://gate.example.com.evil.test", "X-Workbench-CSRF": "1"},
                        {"Origin": "null", "X-Workbench-CSRF": "1"}):
            response = await self.client.post(path, headers=headers)
            self.assertEqual(response.status_code, 403)
            self.assertEqual((await self.client.get("/workbench-auth/session")).status_code, 200)
        headers = {"Origin": "https://gate.example.com", "X-Workbench-CSRF": "1"}
        self.assertEqual((await self.client.get(path, headers=headers)).status_code, 405)
        response = await self.client.post(path, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIsNone(self.client.cookies.get(SESSION_COOKIE))
        self.assertEqual((await self.client.get("/workbench-auth/session")).status_code, 401)
        self.assertEqual((await self.client.post(path, headers=headers)).status_code, 200)
        response = await self.client.post("/workbench-auth/resolve", json={"session_token": token},
            headers={"Authorization": "Bearer private-bridge-secret"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual((await self.db._db.execute_fetchall("SELECT count(*) FROM workbench_sessions"))[0][0], 0)

    async def test_key_rotation_disable_expiry_and_binding_changes_irreversibly_revoke_sessions(self):
        key = await self.registered_key()
        cases = [
            ("UPDATE api_keys SET token='nai-new-token' WHERE id=?", "UPDATE api_keys SET token=? WHERE id=?", (key["token"], key["id"])),
            ("UPDATE api_keys SET enabled=0 WHERE id=?", "UPDATE api_keys SET enabled=1 WHERE id=?", (key["id"],)),
            ("UPDATE api_keys SET expires_at=1 WHERE id=?", "UPDATE api_keys SET expires_at=NULL WHERE id=?", (key["id"],)),
            ("UPDATE discord_registrations SET key_id=9999 WHERE key_id=?", "UPDATE discord_registrations SET key_id=? WHERE discord_id='777'", (key["id"],)),
            ("UPDATE discord_registrations SET token='nai-changed-binding' WHERE key_id=?", "UPDATE discord_registrations SET token=? WHERE discord_id='777'", (key["token"],)),
            ("UPDATE discord_registrations SET discord_id='888' WHERE key_id=?", "UPDATE discord_registrations SET discord_id='777' WHERE discord_id='888'", ()),
        ]
        for change, undo, undo_args in cases:
            with self.subTest(change=change):
                token = await self.login()
                await self.db._db.execute(change, (key["id"],))
                await self.db._db.commit()
                await self.db._db.execute(undo, undo_args)
                await self.db._db.commit()
                response = await self.client.post("/workbench-auth/resolve", json={"session_token": token},
                    headers={"Authorization": "Bearer private-bridge-secret"})
                self.assertEqual(response.status_code, 401)
                self.assertEqual((await self.client.get("/workbench-auth/session")).status_code, 401)

    async def test_deleting_binding_cannot_revive_session_by_reinserting_same_binding(self):
        key = await self.registered_key()
        await self.login()
        await self.db._db.execute("DELETE FROM discord_registrations WHERE discord_id='777'")
        await self.db._db.execute("INSERT INTO discord_registrations VALUES(?,?,?,?)",
                                 ("777", key["id"], key["token"], time.time()))
        await self.db._db.commit()
        self.assertEqual((await self.client.get("/workbench-auth/session")).status_code, 401)

    async def test_deleting_key_removes_all_bound_sessions(self):
        key = await self.registered_key()
        await self.login()
        await self.db.delete_key(key["id"])
        self.assertEqual((await self.db._db.execute_fetchall("SELECT count(*) FROM workbench_sessions"))[0][0], 0)
        self.assertEqual((await self.client.get("/workbench-auth/session")).status_code, 401)

    async def test_lifespan_configures_login_without_registration_bot_or_bridge(self):
        import os
        from app import main
        from app.config import Settings
        env = {"DISCORD_CLIENT_ID": "client-id", "DISCORD_CLIENT_SECRET": "client-secret",
               "DISCORD_REDIRECT_URI": "https://gate.example.com/self-register/callback",
               "WORKBENCH_AUTH_BRIDGE_SECRET": "separate-secret"}
        settings = Settings(data_dir=Path(self.tmp.name), nai_tokens=[], seed_demo_key=False,
                            admin_password="test", key_inactivity_delete_days=0)
        app = FastAPI()
        with patch.dict(os.environ, env, clear=True), patch.object(main, "SETTINGS", settings), patch.object(main, "STATE", None), \
                patch("app.nai.NaiClient.start", new_callable=AsyncMock), patch("app.nai.NaiClient.close", new_callable=AsyncMock):
            async with main.lifespan(app):
                self.assertIsNone(app.state.registrar)
                self.assertIsInstance(getattr(app.state, "workbench_auth", None), auth.WorkbenchAuthService)
                self.assertEqual(app.state.workbench_auth.bridge_secret, "separate-secret")
                self.assertEqual(app.state.workbench_auth.redirect_uri, env["DISCORD_REDIRECT_URI"])
                self.assertIs(app.state.workbench_auth.db, app.state.gate.db)
                await app.state.workbench_auth.begin()
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(auth.configured_service(self.db, self.http))
        with patch.dict(os.environ, {"DISCORD_CLIENT_ID": "client", "DISCORD_CLIENT_SECRET": "secret"}, clear=True):
            self.assertEqual(auth.configured_service(self.db, self.http).redirect_uri,
                             "https://gate.example.com/self-register/callback")

    async def test_session_storage_has_per_account_and_global_bounds(self):
        await self.registered_key()
        tokens = []
        with patch.object(auth, "MAX_USER_SESSIONS", 2):
            for _ in range(3):
                self.client.cookies.clear()
                tokens.append(await self.login())
        self.assertEqual((await self.db._db.execute_fetchall("SELECT count(*) FROM workbench_sessions"))[0][0], 2)
        with self.assertRaises(auth.AuthError):
            await self.service.resolve(tokens[0])
        await self.service.resolve(tokens[1])
        await self.service.resolve(tokens[2])
        with patch.object(auth, "MAX_SESSIONS", 2):
            self.client.cookies.clear()
            query, _ = await self.begin()
            response = await self.finish(query["state"][0])
            self.assertTrue(response.headers["location"].endswith("login_error=service_unavailable"))
        self.assertEqual((await self.db._db.execute_fetchall("SELECT count(*) FROM workbench_sessions"))[0][0], 2)
        await self.service.resolve(tokens[1])

    async def test_auth_method_errors_and_unknown_routes_are_never_cacheable(self):
        from app.main import app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gate.example.com") as client:
            for method, path in (("GET", "/workbench-auth/logout"), ("GET", "/workbench-auth/resolve"),
                                 ("POST", "/workbench-auth/session"), ("GET", "/workbench-auth/unknown")):
                response = await client.request(method, path)
                self.assertIn(response.status_code, (404, 405))
                self.assertEqual(response.headers.get("cache-control"), "no-store")
                self.assertEqual(response.headers.get("referrer-policy"), "no-referrer")

    async def test_error_redirect_uses_closed_whitelist(self):
        response = auth.redirect_error("private-code&return_to=https://evil.test")
        self.assertEqual(response.headers["location"], "https://gate.example.com/workbench/?login_error=auth_failed")

    async def test_nonce_browser_binding_rejects_login_csrf_without_consuming_victim_state(self):
        await self.registered_key()
        query, _ = await self.begin()
        state = query["state"][0]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://gate.example.com") as attacker:
            for cookie in (None, OAUTH_COOKIE + "=" + "x" * 43):
                headers = {} if cookie is None else {"Cookie": cookie}
                response = await attacker.get("/self-register/callback", params={"state": state, "code": "private-code"}, headers=headers)
                self.assertTrue(response.headers["location"].endswith("login_error=state_invalid"))
                self.assertEqual(self.calls, [])
        response = await self.finish(state)
        self.assertEqual(response.headers["location"], "https://gate.example.com/workbench/")

    async def test_state_and_nonce_expiry_replay_and_cancel_never_exchange_code(self):
        await self.registered_key()
        for case in ("expired", "cancel", "missing_code", "forged"):
            with self.subTest(case=case):
                query, _ = await self.begin()
                state = query["state"][0]
                nonce = self.client.cookies.get(OAUTH_COOKIE)
                if case == "expired":
                    await self.db._db.execute("UPDATE workbench_oauth_states SET expires_at=1")
                    await self.db._db.commit()
                params = {"error": "private-cancel-description"} if case == "cancel" else {}
                if case == "missing_code":
                    params["code"] = ""
                supplied = "wb." + "x" * 43 if case == "forged" else state
                response = await self.finish(supplied, **params)
                expected = "auth_failed" if case in ("cancel", "missing_code") else "state_invalid"
                self.assertTrue(response.headers["location"].endswith("login_error=" + expected))
                self.assertNotIn("private-", response.text + str(response.headers))
                self.assertIsNone(self.client.cookies.get(OAUTH_COOKIE))
                if case in ("cancel", "missing_code"):
                    response = await self.client.get("/self-register/callback", params={"state": state, "code": "private-code"},
                                                     headers={"Cookie": OAUTH_COOKIE + "=" + nonce})
                    self.assertTrue(response.headers["location"].endswith("login_error=state_invalid"))
        self.assertEqual(self.calls, [])

    async def test_concurrent_callback_consumes_state_exactly_once_even_with_original_nonce(self):
        await self.registered_key()
        query, _ = await self.begin()
        nonce = self.client.cookies.get(OAUTH_COOKIE)
        params = {"state": query["state"][0], "code": "private-code"}
        responses = await asyncio.gather(*(self.client.get("/self-register/callback", params=params,
            headers={"Cookie": OAUTH_COOKIE + "=" + nonce}) for _ in range(6)))
        locations = [r.headers["location"] for r in responses]
        self.assertEqual(locations.count("https://gate.example.com/workbench/"), 1)
        self.assertEqual(sum(x.endswith("login_error=state_invalid") for x in locations), 5)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual((await self.db._db.execute_fetchall("SELECT count(*) FROM workbench_sessions"))[0][0], 1)
        replay = await self.client.get("/self-register/callback", params=params, headers={"Cookie": OAUTH_COOKIE + "=" + nonce})
        self.assertTrue(replay.headers["location"].endswith("login_error=state_invalid"))
        self.assertEqual(len(self.calls), 2)

    async def test_state_and_session_survive_service_restart_and_current_cookie_is_rotated(self):
        await self.registered_key()
        old_session = await self.login()
        query, _ = await self.begin()
        self.service = auth.WorkbenchAuthService(self.db, self.http, client_id="client-id",
            client_secret="private-client-secret", bridge_secret="private-bridge-secret",
            redirect_uri="https://gate.example.com/self-register/callback")
        await self.service.initialize()
        self.app.state.workbench_auth = self.service
        await self.service.resolve(old_session)
        response = await self.finish(query["state"][0])
        self.assertEqual(response.headers["location"], "https://gate.example.com/workbench/")
        self.assertNotEqual(old_session, self.client.cookies.get(SESSION_COOKIE))
        with self.assertRaises(auth.AuthError):
            await self.service.resolve(old_session)
        await self.service.resolve(self.client.cookies.get(SESSION_COOKIE))

    async def test_discord_identity_is_exact_not_a_name_or_coerced_numeric_value(self):
        await self.registered_key()
        for identity in (777, "0777", "777 ", "777.0", None, "test Discord"):
            with self.subTest(identity=identity):
                self.user_id = identity
                query, _ = await self.begin()
                response = await self.finish(query["state"][0])
                expected = "not_registered" if identity == "0777" else "auth_failed"
                self.assertTrue(response.headers["location"].endswith("login_error=" + expected))
                self.assertIsNone(self.client.cookies.get(SESSION_COOKIE))

    async def test_oauth_errors_are_sanitized_and_state_stays_consumed(self):
        await self.registered_key()
        for payload, status in (({}, 200), ({"access_token": []}, 200), ({"access_token": "secret"}, 400)):
            query, _ = await self.begin()
            nonce = self.client.cookies.get(OAUTH_COOKIE)
            with patch.object(self.http, "post", AsyncMock(return_value=httpx.Response(status, json=payload))):
                response = await self.finish(query["state"][0])
            self.assertTrue(response.headers["location"].endswith("login_error=auth_failed"))
            with self.assertRaises(auth.AuthError):
                await self.service.consume_state(query["state"][0], nonce)
        self.fail_discord = True
        query, _ = await self.begin()
        response = await self.finish(query["state"][0])
        self.assertTrue(response.headers["location"].endswith("login_error=service_unavailable"))
        self.assertNotIn("private-", response.text + str(response.headers))

    async def test_session_expiration_invalid_cookie_and_key_rotation_fail_closed(self):
        key = await self.registered_key()
        token = await self.login()
        await self.db._db.execute("UPDATE workbench_sessions SET expires_at=1")
        await self.db._db.commit()
        self.assertEqual((await self.client.get("/workbench-auth/session")).status_code, 401)
        self.assertIsNone(self.client.cookies.get(SESSION_COOKIE))
        with self.assertRaises(auth.AuthError):
            await self.service.resolve(token)
        for token in (None, 1, "", "x", "x" * 44, "../" + "x" * 40, "x" * 43):
            with self.subTest(token=token), self.assertRaises(auth.AuthError):
                await self.service.resolve(token)
        await self.login()
        await self.db.rotate_key_token(key["id"], "nai-rotated-current-key")
        self.assertEqual((await self.client.get("/workbench-auth/session")).status_code, 401)
        # A new login uses the live key, not the stale recovery token in the binding.
        token = await self.login()
        self.assertEqual(await self.service.resolve(token), "nai-rotated-current-key")

    async def test_unauthorized_bridge_request_never_reaches_session_lookup(self):
        with patch.object(self.service, "resolve", new_callable=AsyncMock) as resolve:
            response = await self.client.post("/workbench-auth/resolve", json={"session_token": "x" * 43})
            self.assertEqual(response.status_code, 401)
            resolve.assert_not_awaited()
        self.service.bridge_secret = ""
        response = await self.client.post("/workbench-auth/resolve", json={"session_token": "x" * 43},
                                          headers={"Authorization": "Bearer "})
        self.assertEqual(response.status_code, 401)

    async def test_state_capacity_and_expired_cleanup_are_atomic(self):
        with patch.object(auth, "MAX_STATES", 1):
            await self.begin()
            response = await self.client.get("/workbench-auth/discord/start")
            self.assertTrue(response.headers["location"].endswith("login_error=service_unavailable"))
            await self.db._db.execute("UPDATE workbench_oauth_states SET expires_at=1")
            await self.db._db.commit()
            await self.begin()
        self.assertEqual((await self.db._db.execute_fetchall("SELECT count(*) FROM workbench_oauth_states"))[0][0], 1)

    async def test_auth_transaction_does_not_commit_shared_gate_connection(self):
        await self.db._db.execute("INSERT INTO site_settings VALUES('uncommitted-auth-test','1')")
        self.assertTrue(self.db._db.in_transaction)
        task = asyncio.create_task(self.service.begin())
        await asyncio.sleep(0.03)
        self.assertFalse(task.done())
        self.assertTrue(self.db._db.in_transaction)
        await self.db._db.commit()
        await task
        self.assertEqual(await self.db.get_setting("uncommitted-auth-test"), "1")

    async def test_non_workbench_callback_still_dispatches_to_registration_service(self):
        class Registrar:
            finish = AsyncMock(return_value="sent")
        self.app.state.registrar = Registrar()
        response = await self.client.get("/self-register/callback?state=legacy-state&code=legacy-code")
        self.assertEqual(response.status_code, 200)
        self.app.state.registrar.finish.assert_awaited_once_with("legacy-code", "legacy-state")
        self.assertIn("API Key", response.text)
        query, _ = await self.begin()
        response = await self.finish(query["state"][0], error="access_denied")
        self.assertEqual(response.status_code, 302)
        self.app.state.registrar.finish.assert_awaited_once()

    async def test_start_sets_browser_nonce_identify_only_and_persistent_pkce(self):
        query, response = await self.begin()
        self.assertEqual(urlparse(response.headers["location"]).netloc, "discord.com")
        self.assertEqual(query["scope"], ["identify"])
        self.assertEqual(query["redirect_uri"], ["https://gate.example.com/self-register/callback"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertTrue(query["state"][0].startswith("wb."))
        cookie = response.headers["set-cookie"]
        for part in (OAUTH_COOKIE, "HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=600"):
            self.assertIn(part, cookie)
        self.assertNotIn("Domain=", cookie)
        rows = await self.db._db.execute_fetchall("SELECT * FROM workbench_oauth_states")
        self.assertEqual(len(rows), 1)
        row = dict(rows[0])
        self.assertNotIn(self.client.cookies.get(OAUTH_COOKIE), row.values())
        self.assertNotIn(query["state"][0], row.values())
        challenge = base64.urlsafe_b64encode(hashlib.sha256(row["verifier"].encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(query["code_challenge"], [challenge])
        self.assertTrue(time.time() < row["expires_at"] <= time.time() + 600)
        self.assertEqual(self.calls, [])


class WorkbenchUnavailableTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_without_configuration_fails_closed_with_safe_redirect(self):
        from app.main import app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://gate.example.com") as client:
            response = await client.get("/workbench-auth/discord/start?return_to=https://evil.test")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"],
                         "https://gate.example.com/workbench/?login_error=service_unavailable")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
