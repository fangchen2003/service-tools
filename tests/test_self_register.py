"""No billable upstream calls: Discord /register enrollment contract (issue_direct is the only provisioning path)."""
import re
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI

from app.database import Database
from app.registration import RegistrationService, RegistrationError
from app.registration_routes import router

GUILD = "1480185480048808009"        # 发 /register 的服务器
MEMBER_PATH = re.compile(r"^/api/guilds/(\d+)/members/\d+$")     # 机器人 Token 查成员身份组


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "gate.sqlite"))
        await self.db.connect()
        self.calls = []
        self.roles = ["1335363403870502912"]
        self.other_roles = None

        def discord(request):
            self.calls.append((request.method, request.url.path))
            member = MEMBER_PATH.match(request.url.path)
            if member and request.method == "GET":
                assert request.headers["authorization"] == "Bot fake-bot-token"     # 身份组门槛用机器人 Token 核验
                if member.group(1) == "1134557553011998840":
                    return httpx.Response(200, json={"roles": self.roles})
                if member.group(1) == "222222222222222222":                         # 别的社区
                    return httpx.Response(200, json={"roles": self.other_roles}) if self.other_roles is not None \
                        else httpx.Response(404, json={})
            if request.url.path == "/api/users/@me/channels":
                return httpx.Response(200, json={"id": "dm-1"})
            if request.url.path == "/api/channels/dm-1/messages":
                return httpx.Response(200, json={})
            raise AssertionError(f"Unexpected Discord request {request.method} {request.url}")
        await self.db.set_setting("register_open", "1")        # registration is closed by default (safe default)
        await self.db.set_setting("waitlist_enabled", "1")     # 候补默认关；这里的候补用例显式打开
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(discord), base_url="https://discord.com")
        self.service = RegistrationService(self.db, self.http, client_id="client-id", client_secret="client-secret",
            bot_token="fake-bot-token", bridge_secret="bridge-secret")

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.db.close()
        self.tmp.cleanup()

    async def mint(self, user="777"):
        return await self.service.issue_direct(user, GUILD)

    async def count(self, table):
        return (await self.db._db.execute_fetchall(f"SELECT count(*) FROM {table}"))[0][0]

    async def test_valid_role_receives_key_in_reply_once(self):
        result = await self.mint()
        self.assertIn("nai-", result["key"])
        self.assertIn(result["key"], result["message"])
        self.assertIn("https://novelai.fangchen2003.asia/", result["message"])
        self.assertIn(("GET", "/api/guilds/1134557553011998840/members/777"), self.calls)   # 机器人核验身份组
        self.assertEqual(await self.count("api_keys"), 1)
        row = (await self.db._db.execute_fetchall("SELECT daily_v5,daily_images,allow_anlas,allow_img2img,exclude_global_v5,image_model_scope FROM api_keys"))[0]
        self.assertEqual(tuple(row), (50, 100, 0, 0, 0, "all"))
        self.assertEqual((await self.db._db.execute_fetchall("SELECT discord_id FROM discord_registrations"))[0][0], "777")
        self.assertNotIn(("POST", "/api/channels/dm-1/messages"), self.calls)   # Key 只在临时回复里，不私信
        with self.assertRaises(RegistrationError):
            await self.mint()                                                   # 已领过
        self.assertEqual(await self.count("api_keys"), 1)

    async def test_wrong_server_and_role_never_mint(self):
        with self.assertRaises(RegistrationError):
            await self.service.issue_direct("777", "1134557553011998840")
        self.roles = []
        with self.assertRaisesRegex(RegistrationError, "身份组"):
            await self.mint()
        self.assertEqual(await self.count("api_keys"), 0)

    async def test_admin_role_gate_in_other_community(self):
        from app import ops
        await ops.set_registration(self.db, {"role_guild": "222222222222222222", "role_id": "1461731450058575986",
                                             "role_note": "创作者"})
        with self.assertRaisesRegex(RegistrationError, "先加入"):           # 不在那个服务器
            await self.mint()
        self.other_roles = ["1"]
        with self.assertRaisesRegex(RegistrationError, "创作者"):           # 在服务器但没有身份组
            await self.mint()
        self.assertEqual(await self.count("api_keys"), 0)
        self.other_roles = ["1461731450058575986"]
        self.assertIn("nai-", (await self.mint())["key"])
        self.assertIn(("GET", "/api/guilds/222222222222222222/members/777"), self.calls)
        with self.assertRaises(ValueError):
            await ops.set_registration(self.db, {"role_id": "abc"})

    async def test_issue_direct_mints_key_without_oauth_or_dm(self):
        # 生产同款：无身份组门槛 → /register 直发 Key，不走 OAuth、不私信
        self.service.membership_role = ""
        result = await self.service.issue_direct("777", "1480185480048808009", username="neo", global_name="Neo")
        self.assertIn("nai-", result["key"])
        self.assertIn(result["key"], result["message"])
        self.assertIn("猫头鹰", result["message"])
        self.assertEqual((await self.db._db.execute_fetchall("SELECT count(*) FROM api_keys"))[0][0], 1)
        self.assertEqual(tuple((await self.db._db.execute_fetchall(
            "SELECT discord_id,display_name FROM discord_registrations"))[0]), ("777", "Neo"))
        self.assertNotIn(("POST", "/api/channels/dm-1/messages"), self.calls)   # 不私信
        with self.assertRaises(RegistrationError):                              # 已领过会被去重拦下
            await self.service.issue_direct("777", "1480185480048808009")
        with self.assertRaises(RegistrationError):                              # 非指定服务器拒绝
            await self.service.issue_direct("888", "1134557553011998840")

    async def test_issue_direct_hourly_rate_limit(self):
        # 每小时领取总数硬上限：抹平授权突增（曾触发 Discord 680009）；0 = 关闭
        self.service.membership_role = ""
        await self.db.set_setting("issue_hourly_cap", "2")
        await self.service.issue_direct("777", "1480185480048808009")
        await self.service.issue_direct("778", "1480185480048808009")
        with self.assertRaises(RegistrationError) as cm:
            await self.service.issue_direct("779", "1480185480048808009")
        self.assertIn("限流", str(cm.exception))
        await self.db.set_setting("issue_hourly_cap", "0")                       # 关闭后恢复
        self.assertIn("nai-", (await self.service.issue_direct("779", "1480185480048808009"))["key"])

    async def test_http_issue_requires_bridge_secret_and_oauth_routes_are_gone(self):
        app = FastAPI()
        app.include_router(router)
        app.state.registrar = self.service
        body = {"discord_id": "777", "guild_id": GUILD}
        auth = {"Authorization": "Bearer bridge-secret"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://novelai.fangchen2003.asia") as client:
            self.assertEqual((await client.post("/self-register/issue", json=body)).status_code, 401)
            allowed = await client.post("/self-register/issue", headers=auth, json=body)
            self.assertEqual(allowed.status_code, 200)
            self.assertIn("nai-", allowed.json()["key"])
            self.assertEqual(allowed.headers["cache-control"], "no-store")
            # 旧的 OAuth 领取流程（/intent → Discord 授权 → /callback 私信发 Key）已删除，不能再恢复
            self.assertIn((await client.post("/self-register/intent", headers=auth, json=body)).status_code, (404, 405))
            self.assertIn((await client.get("/self-register/callback", params={"code": "c", "state": "s"})).status_code,
                          (404, 405))
        self.assertFalse(hasattr(self.service, "begin") or hasattr(self.service, "finish"))


class ConfiguredServiceTests(unittest.IsolatedAsyncioTestCase):
    def _env(self, **extra):
        base = dict(DISCORD_CLIENT_ID="c", DISCORD_CLIENT_SECRET="s", DISCORD_BOT_TOKEN="b",
                    REGISTRATION_BRIDGE_SECRET="x" * 32, DISCORD_GUILD_ID="123", SITE_URL="https://gate.example.com")
        base.update(extra)
        return base

    async def test_disabled_unless_fully_configured(self):
        from unittest.mock import patch
        from app.registration import configured_service
        env = self._env()
        del env["SITE_URL"]
        with patch.dict("os.environ", env, clear=True):
            self.assertIsNone(configured_service(None, None))

    async def test_env_values_drive_guild_role_site_and_key_defaults(self):
        from unittest.mock import patch
        from app.registration import configured_service
        with patch.dict("os.environ", self._env(DISCORD_ROLE_ID="999"), clear=True):
            svc = configured_service(None, None)
        self.assertEqual((svc.command_guild, svc.membership_guild, svc.membership_role), ("123", "123", "999"))
        self.assertEqual(svc.site_url, "https://gate.example.com/")
        self.assertEqual((svc.key_daily_images, svc.key_daily_v5, svc.key_image_scope, svc.key_expires_days),
                         (30, 0, "legacy", 30))
        with self.assertRaises(RegistrationError):
            await svc.issue_direct("777", GUILD)            # the original author's guild is not accepted


class CapacityAndAdminTests(RegistrationTests):
    """Reuses the Discord mock from RegistrationTests (inherited tests re-run harmlessly)."""

    async def mint(self, user="777"):
        self.service.max_users = 0
        return await self.service.issue_direct(user, GUILD)

    async def test_capacity_blocks_new_registrations_and_revoke_frees_slot(self):
        await self.mint()
        self.assertEqual(await self.service.count_active(), 1)
        self.service.max_users = 1
        with self.assertRaises(RegistrationError):
            await self.service.issue_direct("888", GUILD)
        self.assertTrue(await self.service.revoke("777"))
        self.assertEqual(await self.service.count_active(), 0)
        self.assertIn("nai-", (await self.service.issue_direct("888", GUILD))["key"])   # slot is free again
        self.assertEqual(await self.service.count_active(), 1)
        self.assertFalse(await self.service.revoke("777"))

    async def test_reset_all_removes_keys_and_registrations_so_users_can_return(self):
        await self.mint()
        self.assertEqual(await self.service.reset_all(), 1)
        self.assertEqual((await self.db._db.execute_fetchall("SELECT count(*) FROM api_keys"))[0][0], 0)
        self.assertEqual((await self.db._db.execute_fetchall("SELECT count(*) FROM discord_registrations"))[0][0], 0)
        self.assertIn("nai-", (await self.mint())["key"])

    async def test_quota_and_resetkey_endpoints_need_secret_and_right_guild(self):
        await self.mint()
        app = FastAPI()
        app.include_router(router)
        app.state.registrar = self.service
        from types import SimpleNamespace
        app.state.gate = SimpleNamespace(db=self.db, day=lambda: "2026-10-09")
        auth = {"Authorization": "Bearer bridge-secret"}
        body = {"discord_id": "777", "guild_id": "1480185480048808009"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://x") as client:
            self.assertEqual((await client.post("/self-register/quota", json=body)).status_code, 401)
            self.assertEqual((await client.post("/self-register/quota", headers=auth,
                json={**body, "guild_id": "1"})).status_code, 403)
            quota = await client.post("/self-register/quota", headers=auth, json=body)
            self.assertEqual(quota.status_code, 200)
            self.assertEqual(quota.json()["daily_images"], 100)
            old = (await self.db._db.execute_fetchall("SELECT token FROM api_keys"))[0][0]
            new = (await client.post("/self-register/resetkey", headers=auth, json=body)).json()["key"]
            self.assertNotEqual(old, new)
            self.assertIsNone(await self.db.get_key_by_token(old))
            self.assertIsNotNone(await self.db.get_key_by_token(new))
            self.assertEqual((await client.post("/self-register/quota", headers=auth,
                json={**body, "discord_id": "999"})).status_code, 404)


class OpenRegistrationTests(RegistrationTests):
    """First-come-first-served registration: no role needed, expired members rotate out, young accounts blocked."""

    async def test_no_role_required_when_role_is_empty(self):
        self.service.membership_role = ""
        self.roles = []
        self.assertIn("nai-", (await self.mint())["key"])
        self.assertFalse(any(MEMBER_PATH.match(p) for _, p in self.calls))     # 没有门槛就不查身份组

    async def test_expired_key_frees_slot_and_member_can_register_again(self):
        self.service.max_users = 1
        await self.mint()
        self.assertEqual(await self.service.count_active(), 1)
        with self.assertRaises(RegistrationError):
            await self.service.issue_direct("888", GUILD)               # full
        await self.db._db.execute("UPDATE api_keys SET expires_at=?", (time.time() - 5,))
        await self.db._db.commit()
        self.assertEqual(await self.service.count_active(), 0)           # expired key no longer holds the slot
        self.assertEqual([w["discord_id"] for w in await self.service.waitlist()], ["888"])   # 名额满时进了候补
        with self.assertRaises(RegistrationError) as caught:
            await self.service.issue_direct("777", GUILD)               # 候补排在前面，后来者不能插队
        self.assertIn("第 2 位", str(caught.exception))
        self.assertIn("nai-", (await self.service.issue_direct("888", GUILD))["key"])   # 排第一的候补可以领
        self.assertEqual([w["discord_id"] for w in await self.service.waitlist()], ["777"])   # 领到后移出候补
        self.assertEqual(await self.service.count_active(), 1)

    async def test_waitlist_invites_in_order_and_holds_slot_24h(self):
        from unittest.mock import AsyncMock
        from app.registration import WAITLIST_HOLD
        self.service.max_users = 1
        self.service.send_dm = AsyncMock(return_value=True)
        await self.db.set_setting("waitlist_dm", "1")          # 这个测试测的是私信邀请模式（默认已改为不私信）
        await self.mint()
        for who in ("901", "902"):
            with self.assertRaises(RegistrationError):
                await self.service.issue_direct(who, GUILD, name="u" + who)
        self.assertEqual([w["name"] for w in await self.service.waitlist()], ["u901", "u902"])
        self.assertEqual(await self.service.invite_waitlist(), 0)                     # 没有空位
        await self.db._db.execute("UPDATE api_keys SET expires_at=?", (time.time() - 5,))
        await self.db._db.commit()
        self.assertEqual(await self.service.invite_waitlist(), 1)                     # 只邀请第一位
        self.assertEqual(self.service.send_dm.await_args.args[0], "901")
        with self.assertRaises(RegistrationError):
            await self.service.issue_direct("903", GUILD)                             # 名额为 901 保留
        await self.service._check_capacity("901")                                     # 901 可以领（这里只探测，不真领）
        later = time.time() + WAITLIST_HOLD + 1
        self.assertEqual(await self.service.invite_waitlist(now=later), 1)            # 901 过期 → 邀请 902
        self.assertEqual(self.service.send_dm.await_args.args[0], "902")
        self.assertEqual([w["discord_id"] for w in await self.service.waitlist()], ["902", "903"])
        self.assertIn("nai-", (await self.service.issue_direct("902", GUILD))["key"])  # 被邀请的 902 真的领到
        self.assertEqual([w["discord_id"] for w in await self.service.waitlist()], ["903"])

    async def test_waitlist_without_dm_holds_slot_and_posts_one_announcement(self):
        # 申诉期：waitlist_dm=0 → 不私信，照样保留名额，公告频道只发一条汇总
        from unittest.mock import AsyncMock
        self.service.max_users = 2
        self.service.send_dm = AsyncMock(return_value=True)
        await self.db.set_setting("waitlist_dm", "0")
        await self.mint()
        await self.db._db.execute("INSERT INTO waitlist(discord_id, name, joined_at) VALUES ('901','a',0), ('902','b',1)")
        await self.db._db.commit()
        posts = []
        self.assertEqual(await self.service.invite_waitlist(announce=posts.append), 1)   # 只空 1 个位
        self.service.send_dm.assert_not_awaited()
        self.assertEqual(len(posts), 1)
        self.assertIn("候补前 1 位", posts[0])
        with self.assertRaises(RegistrationError):
            await self.service.issue_direct("902", GUILD)                                # 名额为 901 保留
        self.assertIn("nai-", (await self.service.issue_direct("901", GUILD))["key"])

    async def test_web_login_paused_by_default_never_starts_oauth(self):
        # 默认暂停：GET /login 必须跳回首页，不能把人送进 Discord 授权（应用审核期间）；设 0 才开放
        from types import SimpleNamespace
        from app.registration_routes import member_router
        app = FastAPI()
        app.include_router(member_router)
        app.state.registrar = self.service
        app.state.gate = SimpleNamespace(db=self.db)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://fixture.invalid") as client:
            paused = await client.get("/login", follow_redirects=False)
            self.assertIn(paused.status_code, (302, 307))
            self.assertIn("login=paused", paused.headers["location"])
            self.assertNotIn("discord.com", paused.headers["location"])
            await self.db.set_setting("web_login_paused", "0")
            opened = await client.get("/login", follow_redirects=False)
            self.assertIn("discord.com", opened.headers["location"])

    async def test_key_login_issues_member_session_only_for_registered_keys(self):
        # 网页 OAuth 暂停期间：用 /register 领到的 Key 登录首页；乱填的不行，后台手建的用 key:<id> 会话，错多了限流
        from types import SimpleNamespace
        from app.registration_routes import member_router, MEMBER_COOKIE, _KEY_LOGIN_FAILS
        app = FastAPI()
        app.include_router(member_router)
        app.state.registrar = self.service
        app.state.gate = SimpleNamespace(db=self.db, settings=SimpleNamespace(secret_key="k" * 40, data_dir=None))
        await self.mint()
        did = (await self.db._db.execute_fetchall("SELECT discord_id FROM discord_registrations"))[0][0]
        row = await self.service.key_row_for(did)
        await self.db._db.execute("UPDATE discord_registrations SET key_id=-1")     # 解绑后同一个 Key 就成了"后台手建"
        _KEY_LOGIN_FAILS.clear()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://fixture.invalid") as client:
            self.assertEqual((await client.post("/login/key", json={"key": "nai-wrong"})).status_code, 401)
            # 后台手建（没绑定 Discord）的 Key 也能登录（10-11 站长的 Key 登不进去），会话用 key:<id>，能看到自己的额度
            unbound = await client.post("/login/key", json={"key": row["token"]})
            self.assertEqual(unbound.status_code, 200)
            self.assertTrue(unbound.cookies[MEMBER_COOKIE].strip('"').startswith(f"key:{row['id']}:"))
            from app.registration_routes import _member_session
            req = SimpleNamespace(cookies={MEMBER_COOKIE: unbound.cookies[MEMBER_COOKIE].strip('"')}, app=app)
            self.assertEqual(await _member_session(req), f"key:{row['id']}")
            self.assertEqual((await self.service.key_row_for(f"key:{row['id']}"))["id"], row["id"])
            self.assertIsNone(await self.service.key_row_for("key:abc"))
            # 停用的、站长的 Key 不行
            await self.db._db.execute("UPDATE api_keys SET enabled=0 WHERE id=?", (row["id"],))
            self.assertEqual((await client.post("/login/key", json={"key": row["token"]})).status_code, 401)
            await self.db._db.execute("UPDATE api_keys SET enabled=1, is_admin=1 WHERE id=?", (row["id"],))
            self.assertEqual((await client.post("/login/key", json={"key": row["token"]})).status_code, 401)
            await self.db._db.execute("UPDATE api_keys SET is_admin=0 WHERE id=?", (row["id"],))
            _KEY_LOGIN_FAILS.clear()
            await self.db._db.execute("UPDATE discord_registrations SET key_id=?", (row["id"],))
            ok = await client.post("/login/key", json={"key": " " + row["token"] + " "})
            self.assertEqual(ok.status_code, 200)
            self.assertIn(MEMBER_COOKIE, ok.headers["set-cookie"])
            self.assertTrue(ok.cookies[MEMBER_COOKIE].strip('"').startswith(f"{did}:"))
            for _ in range(8):
                await client.post("/login/key", json={"key": "nai-wrong"})
            self.assertEqual((await client.post("/login/key", json={"key": row["token"]})).status_code, 429)
        _KEY_LOGIN_FAILS.clear()

    async def test_registration_and_ban_leave_the_waitlist(self):
        self.service.max_users = 1
        await self.db._db.execute("INSERT INTO waitlist(discord_id, name, joined_at) VALUES ('777','',0), ('999','',1)")
        await self.db._db.commit()
        self.assertIn("nai-", (await self.mint("777"))["key"])
        await self.service.ban("999")
        self.assertEqual(await self.service.waitlist(), [])

    async def test_young_discord_accounts_cannot_grab_slots(self):
        self.service.min_account_days = 7
        young = str(((int(time.time() * 1000) - 86_400_000) - 1420070400000) << 22)   # created yesterday
        with self.assertRaises(RegistrationError):
            await self.service.issue_direct(young, GUILD)
        self.assertEqual(await self.count("api_keys"), 0)
        old = str(((int(time.time() * 1000) - 30 * 86_400_000) - 1420070400000) << 22)
        self.assertIn("nai-", (await self.service.issue_direct(old, GUILD))["key"])


class MemberRoleTests(RegistrationTests):
    """Registering grants the 'has key' role; expiry/revoke/delete remove it; Discord errors never block registration."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.role_calls = []
        self.role_status = 204
        inner = self.http._transport

        def with_roles(request):
            if "/roles/" in request.url.path:
                self.role_calls.append((request.method, request.url.path.rsplit("/", 3)[-3:]))
                return httpx.Response(self.role_status)
            return inner.handler(request)
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(with_roles), base_url="https://discord.com")
        self.service.http = self.http
        self.service.member_role_id = "555"

    async def test_role_granted_on_success_and_removed_on_revoke(self):
        self.assertIn("nai-", (await self.mint())["key"])
        self.assertEqual(self.role_calls[0][0], "PUT")
        flag = (await self.db._db.execute_fetchall("SELECT role_granted FROM discord_registrations"))[0][0]
        self.assertEqual(flag, 1)
        await self.service.revoke("777")
        self.assertEqual(self.role_calls[-1][0], "DELETE")

    async def test_discord_role_failure_does_not_break_registration(self):
        self.role_status = 403
        self.assertIn("nai-", (await self.mint())["key"])
        flag = (await self.db._db.execute_fetchall("SELECT role_granted FROM discord_registrations"))[0][0]
        self.assertEqual(flag, 1)                    # marked first so a later expiry sync always tries to remove it

    async def test_sync_removes_role_when_key_expires_or_is_deleted(self):
        await self.mint()
        self.assertEqual(await self.service.sync_roles(), 0)             # still valid
        await self.db._db.execute("UPDATE api_keys SET expires_at=?", (time.time() - 5,))
        await self.db._db.commit()
        self.assertEqual(await self.service.sync_roles(), 1)
        self.assertEqual(self.role_calls[-1][0], "DELETE")
        self.assertEqual(await self.service.sync_roles(), 0)             # flag cleared, idempotent

    async def test_inactivity_cleanup_hook_removes_role(self):
        await self.mint()
        key_id = (await self.db._db.execute_fetchall("SELECT key_id FROM discord_registrations"))[0][0]
        ids = await self.db.forget_registration_for_key(key_id)
        self.assertEqual(ids, ["777"])


class HardeningTests(MemberRoleTests):
    """Bans, suspended members, deferred role removal retries."""

    async def test_short_bridge_secret_keeps_feature_off(self):
        from unittest.mock import patch
        from app.registration import configured_service
        env = dict(DISCORD_CLIENT_ID="c", DISCORD_CLIENT_SECRET="s", DISCORD_BOT_TOKEN="b",
                   REGISTRATION_BRIDGE_SECRET="short", DISCORD_GUILD_ID="123", SITE_URL="https://x")
        with patch.dict("os.environ", env, clear=True):
            self.assertIsNone(configured_service(None, None))

    async def test_ban_blocks_registration_and_survives_reset_and_expiry(self):
        await self.mint("777")
        await self.service.ban("777")
        self.assertEqual((await self.db._db.execute_fetchall("SELECT count(*) FROM api_keys"))[0][0], 0)
        with self.assertRaises(RegistrationError):
            await self.mint("777")
        await self.service.reset_all()
        with self.assertRaises(RegistrationError):
            await self.mint("777")                                              # still banned
        self.assertEqual(await self.count("api_keys"), 0)
        self.assertTrue(await self.service.unban("777"))
        self.assertIn("nai-", (await self.mint("777"))["key"])

    async def test_disabled_member_is_not_released_by_expiry_or_reset_all(self):
        await self.mint("777")
        await self.db._db.execute("UPDATE api_keys SET enabled=0, expires_at=?", (time.time() - 5,))
        await self.db._db.commit()
        with self.assertRaises(RegistrationError):
            await self.mint("777")                                              # suspended, not auto-released
        self.assertEqual(await self.service.reset_all(), 0)
        self.assertEqual((await self.db._db.execute_fetchall("SELECT count(*) FROM discord_registrations"))[0][0], 1)
        self.assertEqual(await self.db.inactive_key_ids(time.time() + 10 * 86400), [])   # inactivity cleanup skips it too

    async def test_failed_role_removal_is_retried_after_the_registration_row_is_gone(self):
        await self.mint("777")
        self.role_status = 429
        await self.service.revoke("777")                                        # key + row gone, role DELETE failed
        pending = await self.db._db.execute_fetchall("SELECT discord_id FROM pending_role_removals")
        self.assertEqual([r[0] for r in pending], ["777"])
        self.role_status = 204
        self.assertEqual(await self.service.sync_roles(), 1)                    # retried successfully
        self.assertEqual(await self.db._db.execute_fetchall("SELECT 1 FROM pending_role_removals"), [])

    async def test_expiry_release_inside_issue_defers_role_http(self):
        await self.mint("777")
        await self.db._db.execute("UPDATE api_keys SET expires_at=?", (time.time() - 5,))
        await self.db._db.commit()
        before = len(self.role_calls)
        self.assertIn("nai-", (await self.mint("777"))["key"])                 # 到期自动释放后重新领取
        self.assertEqual([c[0] for c in self.role_calls[before:]], ["PUT"])     # 锁内不去摘身份组，只重新挂上
        self.assertEqual(await self.db._db.execute_fetchall("SELECT 1 FROM pending_role_removals"), [])
        self.assertEqual(await self.count("api_keys"), 1)


class DepartedSweepTests(RegistrationTests):
    """退群回收：每人每天核对一次；只有 Discord 明确说「不是成员」(404/10007) 才删 Key，其他错误一律跳过。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.member_answers = {}
        inner = self.http._transport

        def sweep(request):
            if request.method == "GET" and re.match(r"^/api/v10/guilds/\d+/members/\d+$", request.url.path):
                return self.member_answers.get(request.url.path.rsplit("/", 1)[-1], httpx.Response(200, json={}))
            return inner.handler(request)
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(sweep), base_url="https://discord.com")
        self.service.http = self.http

    async def test_only_confirmed_departures_are_revoked(self):
        for uid in ("777", "778", "779", "780"):
            await self.mint(uid)
        self.member_answers = {
            "777": httpx.Response(404, json={"code": 10007, "message": "Unknown Member"}),   # 退群了
            "778": httpx.Response(404, json={"code": 10004, "message": "Unknown Guild"}),    # 机器人配置问题：不能误删
            "779": httpx.Response(429, json={"retry_after": 1}),                             # 限流：下轮再查
        }
        now = time.time()
        removed = 0
        for _ in range(4):
            removed += await self.service.sweep_departed(now=now)
        self.assertEqual(removed, 1)
        left = {r[0] for r in await self.db._db.execute_fetchall("SELECT discord_id FROM discord_registrations")}
        self.assertEqual(left, {"778", "779", "780"})
        self.assertEqual(await self.count("api_keys"), 3)
        checked = dict(await self.db._db.execute_fetchall("SELECT discord_id, member_checked_at FROM discord_registrations"))
        self.assertEqual(checked["780"], now)                  # 在服务器：今天不再查
        self.assertEqual(checked["778"], now - 86400 + 3600)   # 没查清：1 小时后重查，不挡住队列
        self.member_answers = {}
        await self.service.sweep_departed(now=now + 60)         # 778 / 779 重查，780 今天已查过
        self.assertEqual(await self.service.sweep_departed(now=now + 60), 0)

    async def test_kill_switch(self):
        await self.mint("777")
        await self.db.set_setting("member_sweep", "0")
        self.member_answers = {"777": httpx.Response(404, json={"code": 10007})}
        self.assertEqual(await self.service.sweep_departed(), 0)
        self.assertEqual(await self.count("discord_registrations"), 1)


class SessionBindingTests(RegistrationTests):
    async def test_resetkey_kills_sessions_opened_with_the_old_key(self):
        # 审查 P0：泄露的 Key 被拿去 /login/key，成员 /resetkey 后，攻击者的会话必须失效、拿不到新 Key
        from types import SimpleNamespace
        from app.registration_routes import member_router, MEMBER_COOKIE, _KEY_LOGIN_FAILS
        from app.policy import gen_key
        app = FastAPI()
        app.include_router(member_router)
        app.state.registrar = self.service
        app.state.gate = SimpleNamespace(db=self.db, settings=SimpleNamespace(secret_key="k" * 40, data_dir=None),
                                         day=lambda: "2026-10-10")
        await self.mint()
        did = (await self.db._db.execute_fetchall("SELECT discord_id FROM discord_registrations"))[0][0]
        old = await self.service.key_row_for(did)
        _KEY_LOGIN_FAILS.clear()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://fixture.invalid") as client:
            login = await client.post("/login/key", json={"key": old["token"]})
            cookie = {MEMBER_COOKIE: login.cookies[MEMBER_COOKIE]}
            from app import registration_routes as mr
            req = SimpleNamespace(cookies=cookie, app=app)
            self.assertEqual(await mr._member_session(req), str(did))
            await self.db.rotate_key_token(old["id"], gen_key("nai"))          # /resetkey
            self.assertIsNone(await mr._member_session(req))


class NoWaitlistTests(RegistrationTests):
    async def test_full_without_waitlist_refuses_and_frees_first_come(self):
        # 候补关闭（默认）：名额满直接提示、不进候补；有空位先到先得
        await self.db.set_setting("waitlist_enabled", "0")
        self.service.max_users = 1
        await self.db.set_setting("register_max_users", "1")
        await self.mint("777")
        with self.assertRaises(RegistrationError) as ctx:
            await self.mint("888")
        self.assertIn("先到先得", str(ctx.exception))
        self.assertEqual(await self.count("waitlist"), 0)
        await self.service.revoke("777")
        self.assertIn("nai-", (await self.mint("888"))["key"])


class GiftTests(RegistrationTests):
    async def test_gift_issues_key_even_when_closed_and_is_idempotent(self):
        # 站长 / 机器人奖励：领 Key 关着（或名额满）也给没有 Key 的人发一把；已经有 Key 就不再发
        await self.db.set_setting("register_open", "0")
        with self.assertRaises(RegistrationError):
            await self.mint("888")
        g = await self.service.gift({"id": "888", "username": "sese", "global_name": "涩涩"})
        self.assertTrue(g["new"])
        self.assertIn(g["key"], g["message"])
        again = await self.service.gift({"id": "888"})
        self.assertEqual((again["new"], again["key_id"]), (False, g["key_id"]))
        self.assertEqual(await self.count("api_keys"), 1)


class InviteBindTests(RegistrationTests):
    """10-11 站长：邀请 Key 用 /register 的 key 一栏绑定，后台自动认人；领 Key 关着也能绑。"""

    async def make_invite(self):
        row = await self.db.create_key({"name": "邀请-9", "token": "nai-invite-xyz", "daily_images": 1,
                                        "monthly_anlas": 0, "daily_text_tokens": 0, "rpm": 5})
        await self.db._db.execute("INSERT INTO key_tags(key_id, tag, note, by, ts) VALUES (?, '邀请', '', '站长', 0)",
                                  (row["id"],))
        await self.db._db.commit()
        return row["id"]

    async def test_invite_binds_even_when_closed_and_only_once(self):
        kid = await self.make_invite()
        await self.db.set_setting("register_open", "0")
        self.assertTrue(await self.service.is_unbound_invite(kid))
        with self.assertRaisesRegex(RegistrationError, "邀请 Key"):
            await self.service.issue_direct("777", GUILD, username="bob", invite_key="nai-wrong")
        res = await self.service.issue_direct("777", GUILD, username="bob", global_name="Bob", invite_key="nai-invite-xyz")
        self.assertIn("已绑定", res["message"])
        self.assertEqual(await self.count("api_keys"), 1)                         # 没有发新 Key
        self.assertEqual((await self.service.key_row_for("777"))["id"], kid)
        self.assertFalse(await self.service.is_unbound_invite(kid))
        self.assertIn("bob", (await self.db.get_key(kid))["name"])                # 后台自动显示是谁
        with self.assertRaises(RegistrationError):                                  # 别人不能再绑同一把
            await self.service.issue_direct("888", GUILD, username="eve", invite_key="nai-invite-xyz")
        with self.assertRaisesRegex(RegistrationError, "已经领取过"):               # 已绑过的人不能再绑
            await self.service.issue_direct("777", GUILD, invite_key="nai-invite-xyz")
