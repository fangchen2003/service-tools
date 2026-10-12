"""Alerts, brute-force limiter, audit logging and member/audit admin endpoints."""
import asyncio
import io
import time
import zipfile
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
import pytest_asyncio
from PIL import Image

from app.admin import router
from app.alerts import Alerter
from app.audit import THUMB_SIDE, make_thumbnail, prompt_texts
from app.config import Settings
from app.database import Database
from app.state import GateState


def png_zip(size=(640, 960)) -> bytes:
    raw = io.BytesIO()
    Image.new("RGB", size, (200, 40, 40)).save(raw, "PNG")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("image_0.png", raw.getvalue())
    return out.getvalue()


def test_thumbnail_is_small_jpeg_from_zip_and_bad_input_is_none():
    thumb = make_thumbnail(png_zip())
    assert thumb and thumb[:2] == b"\xff\xd8" and len(thumb) < 60000
    with Image.open(io.BytesIO(thumb)) as image:
        assert max(image.size) <= THUMB_SIDE
    assert make_thumbnail(b"not an image") is None
    assert make_thumbnail(b"PK\x03\x04broken") is None


def test_prompt_texts_truncates_and_falls_back_to_v4_caption():
    body = {"input": "x" * 5000, "parameters": {"negative_prompt": "n" * 5000}}
    positive, negative = prompt_texts(body)
    assert len(positive) == 2000 and len(negative) == 1000
    body = {"parameters": {"v4_prompt": {"caption": {"base_caption": "from caption"}}}}
    assert prompt_texts(body)[0] == "from caption"


@pytest.mark.asyncio
async def test_alerter_dedupes_by_kind_and_never_raises(monkeypatch):
    sent = []
    alerter = Alerter(webhook_url="https://example.invalid/hook")

    async def fake_send(text):
        sent.append(text)
    monkeypatch.setattr(alerter, "_send", fake_send)
    alerter.notify("a", "one", cooldown=60)
    alerter.notify("a", "two", cooldown=60)      # suppressed by cooldown
    alerter.notify("b", "three", cooldown=60)
    await asyncio.sleep(0.05)
    assert len(sent) == 2 and "one" in sent[0] and "three" in sent[1]
    assert Alerter().configured is False
    Alerter().notify("x", "unconfigured is a no-op")


@pytest.mark.asyncio
async def test_failed_key_limiter_blocks_ip_after_threshold_and_alerts(tmp_path):
    settings = Settings(auth_fail_max=3, auth_fail_window=60, auth_block_seconds=120,
                        data_dir=tmp_path)
    state = GateState(settings)
    seen = []
    state.alerter.notify = lambda kind, msg, cooldown=0: seen.append(kind)
    for _ in range(2):
        state.record_auth_failure("1.2.3.4")
    assert state.auth_blocked("1.2.3.4") == 0
    state.record_auth_failure("1.2.3.4")
    assert 0 < state.auth_blocked("1.2.3.4") <= 120
    assert state.auth_blocked("5.6.7.8") == 0           # other IPs unaffected
    assert seen == ["auth_flood"]


@pytest_asyncio.fixture
async def db():
    value = Database(":memory:")
    await value.connect()
    yield value
    await value.close()


@pytest.mark.asyncio
async def test_audit_roundtrip_filter_and_purge(db):
    key = await db.create_key(dict(name="m1", token="t1", daily_images=5, monthly_anlas=0,
                                   daily_text_tokens=0, rpm=5))
    await db.add_audit(key["id"], "m1", "image", "nai-diffusion-4-5-full", "ok", "a cat", "lowres", b"\xff\xd8thumb")
    await db.add_audit(key["id"], "m1", "image", "nai-diffusion-4-5-full", "error", "a dog", "", None)
    rows, total = await db.list_audit(10, 0, key["id"])
    assert total == 2 and [r["prompt"] for r in rows] == ["a dog", "a cat"] and rows[1]["has_thumb"] == 1
    assert await db.audit_thumb(rows[1]["id"]) == b"\xff\xd8thumb"
    assert await db.audit_thumb(rows[0]["id"]) is None
    assert (await db.list_audit(10, 0, 999))[1] == 0
    assert await db.purge_audit(time.time() + 10) == 2
    assert (await db.list_audit(10, 0))[1] == 0


