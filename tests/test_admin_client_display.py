import asyncio
import pytest
from app.database import Database
from app.config import Settings
from fastapi import FastAPI
import httpx
from app.admin import router, COOKIE


@pytest.mark.asyncio
async def test_latest_member_clients_query(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    try:
        await db._db.execute("INSERT INTO api_keys(id, name, token, created_at) VALUES (1, 'user1', 'tok1', 100)")
        await db._db.execute("INSERT INTO api_keys(id, name, token, created_at) VALUES (2, 'user2', 'tok2', 100)")
        await db._db.commit()

        # user1 has two logs with different clients
        await db._db.execute(
            "INSERT INTO usage_log(id, ts, key_id, kind, status, client) VALUES (10, 100, 1, 'image', 'ok', 'Mozilla/5.0 Chrome/100')"
        )
        await db._db.execute(
            "INSERT INTO usage_log(id, ts, key_id, kind, status, client) VALUES (11, 200, 1, 'image', 'ok', 'Mozilla/5.0 Edg/120')"
        )

        # user2 has an empty client log followed by no other logs
        await db._db.execute(
            "INSERT INTO usage_log(id, ts, key_id, kind, status, client) VALUES (12, 100, 2, 'image', 'ok', '')"
        )
        await db._db.commit()

        clients = await db.latest_member_clients()
        assert clients.get(1) == "Mozilla/5.0 Edg/120"
        assert 2 not in clients
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_admin_members_returns_last_client(tmp_path):
    from types import SimpleNamespace
    db = Database(str(tmp_path / "test_admin.db"))
    await db.connect()
    try:
        await db._db.execute("INSERT INTO api_keys(id, name, token, created_at) VALUES (1, 'alice', 'tok-a', 100)")
        await db._db.execute(
            "INSERT INTO usage_log(id, ts, key_id, kind, status, client) VALUES (1, 100, 1, 'image', 'ok', 'SillyTavern/1.0')"
        )
        await db._db.commit()

        settings = Settings(admin_password="test-password-123", secret_key="test-secret", data_dir=tmp_path, admin_cookie_secure=False)
        app = FastAPI()
        async def fake_hit_login(ip):
            return True

        gate = SimpleNamespace(
            db=db,
            settings=settings,
            day=lambda: "2026-10-11",
            week_days=lambda n: ["2026-10-11"],
            hit_login=fake_hit_login,
        )
        app.state.gate = gate
        app.include_router(router)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            login_resp = await client.post("/admin/api/login", json={"password": "test-password-123"})
            assert login_resp.status_code == 200

            resp = await client.get("/admin/api/members")
            assert resp.status_code == 200
            data = resp.json()
            members = data.get("members", [])
            assert len(members) == 1
            assert members[0]["name"] == "alice"
            assert members[0].get("last_client") == "SillyTavern/1.0"
    finally:
        await db.close()
