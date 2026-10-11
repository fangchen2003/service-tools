"""Landing page serving, public status and sandboxed announcement."""
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app import main
from app.database import Database


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    db = Database(":memory:")
    await db.connect()
    monkeypatch.setattr(main.SETTINGS, "data_dir", tmp_path)
    announcement = main.SETTINGS.announcement_path
    monkeypatch.setattr(main.SETTINGS, "site_url", "https://gate.example.top/", raising=False)
    monkeypatch.setattr(main.SETTINGS, "discord_invite_url", "https://discord.gg/abc", raising=False)
    state = SimpleNamespace(db=db, upstream_health=lambda: {
        "status": "degraded", "recent": 9, "failed": 6, "image_cooldown_seconds": 42})
    monkeypatch.setattr(main, "STATE", state)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="https://gate.example.top")
    yield SimpleNamespace(client=client, db=db, announcement=announcement)
    await client.aclose()
    await db.close()


@pytest.mark.asyncio
async def test_landing_page_is_served_with_strict_csp_and_no_admin_link(env):
    response = await env.client.get("/")
    assert response.status_code == 200 and "text/html" in response.headers["content-type"]
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "/admin" not in response.text           # members never see an admin link


@pytest.mark.asyncio
async def test_public_status_exposes_only_safe_fields(env):
    main._PUBLIC_STATUS_CACHE["body"] = None
    response = await env.client.get("/public/status")
    data = response.json()
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert data["upstream"] == {"status": "degraded", "image_cooldown_seconds": 42}   # no counts leaked
    assert data["site"] == "https://gate.example.top" and data["discord_invite"] == "https://discord.gg/abc"
    assert data["registration"] == {"open": False, "slots_left": None}               # no registrar configured
    assert data["has_announcement"] is False
    assert [f["id"] for f in data["default_features"]] == ["image"]                   # 与后台新建 Key 默认一致
    assert set(data) == {"site", "upstream", "registration", "default_features", "audit_notice", "algo_notice",
                         "discord_invite", "has_announcement", "key_inactivity_delete_days", "image_jobs", "stability",
                         "limits", "web_login"}   # limits 只含排队数、保底张数和安静时段，不含用量
    assert data["web_login"] is False                                 # 默认：网页登录暂停（fail-closed）


@pytest.mark.asyncio
async def test_announcement_is_sandboxed_and_404_when_empty(env):
    assert (await env.client.get("/announcement")).status_code == 404
    env.announcement.write_text("<h1>hi</h1><script>alert(1)</script>", encoding="utf-8")
    response = await env.client.get("/announcement")
    assert response.status_code == 200 and "<h1>hi</h1>" in response.text
    assert response.headers["content-security-policy"].startswith("sandbox")          # scripts cannot run
    main._PUBLIC_STATUS_CACHE["body"] = None
    assert (await env.client.get("/public/status")).json()["has_announcement"] is True
    await env.db.set_settings_bulk({"audit_prompts": "1", "audit_thumbs": "1", "audit_retention_days": 7})
    main._PUBLIC_STATUS_CACHE["body"] = None
    assert "7 天" in (await env.client.get("/public/status")).json()["audit_notice"]