@pytest.mark.asyncio
async def test_members_audit_status_endpoints_require_session_and_return_data(tmp_path, db):
    settings = Settings(admin_password="a-strong-password-123", secret_key="s", data_dir=tmp_path,
                        admin_cookie_secure=False, audit_prompts=True, audit_thumbs=True)
    key = await db.create_key(dict(name="member-one", token="t-one", daily_images=30, monthly_anlas=0,
                                   daily_text_tokens=0, rpm=5))
    await db.bump_counters(key["id"], "2026-10-09", images=3)
    await db.add_audit(key["id"], "member-one", "image", "m", "ok", "<b>prompt</b>", "", b"\xff\xd8x")

    class State:
        def __init__(self):
            self.settings, self.db = settings, db
            self.alerter = Alerter()
            self.announcer = Alerter()

        def upstream_health(self):
            return {"status": "idle", "recent": 0, "failed": 0, "image_cooldown_seconds": 0}

        async def hit_login(self, _):
            return True

        def day(self):
            return "2026-10-09"

        def week_days(self, n=7):
            return [f"2026-10-0{i}" for i in range(3, 10)]

    app = FastAPI()
    app.state.gate = State()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        for path in ("/admin/api/members", "/admin/api/audit", "/admin/api/audit/1/thumb", "/admin/api/status"):
            assert (await client.get(path)).status_code == 401
        assert (await client.post("/admin/api/login", json={"password": "a-strong-password-123"})).status_code == 200
        members = (await client.get("/admin/api/members")).json()["members"]
        assert members[0]["name"] == "member-one" and members[0]["today"]["images"] == 3
        assert members[0]["week"]["images"] == 3 and members[0]["discord_id"] is None
        audit = (await client.get("/admin/api/audit")).json()
        assert audit["total"] == 1 and audit["items"][0]["prompt"] == "<b>prompt</b>"
        thumb = await client.get("/admin/api/audit/1/thumb")
        assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
        assert (await client.get("/admin/api/audit/99/thumb")).status_code == 404
        status = (await client.get("/admin/api/status")).json()
        assert status["audit"]["prompts"] is True and status["alerts"]["configured"] is False
        assert (await client.post("/admin/api/alerts/test", headers={"Origin": "http://t"})).status_code == 409


@pytest.mark.asyncio
async def test_feature_flags_global_and_per_key(db):
    from app import features
    key = await db.create_key(dict(name="k", token="t", daily_images=5, monthly_anlas=0,
                                   daily_text_tokens=0, rpm=5, features="image"))
    legacy = await db.create_key(dict(name="old", token="t2", daily_images=5, monthly_anlas=0,
                                      daily_text_tokens=0, rpm=5))
    assert await features.check(db, key, "image") is None
    assert "暂无权" in await features.check(db, key, "text")           # not granted to this key
    assert await features.check(db, legacy, "text") is None             # NULL = legacy behaviour
    from app import ops
    await ops.set_global_features(db, {"image": False})
    assert "暂未开放" in await features.check(db, key, "image")        # global switch beats grant
    assert "暂未开放" in await features.check(db, legacy, "image")
    admin = dict(is_admin=1, features=None)
    assert await features.check(db, admin, "image") is None              # admin bypass


