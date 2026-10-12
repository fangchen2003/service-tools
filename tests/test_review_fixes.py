"""Regression tests for the adversarial-review findings."""
import asyncio
import io
import time
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
import pytest_asyncio
from PIL import Image

from app import main
from app.admin import _secret, router as admin_router
from app.audit import make_thumbnail
from app.config import Settings
from app.database import Database
from app.registration import RegistrationService
from app.registration_routes import router as bridge_router
from app.state import GateState
from tests.test_admin_hardening import make_client
from tests.test_admin_session import AdminState


@pytest_asyncio.fixture
async def db():
    value = Database(":memory:")
    await value.connect()
    yield value
    await value.close()


def bridge_app(db, admin_ids=("42",)):
    failures = []
    service = RegistrationService(db, None, client_id="c", client_secret="s", bot_token="b",
                                  bridge_secret="x" * 40, admin_ids=admin_ids)
    app = FastAPI()
    app.include_router(bridge_router)
    app.state.registrar = service
    app.state.gate = SimpleNamespace(db=db, record_auth_failure=failures.append, day=lambda: "d",
                                     upstream_health=lambda: {}, settings=Settings(), announcer=None)
    return app, service, failures


@pytest.mark.asyncio
async def test_admin_bridge_actions_need_an_allowlisted_actor_and_bad_secrets_are_counted(db):
    app, service, failures = bridge_app(db)
    auth = {"Authorization": "Bearer " + "x" * 40}
    body = {"discord_id": "7", "guild_id": service.command_guild, "action": "open", "value": "on"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("9.9.9.9", 1)), base_url="https://t") as c:
        assert (await c.post("/self-register/ops", headers=auth, json=body)).status_code == 403                # no actor
        assert (await c.post("/self-register/ops", headers=auth, json={**body, "actor_id": "7"})).status_code == 403
        assert (await c.post("/self-register/ops", headers=auth, json={**body, "actor_id": "42"})).status_code == 200
        assert (await c.post("/self-register/revoke", headers=auth, json={**body, "actor_id": "7"})).status_code == 403
        assert (await c.post("/self-register/slots", headers=auth, json={**body, "actor_id": "7"})).status_code == 403
        assert (await c.post("/self-register/ops", headers={"Authorization": "Bearer nope"}, json=body)).status_code == 401
        assert failures == ["9.9.9.9"]                                                  # wrong secret counted
        weird = await c.post("/self-register/info", headers={"Authorization": b"Bearer \xe9"}, json=body)
        assert weird.status_code == 401                                                 # non-ASCII is 401, not 500
    app2, _, _ = bridge_app(db, admin_ids=())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="https://t") as c:
        assert (await c.post("/self-register/ops", headers=auth, json={**body, "actor_id": "42"})).status_code == 403   # empty allowlist = deny


@pytest.mark.asyncio
async def test_ban_action_through_the_bridge(db):
    app, service, _ = bridge_app(db)
    auth = {"Authorization": "Bearer " + "x" * 40}
    body = {"discord_id": "7", "guild_id": service.command_guild, "action": "ban", "target": "555", "actor_id": "42"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://t") as c:
        assert (await c.post("/self-register/ops", headers=auth, json=body)).status_code == 200
        assert await service.is_banned("555")
        assert (await c.post("/self-register/ops", headers=auth, json={**body, "action": "unban"})).status_code == 200
        assert not await service.is_banned("555")


@pytest.mark.asyncio
async def test_valid_keys_are_not_blocked_by_an_ip_block_but_bad_ones_are(db, monkeypatch):
    key = await db.create_key(dict(name="m", token="nai-good", daily_images=5, monthly_anlas=0,
                                   daily_text_tokens=0, rpm=5))
    state = SimpleNamespace(db=db, auth_blocked=lambda ip: 600, record_auth_failure=lambda ip: None)

    async def touch(_id):
        return None
    state.db.touch_key = touch
    monkeypatch.setattr(main, "STATE", state)

    def req(token):
        return SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"),
                               headers={"authorization": "Bearer " + token} if token else {})
    assert (await main.authenticate(req("nai-good")))["id"] == key["id"]          # member behind a blocked NAT still works
    with pytest.raises(main.GateError) as bad:
        await main.authenticate(req("nai-bogus"))
    assert bad.value.status == 429
    with pytest.raises(main.GateError) as none:
        await main.authenticate(req(""))
    assert none.value.status == 429


