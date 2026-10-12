"""Admin hardening: origin check, session invalidation, weak password, input errors."""

from fastapi import FastAPI
import httpx
import pytest

from app.admin import COOKIE, router
from app.config import Settings
from tests.test_admin_session import AdminState


def make_client(tmp_path, password="a-strong-password-123"):
    settings = Settings(admin_password=password, secret_key="fixture-secret",
                        data_dir=tmp_path, admin_cookie_secure=False)
    app = FastAPI()
    app.state.gate = AdminState(settings)
    app.include_router(router)
    return app, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://admin.fixture")


@pytest.mark.asyncio
async def test_cross_origin_writes_are_rejected_even_with_valid_session(tmp_path):
    app, client = make_client(tmp_path)
    async with client:
        assert (await client.post("/admin/api/login", json={"password": "a-strong-password-123"})).status_code == 200
        for origin in ("http://evil.admin.fixture", "https://sibling.example", "null"):
            r = await client.post("/admin/api/keys/1/regenerate", headers={"Origin": origin})
            assert r.status_code == 403
            r = await client.post("/admin/api/login", json={"password": "x"}, headers={"Origin": origin})
            assert r.status_code == 403
        # same-origin and non-browser requests still reach the auth check / handler
        assert (await client.get("/admin/api/me", headers={"Origin": "http://evil.example"})).status_code == 200
        assert (await client.post("/admin/api/logout", headers={"Origin": "http://admin.fixture"})).status_code == 200


@pytest.mark.asyncio
async def test_password_change_invalidates_existing_sessions(tmp_path):
    app, client = make_client(tmp_path)
    async with client:
        await client.post("/admin/api/login", json={"password": "a-strong-password-123"})
        assert (await client.get("/admin/api/me")).status_code == 200
        app.state.gate.settings.admin_password = "another-strong-password"
        assert (await client.get("/admin/api/me")).status_code == 401


def test_non_ascii_signature_is_rejected_not_raised(tmp_path):
    from types import SimpleNamespace
    from app.admin import check_session
    settings = Settings(admin_password="p", secret_key="s", data_dir=tmp_path)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(gate=SimpleNamespace(settings=settings))),
                          cookies={COOKIE: "123.\u00e9\u00e9"})
    assert check_session(req) is False


@pytest.mark.asyncio
async def test_example_password_cannot_log_in(tmp_path):
    _, client = make_client(tmp_path, password="changeme-please")
    async with client:
        r = await client.post("/admin/api/login", json={"password": "changeme-please"})
        assert r.status_code == 503


def test_secret_key_file_is_private(tmp_path):
    from types import SimpleNamespace
    from app.admin import _secret
    settings = Settings(admin_password="p", secret_key="", data_dir=tmp_path)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(gate=SimpleNamespace(settings=settings))))
    _secret(req)
    assert (tmp_path / "secret_key").stat().st_mode & 0o777 == 0o600
