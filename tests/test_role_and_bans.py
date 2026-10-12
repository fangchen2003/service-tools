"""Tests for role switching (admin, test, invite, regular) and bans management."""
import time
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.routing import Mount

from app.config import Settings
from app.database import (
    Database, INVITE_TAG, ROLE_ADMIN, ROLE_TEST, ROLE_INVITE, ROLE_REGULAR, ROLE_LABELS
)
from app.state import GateState
from app.admin import router as admin_router


@pytest_asyncio.fixture
async def db():
    value = Database(":memory:")
    await value.connect()
    try:
        yield value
    finally:
        await value.close()


@pytest_asyncio.fixture
async def state(db):
    s = GateState(Settings(key_inactivity_delete_days=3, admin_password="test-password", admin_cookie_secure=False))
    s.db = db
    return s


@pytest_asyncio.fixture
async def client(state):
    from fastapi import FastAPI
    app = FastAPI()
    app.state.gate = state
    app.include_router(admin_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver", headers={"Origin": "http://testserver"}) as c:
        # 登录获取 session
        r = await c.post("/admin/api/login", json={"password": "test-password"})
        assert r.status_code == 200
        yield c


@pytest.mark.asyncio
async def test_key_role_transitions(db):
    row = await db.create_key({
        "name": "u1", "token": "nai-role-1", "daily_images": 10,
        "daily_anlas": 0, "monthly_anlas": 0, "daily_text_tokens": 0, "rpm": 5
    })
    key_id = row["id"]
    k = await db.get_key(key_id)
    assert await db.get_key_role(k) == ROLE_REGULAR

    # 切换为邀请
    await db.set_key_role(key_id, ROLE_INVITE)
    k = await db.get_key(key_id)
    assert await db.get_key_role(k) == ROLE_INVITE
    assert (await db._db.execute_fetchall("SELECT 1 FROM key_tags WHERE key_id=? AND tag=?", (key_id, INVITE_TAG))) != []

    # 切换为测试
    await db.set_key_role(key_id, ROLE_TEST)
    k = await db.get_key(key_id)
    assert await db.get_key_role(k) == ROLE_TEST
    assert k["is_test"] == 1
    assert (await db._db.execute_fetchall("SELECT 1 FROM key_tags WHERE key_id=? AND tag=?", (key_id, INVITE_TAG))) == []

    # 切换为站长
    await db.set_key_role(key_id, ROLE_ADMIN)
    k = await db.get_key(key_id)
    assert await db.get_key_role(k) == ROLE_ADMIN
    assert k["is_admin"] == 1
    assert k["is_test"] == 0

    # 切换回普通
    await db.set_key_role(key_id, ROLE_REGULAR)
    k = await db.get_key(key_id)
    assert await db.get_key_role(k) == ROLE_REGULAR
    assert k["is_admin"] == 0
    assert k["is_test"] == 0
    assert (await db._db.execute_fetchall("SELECT 1 FROM key_tags WHERE key_id=? AND tag=?", (key_id, INVITE_TAG))) == []


@pytest.mark.asyncio
async def test_role_api_endpoint(client, state):
    row = await state.db.create_key({
        "name": "u2", "token": "nai-role-2", "daily_images": 10,
        "daily_anlas": 0, "monthly_anlas": 0, "daily_text_tokens": 0, "rpm": 5
    })
    key_id = row["id"]

    # 切为邀请
    r = await client.post(f"/admin/api/keys/{key_id}/role", json={"role": "invite"})
    assert r.status_code == 200
    assert r.json()["role"] == "invite"
    assert r.json()["role_label"] == "邀请"

    # 切为普通
    r = await client.post(f"/admin/api/keys/{key_id}/role", json={"role": "regular"})
    assert r.status_code == 200
    assert r.json()["role"] == "regular"

    # 非法身份组
    r = await client.post(f"/admin/api/keys/{key_id}/role", json={"role": "invalid"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_inactivity_cleanup_bans_to_discord_bans(state):
    # 创建一个领了 Key 超过 3 天未活动的用户
    row = await state.db.create_key({
        "name": "idle_user", "token": "nai-idle-1", "daily_images": 10,
        "daily_anlas": 0, "monthly_anlas": 0, "daily_text_tokens": 0, "rpm": 5
    })
    key_id = row["id"]
    four_days_ago = time.time() - 4 * 86400
    await state.db._db.execute("UPDATE api_keys SET created_at=?, last_used_at=NULL WHERE id=?", (four_days_ago, key_id))
    await state.db._db.execute(
        "INSERT INTO discord_registrations(discord_id, key_id, created_at, username, display_name) VALUES (?, ?, ?, ?, ?)",
        ("987654321", key_id, four_days_ago, "idle_dc", "闲置老哥")
    )
    await state.db._db.commit()

    # 执行回收
    deleted = await state.delete_inactive_keys()
    assert deleted == 1

    # Key 已被删除
    assert await state.db.get_key(key_id) is None
    # 领取记录已释放
    left = await state.db._db.execute_fetchall("SELECT 1 FROM discord_registrations WHERE discord_id='987654321'")
    assert left == []
    # 用户已被自动拉黑，并附带原因和资料
    bans = await state.db.list_bans("987654321")
    assert len(bans) == 1
    assert bans[0]["discord_id"] == "987654321"
    assert "3 天未活动自动回收" in bans[0]["reason"]
    assert bans[0]["username"] == "idle_dc"
    assert bans[0]["display_name"] == "闲置老哥"
    assert bans[0]["banned_by"] == "系统"


@pytest.mark.asyncio
async def test_bans_api_management(client, state):
    # 添加黑名单
    r = await client.post("/admin/api/bans", json={
        "discord_id": "1122334455",
        "reason": "测试封禁"
    })
    assert r.status_code == 200

    # 查黑名单
    r = await client.get("/admin/api/bans")
    assert r.status_code == 200
    bans = r.json()["bans"]
    assert any(b["discord_id"] == "1122334455" for b in bans)

    # 搜索
    r = await client.get("/admin/api/bans?q=112233")
    assert r.status_code == 200
    assert len(r.json()["bans"]) == 1

    # 移出黑名单（解封）
    r = await client.delete("/admin/api/bans/1122334455")
    assert r.status_code == 200

    # 再次查询为空
    r = await client.get("/admin/api/bans?q=1122334455")
    assert len(r.json()["bans"]) == 0