@pytest.mark.asyncio
async def test_one_key_cannot_fill_the_shared_tag_queue(tmp_path):
    state = GateState(Settings(data_dir=tmp_path))
    state.settings.key_image_min_interval = 30
    results = []

    async def attempt():
        results.append(await state.wait_for_tag_request(1))
    tasks = [asyncio.create_task(attempt()) for _ in range(6)]
    await asyncio.sleep(0.2)
    # one admitted; older waiters are superseded by newer ones (None), only the newest keeps waiting
    assert results.count(True) == 1 and results.count(None) == 4 and results.count(False) == 0
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    # key 1's waiters do not stop another key from being admitted once the lane is free
    await state.finish_tag_request(1)
    assert await asyncio.wait_for(state.wait_for_tag_request(2), 1) is True


def test_corrupt_or_empty_secret_file_is_regenerated_atomically(tmp_path):
    settings = Settings(admin_password="p", secret_key="", data_dir=tmp_path)
    (tmp_path / "secret_key").write_text("")                      # e.g. left behind by a crash
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(gate=SimpleNamespace(settings=settings))))
    secret = _secret(req)
    assert len(secret) >= 32 and (tmp_path / "secret_key").read_text().strip() == secret
    assert not (tmp_path / "secret_key.tmp").exists()
    assert (tmp_path / "secret_key").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_non_ascii_admin_password_can_log_in(tmp_path):
    _, client = make_client(tmp_path, password="密码密码密码密码密码密码")
    async with client:
        ok = await client.post("/admin/api/login", json={"password": "密码密码密码密码密码密码"})
        bad = await client.post("/admin/api/login", json={"password": "错误错误错误错误错误错误"})
        assert ok.status_code == 200 and bad.status_code == 401


def test_decompression_bomb_images_are_rejected_not_decoded():
    raw = io.BytesIO()
    Image.new("L", (6000, 6000), 0).save(raw, "PNG")              # 36M pixels, tiny file
    assert make_thumbnail(raw.getvalue()) is None


@pytest.mark.asyncio
async def test_usage_log_retention_and_login_window_pruning(db, tmp_path):
    await db.add_log(None, "k", "image", "m", "ok")
    assert await db.purge_usage_log(time.time() + 5) == 1
    state = GateState(Settings(data_dir=tmp_path, login_window_seconds=1, login_max_attempts=100))
    for i in range(2100):
        state._login_attempts[f"ip{i}"] = __import__("collections").deque([time.time() - 10])
    await state.hit_login("fresh")
    assert len(state._login_attempts) < 100