@pytest.mark.asyncio
async def test_registration_runtime_settings_override_env_defaults(db):
    from app import ops
    from app.registration import RegistrationService
    service = RegistrationService(db, None, client_id="c", client_secret="s", bot_token="b", bridge_secret="x",
                                  key_daily_images=30, key_daily_v5=0, max_users=10)
    cfg = await ops.registration_settings(db, service)
    assert (cfg["open"], cfg["max_users"], cfg["daily_images"]) == (False, 10, 30)    # closed until the owner opens it
    await ops.set_registration(db, {"open": True})
    assert (await ops.registration_settings(db, service))["open"] is True
    await ops.set_registration(db, {"open": False, "max_users": 3, "features": ["image", "text"], "daily_v5": 15})
    cfg = await ops.registration_settings(db, service)
    assert (cfg["open"], cfg["max_users"], cfg["daily_v5"], cfg["features"]) == (False, 3, 15, ["image", "text"])
    with pytest.raises(Exception):
        await service.issue_direct("1", service.command_guild)           # closed -> refuses
    with pytest.raises(ValueError):
        await ops.set_registration(db, {"max_users": 5000})


@pytest.mark.asyncio
async def test_registration_changes_are_announced_to_members(db):
    from app import ops

    class Announcer:
        def __init__(self):
            self.sent = []

        def post(self, text):
            self.sent.append(text)

    state = SimpleNamespace(announcer=Announcer())
    await ops.set_registration(db, {"open": True}, state)
    await ops.set_registration(db, {"open": False}, state)
    await ops.set_registration(db, {"max_users": 10, "open": True}, state)
    await ops.set_registration(db, {"daily_images": 50}, state)             # not a headcount/open change -> silent
    await ops.set_registration(db, {"max_users": 12, "notify": False}, state)   # explicitly silent
    await ops.set_registration(db, {"max_users": 15}, state)
    texts = state.announcer.sent
    assert len(texts) == 4 and "已开放" in texts[0] and "已暂停" in texts[1] and "已开放" in texts[2] and "10" in texts[2]
    assert "已调整" in texts[3] and "15" in texts[3]


@pytest.mark.asyncio
async def test_action_log_filters_by_actor_and_action(tmp_path, db):
    from app.action_log import log_action
    settings = Settings(admin_password="a-strong-password-123", secret_key="s", data_dir=tmp_path,
                        admin_cookie_secure=False)
    await log_action(db, "后台 *.*.1.2", "重置今日额度", "Key #7 甲", "", ok=True)
    await log_action(db, "系统", "动态额度应用", "40 把 Key", "", ok=True)
    await log_action(db, "Discord:123", "网页登录", "", "", ok=True)
    await log_action(db, "后台 *.*.1.2", "重新生成 Key", "Key #7 甲", "", ok=True)

    class State:
        def __init__(self):
            self.settings, self.db = settings, db

        async def hit_login(self, _):
            return True

    app = FastAPI()
    app.state.gate = State()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.get("/admin/api/actions")).status_code == 401
        assert (await client.post("/admin/api/login", json={"password": "a-strong-password-123"})).status_code == 200
        alld = (await client.get("/admin/api/actions")).json()
        # 登录本身也记一条「登录后台」，所以总数 >= 我们插入的 4 条
        assert alld["total"] >= 4 and "重置今日额度" in alld["action_kinds"]
        admin_only = (await client.get("/admin/api/actions?actor=admin")).json()
        assert all(a["actor"].startswith("后台") for a in admin_only["actions"])
        assert any(a["action"] == "重置今日额度" for a in admin_only["actions"])
        system_only = (await client.get("/admin/api/actions?actor=system")).json()
        assert system_only["total"] == 1 and system_only["actions"][0]["actor"] == "系统"
        member_only = (await client.get("/admin/api/actions?actor=member")).json()
        assert member_only["total"] == 1 and member_only["actions"][0]["actor"].startswith("Discord:")
        reset_only = (await client.get("/admin/api/actions?action=%E9%87%8D%E7%BD%AE%E4%BB%8A%E6%97%A5%E9%A2%9D%E5%BA%A6")).json()
        assert reset_only["total"] == 1 and reset_only["actions"][0]["action"] == "重置今日额度"
        combo = (await client.get("/admin/api/actions?actor=admin&action=%E9%87%8D%E7%BD%AE%E4%BB%8A%E6%97%A5%E9%A2%9D%E5%BA%A6")).json()
        assert combo["total"] == 1
