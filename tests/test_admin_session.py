"""Admin cookies must honor the explicit transport compatibility setting."""

import asyncio
from http.cookies import SimpleCookie

from fastapi import FastAPI
import httpx
import pytest

from app.admin import COOKIE, router
from app.config import Settings


class AdminState:
    def __init__(self, settings):
        self.settings = settings
        self.login_attempts = 0

    async def hit_login(self, _client_id):
        self.login_attempts += 1
        return True


def admin_app(monkeypatch, tmp_path, secure_env):
    if secure_env is None:
        monkeypatch.delenv("ADMIN_COOKIE_SECURE", raising=False)
    else:
        monkeypatch.setenv("ADMIN_COOKIE_SECURE", secure_env)
    settings = Settings(
        admin_password="fixture-admin-password",
        secret_key="fixture-only-session-secret",
        data_dir=tmp_path / "unused-data",
    )
    app = FastAPI()
    app.state.gate = AdminState(settings)
    app.include_router(router)
    return app


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, True),
        ("0", False),
        ("1", True),
        ("", True),
        ("flase", True),
        ("false", False),
        ("off", False),
        (" 0 ", False),
    ],
)
def test_cookie_security_is_read_when_settings_are_created(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("ADMIN_COOKIE_SECURE", raising=False)
    else:
        monkeypatch.setenv("ADMIN_COOKIE_SECURE", value)
    assert Settings().admin_cookie_secure is expected


@pytest.mark.parametrize(
    "secure_env, scheme, expected_me",
    [
        (None, "http", 401),
        (None, "https", 200),
        ("0", "http", 200),
        ("1", "http", 401),
        ("1", "https", 200),
    ],
)
def test_login_transport_and_logout(monkeypatch, tmp_path, secure_env, scheme, expected_me):
    app = admin_app(monkeypatch, tmp_path, secure_env)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=f"{scheme}://admin.fixture",
        ) as client:
            assert (await client.get("/admin/api/me")).status_code == 401
            login = await client.post(
                "/admin/api/login", json={"password": "fixture-admin-password"}
            )
            assert login.status_code == 200
            cookie = SimpleCookie(login.headers["set-cookie"])[COOKIE]
            assert bool(cookie["secure"]) is (secure_env != "0")
            assert cookie["httponly"]
            assert cookie["samesite"].lower() == "strict"
            assert cookie["path"] == "/"
            assert cookie["max-age"] == str(7 * 86400)
            assert (await client.get("/admin/api/me")).status_code == expected_me

            logout = await client.post("/admin/api/logout")
            assert logout.status_code == 200
            cleared = SimpleCookie(logout.headers["set-cookie"])[COOKIE]
            assert bool(cleared["secure"]) is (secure_env != "0")
            assert cleared["httponly"]
            assert cleared["samesite"].lower() == "strict"
            assert cleared["path"] == cookie["path"]
            assert cleared["max-age"] == "0"
            assert (await client.get("/admin/api/me")).status_code == 401
            assert app.state.gate.login_attempts == 1

    asyncio.run(run())
    assert not app.state.gate.settings.data_dir.exists()


@pytest.mark.parametrize("secure_env", ["0", "1"])
def test_transport_setting_does_not_bypass_password_or_session(monkeypatch, tmp_path, secure_env):
    app = admin_app(monkeypatch, tmp_path, secure_env)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://admin.fixture"
        ) as client:
            response = await client.post("/admin/api/login", json={"password": "incorrect"})
            assert response.status_code == 401
            assert "set-cookie" not in response.headers
            assert (await client.get("/admin/api/me")).status_code == 401
            forged = await client.get(
                "/admin/api/me", headers={"Cookie": f"{COOKIE}=9999999999.invalid-signature"}
            )
            assert forged.status_code == 401
            assert app.state.gate.login_attempts == 1

    asyncio.run(run())
    assert not app.state.gate.settings.data_dir.exists()