@pytest.mark.asyncio
async def test_owner_can_change_admin_password_from_the_panel(tmp_path, db):
    settings = Settings(admin_password="a-strong-password-123", secret_key="s", data_dir=tmp_path,
                        admin_cookie_secure=False)
    state = AdminState(settings)
    state.db = db
    app = FastAPI()
    app.state.gate = state
    app.include_router(admin_router)
    hdr = {"Origin": "http://t"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/admin/api/login", json={"password": "a-strong-password-123"})).status_code == 200
        assert (await c.put("/admin/api/password", headers=hdr, json={"current": "wrong", "new": "x" * 14})).status_code == 401
        assert (await c.put("/admin/api/password", headers=hdr, json={"current": "a-strong-password-123", "new": "short"})).status_code == 422
        done = await c.put("/admin/api/password", headers=hdr,
                           json={"current": "a-strong-password-123", "new": "a-new-very-strong-pw"})
        assert done.status_code == 200 and done.json()["relogin"] is True
        assert (await c.get("/admin/api/me")).status_code == 401                              # old session is dead
        assert (await c.post("/admin/api/login", json={"password": "a-strong-password-123"})).status_code == 401
        assert (await c.post("/admin/api/login", json={"password": "a-new-very-strong-pw"})).status_code == 200
        assert (await c.get("/admin/api/me")).status_code == 200
    assert (await db.get_setting("admin_password_hash")).startswith("scrypt$")                # only a hash is stored
    assert "a-new-very-strong-pw" not in (await db.get_setting("admin_password_hash"))


@pytest.mark.asyncio
async def test_deleting_a_discord_member_clears_registration_and_can_ban_and_members_show_profile(tmp_path, db):
    settings = Settings(admin_password="a-strong-password-123", secret_key="s", data_dir=tmp_path, admin_cookie_secure=False)
    service = RegistrationService(db, None, client_id="c", client_secret="s", bot_token="b",
                                  bridge_secret="x" * 40)

    async def member(uid, name):
        k = await db.create_key(dict(name=name, token="t" + uid, daily_images=5, monthly_anlas=0, daily_text_tokens=0, rpm=5))
        await db._db.execute(
            "INSERT INTO discord_registrations(discord_id,key_id,created_at,username,display_name,avatar) VALUES (?,?,?,?,?,?)",
            (uid, k["id"], time.time(), "someone", "Some One", "abc123"))
        await db._db.commit()
        return k["id"]
    first, second = await member("1010000000000000001", "Some One (@someone)"), await member("2020000000000000002", "Other")

    class State(AdminState):
        def __init__(self):
            super().__init__(settings)
            self.db = db
            self.admin_pw_hash = None

        def day(self):
            return "2026-10-09"

        def week_days(self, n=7):
            return ["2026-10-09"]
    app = FastAPI()
    app.state.gate = State()
    app.state.registrar = service
    app.include_router(admin_router)
    hdr = {"Origin": "http://t"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/admin/api/login", json={"password": "a-strong-password-123"})
        members = (await c.get("/admin/api/members")).json()["members"]
        profile = next(m for m in members if m["id"] == first)["discord"]
        assert profile["display_name"] == "Some One" and profile["username"] == "someone"
        assert profile["avatar_url"].endswith("/avatars/1010000000000000001/abc123.png?size=64")
        assert profile["profile_url"] == "https://discord.com/users/1010000000000000001"
        gone = await c.delete(f"/admin/api/keys/{first}", headers=hdr)
        assert gone.status_code == 200 and gone.json()["banned"] is False
        assert await db.get_key(first) is None
        assert await db._db.execute_fetchall("SELECT 1 FROM discord_registrations WHERE key_id=?", (first,)) == []
        assert not await service.is_banned("1010000000000000001")                         # plain delete = can come back
        banned = await c.delete(f"/admin/api/keys/{second}?ban=true", headers=hdr)
        assert banned.json()["banned"] is True and await service.is_banned("2020000000000000002")


# ---------------------------------------------------------------- upstream tokens managed from the panel
from app.nai import NaiClient
from app import token_store

TOKEN_A = "pst-" + "A" * 40
TOKEN_B = "pst-" + "B" * 40
TOKEN_C = "pst-" + "C" * 40


async def make_nai(db, tmp_path, tokens=(TOKEN_A,), subscription=200):
    seen = []

    def upstream(request):
        seen.append(request.headers["authorization"])
        if subscription == 200:
            return httpx.Response(200, json={"tier": 3, "active": True})
        return httpx.Response(subscription, json={})
    nai = NaiClient(list(tokens), "https://image.example", "https://text.example", "https://legacy.example",
                    db=db, day_fn=lambda: "2026-10-09", v5_daily_limits=[], allow_anlas=[True] * len(tokens))
    nai._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    nai.managed_path = tmp_path / "upstream_tokens.json"
    return nai, seen


@pytest.mark.asyncio
async def test_token_store_roundtrip_is_private_and_tolerates_garbage(tmp_path):
    path = tmp_path / "t.json"
    assert token_store.load(path) is None
    token_store.save(path, [{"token": TOKEN_A, "allow_anlas": True}])
    assert token_store.load(path) == [{"token": TOKEN_A, "allow_anlas": True}]
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("not json")
    assert token_store.load(path) is None
    assert not token_store.valid_token("pst-short") and not token_store.valid_token("nai-" + "A" * 40)


@pytest.mark.asyncio
async def test_add_replace_remove_keep_settings_and_never_touch_the_database_with_raw_tokens(db, tmp_path):
    nai, _ = await make_nai(db, tmp_path)
    old = nai.pool[0]
    await nai.set_v5_daily_limit(old.token_id, 40)
    await nai.set_image_concurrency(old.token_id, 2)
    await db.bump_upstream_counter(old.token_id, "2026-10-09", 3) if hasattr(db, "bump_upstream_counter") else None
    added = await nai.add_token(TOKEN_B, allow_anlas=False)
    assert [t.position for t in nai.pool] == [1, 2] and added.allow_anlas is False
    with pytest.raises(ValueError):
        await nai.add_token(TOKEN_B)                                           # duplicate
    replaced = await nai.replace_token(old.token_id, TOKEN_C)
    assert replaced.token_id != old.token_id and replaced.v5_daily_limit == 40 and replaced.image_slots.limit == 2
    assert (await db.get_upstream_token_limits()).get(replaced.token_id) == 40            # settings moved to the new id
    assert old.token_id not in await db.get_upstream_token_limits()
    saved = token_store.load(tmp_path / "upstream_tokens.json")
    assert [e["token"] for e in saved] == [TOKEN_C, TOKEN_B]
    with pytest.raises(LookupError):
        await nai.replace_token("token-nope", TOKEN_A)
    assert await nai.remove_token(added.token_id) is True
    with pytest.raises(ValueError):
        await nai.remove_token(nai.pool[0].token_id)                           # never remove the last token
    raw = "".join(str(r) for r in await db._db.execute_fetchall("SELECT * FROM site_settings"))
    assert "pst-" not in raw


@pytest.mark.asyncio
async def test_verify_token_maps_upstream_answers(db, tmp_path):
    nai, seen = await make_nai(db, tmp_path)
    assert await nai.verify_token(TOKEN_B) == {"ok": True, "tier": 3}
    assert seen[-1] == f"Bearer {TOKEN_B}"
    bad, _ = await make_nai(db, tmp_path, subscription=401)
    assert (await bad.verify_token(TOKEN_B))["ok"] is False


@pytest.mark.asyncio
async def test_admin_token_endpoints_validate_then_change_the_pool(tmp_path, db):
    nai, _ = await make_nai(db, tmp_path)
    settings = Settings(admin_password="a-strong-password-123", secret_key="s", data_dir=tmp_path, admin_cookie_secure=False)
    announced = []

    class State(AdminState):
        def __init__(self):
            super().__init__(settings)
            self.db, self.nai, self.admin_pw_hash = db, nai, None
            self.alerter = SimpleNamespace(notify=lambda kind, text, cooldown=0: announced.append(text))
    app = FastAPI()
    app.state.gate = State()
    app.include_router(admin_router)
    hdr = {"Origin": "http://t"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/admin/api/upstream-tokens", headers=hdr, json={"token": TOKEN_B})).status_code == 401
        await c.post("/admin/api/login", json={"password": "a-strong-password-123"})
        assert (await c.post("/admin/api/upstream-tokens", headers=hdr, json={"token": "nope"})).status_code == 422
        ok = await c.post("/admin/api/upstream-tokens", headers=hdr, json={"token": TOKEN_B, "allow_anlas": False})
        assert ok.status_code == 200 and len(ok.json()["pool"]) == 2 and ok.json()["tier"] == 3
        assert TOKEN_A not in ok.text and TOKEN_B not in ok.text                    # raw tokens are never returned
        assert all("…" in row["token"] for row in ok.json()["pool"])                 # only masked forms
        assert (await c.post("/admin/api/upstream-tokens", headers=hdr, json={"token": TOKEN_B})).status_code == 409
        first_id = ok.json()["pool"][0]["token_id"]
        rep = await c.put(f"/admin/api/upstream-tokens/{first_id}", headers=hdr, json={"token": TOKEN_C})
        assert rep.status_code == 200 and rep.json()["pool"][0]["token_id"] != first_id
        gone = await c.delete(f"/admin/api/upstream-tokens/{rep.json()['pool'][1]['token_id']}", headers=hdr)
        assert gone.status_code == 200 and len(gone.json()["pool"]) == 1
        last = await c.delete(f"/admin/api/upstream-tokens/{gone.json()['pool'][0]['token_id']}", headers=hdr)
    assert len(announced) == 3                                                   # owner is told about every change


@pytest.mark.asyncio
async def test_audit_export_and_purge_fixes(tmp_path, db):
    from app.admin import _safe_filename, _THUMB_CACHE
    assert _safe_filename("alex") == "alex"
    assert _safe_filename("user_123.test") == "user_123.test"
    assert _safe_filename("张三") == "member"
    assert _safe_filename("a/b\\c") == "a_b_c"

    # Verify query plan uses idx_audit_key_id
    plan = await db._db.execute_fetchall("EXPLAIN QUERY PLAN SELECT id FROM generation_audit WHERE key_id=? ORDER BY id DESC LIMIT 24", (1,))
    plan_str = " ".join(str(tuple(p)) for p in plan)
    assert "idx_audit_key_id" in plan_str
    assert "TEMP B-TREE" not in plan_str

    # Test audit_purge_images clears _THUMB_CACHE and logs once
    _THUMB_CACHE[999] = b"fake"
    settings = Settings(admin_password="a-strong-password-123", secret_key="s", data_dir=tmp_path, admin_cookie_secure=False)
    state = AdminState(settings)
    state.db = db
    app = FastAPI()
    app.state.gate = state
    app.include_router(admin_router)
    hdr = {"Origin": "http://t"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/admin/api/login", json={"password": "a-strong-password-123"})
        res = await c.post("/admin/api/audit/purge-images", headers=hdr, json={"confirm": "清空"})
        assert res.status_code == 200
        assert 999 not in _THUMB_CACHE
        # Check action log only logged once
        logs = await db._db.execute_fetchall("SELECT action FROM admin_actions WHERE action='清空原图'")
        assert len(logs) == 1
        raw_logs = await db._db.execute_fetchall("SELECT action FROM admin_actions WHERE action LIKE '%/audit/purge-images%'")
        assert len(raw_logs) == 0


@pytest.mark.asyncio
async def test_usage_log_indices_and_pagination(tmp_path, db):
    # Verify query plan for rid point lookup uses idx_log_rid (no full scan)
    plan_rid = await db._db.execute_fetchall("EXPLAIN QUERY PLAN SELECT id FROM usage_log WHERE rid=? ORDER BY id DESC LIMIT 20", ("abc123",))
    plan_rid_str = " ".join(str(tuple(p)) for p in plan_rid)
    assert "idx_log_rid" in plan_rid_str
    assert "SCAN usage_log" not in plan_rid_str

    # Verify query plan for key_id uses idx_log_key_id (no temp b-tree)
    plan_key = await db._db.execute_fetchall("EXPLAIN QUERY PLAN SELECT id FROM usage_log WHERE key_id=? ORDER BY id DESC LIMIT 20", (1,))
    plan_key_str = " ".join(str(tuple(p)) for p in plan_key)
    assert "idx_log_key_id" in plan_key_str
    assert "TEMP B-TREE" not in plan_key_str

    # Verify per_page in logs endpoint
    settings = Settings(admin_password="a-strong-password-123", secret_key="s", data_dir=tmp_path, admin_cookie_secure=False)
    state = AdminState(settings)
    state.db = db
    app = FastAPI()
    app.state.gate = state
    app.include_router(admin_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/admin/api/login", json={"password": "a-strong-password-123"})
        r50 = await c.get("/admin/api/logs?per_page=50")
        assert r50.status_code == 200 and r50.json()["per_page"] == 50
        r_def = await c.get("/admin/api/logs?per_page=999")
        assert r_def.status_code == 200 and r_def.json()["per_page"] == 20


@pytest.mark.asyncio
async def test_bug_tracker_improvements_and_endpoints(tmp_path, db):
    from app.errors import Tracker

    tracker = Tracker(db)
    # Check index existence
    indices = await db._db.execute_fetchall("PRAGMA index_list('error_events')")
    idx_names = [r[1] for r in indices]
    assert "idx_error_events_resolved_last_seen" in idx_names

    # Capture 2 bugs
    sig1 = tracker.capture("request", RuntimeError("err1"), key_id=9999)  # deleted key
    sig2 = tracker.capture("upstream", ValueError("err2"))
    await tracker.drain()

    assert await tracker.open_count() == 2

    # Mark sig2 as resolved
    assert await tracker.resolve(sig2) is True
    # Idempotent resolve on already resolved bug returns True
    assert await tracker.resolve(sig2) is True
    # Resolve non-existent bug returns False
    assert await tracker.resolve("nonexistent1") is False

    assert await tracker.open_count() == 1

    # list(include_resolved=True) should put unresolved bugs first
    items = await tracker.list(include_resolved=True)
    assert len(items) == 2
    assert items[0]["sig"] == sig1
    assert items[0]["resolved_at"] is None
    assert items[1]["sig"] == sig2
    assert items[1]["resolved_at"] is not None

    # Test Admin API endpoints
    settings = Settings(admin_password="a-strong-password-123", secret_key="s", data_dir=tmp_path, admin_cookie_secure=False)
    state = AdminState(settings)
    state.db = db
    state.bugs = tracker
    app = FastAPI()
    app.state.gate = state
    app.include_router(admin_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/admin/api/login", json={"password": "a-strong-password-123"})

        # GET /admin/api/errors
        res = await c.get("/admin/api/errors?all=true")
        assert res.status_code == 200
        body = res.json()
        assert body["open"] == 1
        assert len(body["errors"]) == 2
        # Check deleted key handling
        assert body["errors"][0]["key_name"] == "Key #9999 (已删除)"

        # POST /admin/api/errors/{sig}/resolve
        r_res = await c.post(f"/admin/api/errors/{sig1}/resolve")
        assert r_res.status_code == 200
        assert await tracker.open_count() == 0

        # Create one more bug and test resolve-all
        sig3 = tracker.capture("request", RuntimeError("err3"))
        await tracker.drain()
        assert await tracker.open_count() == 1

        r_all = await c.post("/admin/api/errors/resolve-all")
        assert r_all.status_code == 200 and r_all.json()["count"] == 1
        assert await tracker.open_count() == 0


@pytest.mark.asyncio
async def test_feedback_index_and_export_audit(tmp_path, db):
    # Check index existence
    indices = await db._db.execute_fetchall("PRAGMA index_list('feedback')")
    idx_names = [r[1] for r in indices]
    assert "idx_feedback_discord_id_ts" in idx_names

    settings = Settings(admin_password="a-strong-password-123", secret_key="s", data_dir=tmp_path, admin_cookie_secure=False)
    state = AdminState(settings)
    state.db = db
    app = FastAPI()
    app.state.gate = state
    app.include_router(admin_router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/admin/api/login", json={"password": "a-strong-password-123"})
        res = await c.get("/admin/api/feedback/export")
        assert res.status_code == 200
        assert "Content-Disposition" in res.headers
        assert "filename*=" in res.headers["Content-Disposition"]

        # Verify audit log was written
        logs = await db._db.execute_fetchall("SELECT action, target FROM admin_actions WHERE action='导出成员反馈'")
        assert len(logs) == 1
        assert logs[0][1] == "全部CSV"


@pytest.mark.asyncio
async def test_ops_snapshot_fallback_when_service_none(tmp_path, db):
    # Insert a discord registration and a waitlist record
    key = await db.create_key({
        "name": "Discord:test_user", "token": "test-token-12345", "daily_images": 10,
        "daily_anlas": 0, "daily_v5": 0, "monthly_anlas": 0, "daily_text_tokens": 0,
        "rpm": 10, "allow_anlas": False, "allow_img2img": False, "exclude_global_v5": False, "image_model_scope": "all"
    })
    await db._db.execute("INSERT INTO discord_registrations (discord_id, key_id, created_at, username) VALUES ('123456789012345678', ?, ?, 'test_user')", (key["id"], time.time()))
    await db._db.execute("INSERT INTO waitlist (discord_id, name, joined_at) VALUES ('987654321098765432', 'wait_user', ?)", (time.time(),))
    await db._db.commit()

    settings = Settings(admin_password="a-strong-password-123", secret_key="s", data_dir=tmp_path, admin_cookie_secure=False)
    state = AdminState(settings)
    state.db = db
    app = FastAPI()
    app.state.gate = state
    # app.state.registrar is explicitly None
    app.include_router(admin_router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/admin/api/login", json={"password": "a-strong-password-123"})
        res = await c.get("/admin/api/ops")
        assert res.status_code == 200
        data = res.json()
        assert data["registration"]["active"] == 1
        assert len(data["registration"]["waitlist"]) == 1
        assert data["registration"]["waitlist"][0]["discord_id"] == "987654321098765432"


@pytest.mark.asyncio
async def test_ops_registration_role_pair_validation_and_audit(tmp_path, db):
    settings = Settings(admin_password="a-strong-password-123", secret_key="s", data_dir=tmp_path, admin_cookie_secure=False)
    state = AdminState(settings)
    state.db = db
    app = FastAPI()
    app.state.gate = state
    app.include_router(admin_router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/admin/api/login", json={"password": "a-strong-password-123"})

        # Only role_guild provided -> 422
        r1 = await c.put("/admin/api/ops/registration", json={"role_guild": "1234567890123456", "role_id": ""})
        assert r1.status_code == 422
        assert "同时" in r1.json()["detail"]

        # Only role_id provided -> 422
        r2 = await c.put("/admin/api/ops/registration", json={"role_guild": "", "role_id": "1234567890123456"})
        assert r2.status_code == 422
        assert "同时" in r2.json()["detail"]

        # Both provided -> 200
        r3 = await c.put("/admin/api/ops/registration", json={"role_guild": "1234567890123456", "role_id": "9876543210987654"})
        assert r3.status_code == 200

        # Check audit target for ops/registration
        logs = await db._db.execute_fetchall("SELECT action, target FROM admin_actions WHERE action='修改开放注册设置'")
        assert len(logs) >= 1
        assert logs[-1][1] == "自助领 Key 设置"

        # Check ops/features and its audit target
        rf = await c.put("/admin/api/ops/features", json={"flags": {"text": False}})
        assert rf.status_code == 200
        flogs = await db._db.execute_fetchall("SELECT action, target FROM admin_actions WHERE action='修改全局功能开关'")
        assert len(flogs) >= 1
        assert flogs[-1][1] == "全站功能开关"


@pytest.mark.asyncio
async def test_limits_audit_targets_and_quota_algo_put(tmp_path, db):
    settings = Settings(admin_password="a-strong-password-123", secret_key="s", data_dir=tmp_path, admin_cookie_secure=False)
    state = AdminState(settings)
    state.db = db
    async def fake_update_runtime_limits(body):
        return body
    state.update_runtime_limits = fake_update_runtime_limits
    app = FastAPI()
    app.state.gate = state
    app.include_router(admin_router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/admin/api/login", json={"password": "a-strong-password-123"})

        # 1. runtime-limits
        r1 = await c.put("/admin/api/runtime-limits", json={"queue_timeout": 60})
        assert r1.status_code == 200
        logs1 = await db._db.execute_fetchall("SELECT action, target FROM admin_actions WHERE action='修改排队与冷却'")
        assert len(logs1) >= 1
        assert logs1[-1][1] == "图片排队与冷却"

        # 2. settings
        r2 = await c.put("/admin/api/settings", json={"global_daily_v5": 50})
        assert r2.status_code == 200
        logs2 = await db._db.execute_fetchall("SELECT action, target FROM admin_actions WHERE action='修改预算'")
        assert len(logs2) >= 1
        assert logs2[-1][1] == "Anlas 与 V5 预算"

        # 3. quota-algo PUT
        r3 = await c.put("/admin/api/quota-algo", json={"quota_target_avg": 120, "quota_ceiling_max": 250})
        assert r3.status_code == 200
        assert r3.json()["ok"] is True
        logs3 = await db._db.execute_fetchall("SELECT action, target FROM admin_actions WHERE action='修改动态额度参数'")
        assert len(logs3) >= 1
        assert logs3[-1][1] == "动态额度参数"

        # Check that quota_target_avg setting was actually saved
        saved_avg = await db.get_setting("quota_target_avg", None)
        assert int(saved_avg) == 120


@pytest.mark.asyncio
async def test_modules_audit_targets_and_run_endpoint(tmp_path, db):
    from app.kernel import Kernel, Module
    settings = Settings(admin_password="a-strong-password-123", secret_key="s", data_dir=tmp_path, admin_cookie_secure=False)
    state = AdminState(settings)
    state.db = db
    kernel = Kernel(state)
    kernel.register(Module("testmod", "测试模块", "测试用途？", default_enabled=True))
    state.kernel = kernel
    app = FastAPI()
    app.state.gate = state
    app.include_router(admin_router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/admin/api/login", json={"password": "a-strong-password-123"})

        # 1. PUT /modules/testmod
        r1 = await c.put("/admin/api/modules/testmod", json={"enabled": False})
        assert r1.status_code == 200
        logs1 = await db._db.execute_fetchall("SELECT action, target FROM admin_actions WHERE action='启用 / 关闭模块'")
        assert len(logs1) >= 1
        assert logs1[-1][1] == "模块 测试模块"

        # 2. POST /modules/run
        r2 = await c.post("/admin/api/modules/run")
        assert r2.status_code == 200
        logs2 = await db._db.execute_fetchall("SELECT action, target FROM admin_actions WHERE action='手动执行模块校验'")
        assert len(logs2) >= 1
        assert logs2[-1][1] == "全部模块"


@pytest.mark.asyncio
async def test_discord_bot_audit_target_and_put(tmp_path, db):
    settings = Settings(admin_password="a-strong-password-123", secret_key="s", data_dir=tmp_path, admin_cookie_secure=False)
    state = AdminState(settings)
    state.db = db
    app = FastAPI()
    app.state.gate = state
    app.include_router(admin_router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/admin/api/login", json={"password": "a-strong-password-123"})

        r = await c.put("/admin/api/discord-bot", json={
            "gallery_forum": "测试专区",
            "gallery_like": 1,
            "gallery_ai": 0,
            "gallery_ai_daily": 20,
            "gallery_ai_model": "LongCat-2.5-Preview"
        })
        assert r.status_code == 200
        assert r.json()["ok"] is True
        logs = await db._db.execute_fetchall("SELECT action, target FROM admin_actions WHERE action='修改 Discord 机器人设置'")
        assert len(logs) >= 1
        assert logs[-1][1] == "Discord 机器人设置"


@pytest.mark.asyncio
async def test_settings_announcement_and_password_audit_targets(tmp_path, db):
    settings = Settings(admin_password="a-strong-password-123", secret_key="s", data_dir=tmp_path, admin_cookie_secure=False)
    state = AdminState(settings)
    state.db = db
    app = FastAPI()
    app.state.gate = state
    app.include_router(admin_router)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/admin/api/login", json={"password": "a-strong-password-123"})

        # 1. announcement
        r1 = await c.put("/admin/api/announcement", json={"html": "<h2>欢迎</h2>"})
        assert r1.status_code == 200
        logs1 = await db._db.execute_fetchall("SELECT action, target FROM admin_actions WHERE action='修改站点公告'")
        assert len(logs1) >= 1
        assert logs1[-1][1] == "站点公告"

        # 2. password (successful change)
        r2 = await c.put("/admin/api/password", json={"current": "a-strong-password-123", "new": "a-brand-new-password-456"})
        assert r2.status_code == 200
        logs2 = await db._db.execute_fetchall("SELECT action, target FROM admin_actions WHERE action='修改后台密码'")
        assert len(logs2) >= 1
        assert logs2[-1][1] == "后台登录密码"








