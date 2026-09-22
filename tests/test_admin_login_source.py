"""Login buckets must follow the server's peer/proxy trust, not raw headers."""
import asyncio

from fastapi import FastAPI
import httpx
import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.admin import router
from app.config import Settings
from app.state import GateState


def make_app():
    app = FastAPI()
    app.state.gate = GateState(Settings(
        admin_password="fixture-password", secret_key="fixture-secret",
        login_max_attempts=5, login_window_seconds=300,
    ))
    app.include_router(router)
    return app


@pytest.mark.parametrize("trusted", [False, True])
def test_rotating_untrusted_headers_does_not_reset_login_limit(trusted):
    app = make_app()
    peer = "10.0.0.2" if trusted else "198.51.100.20"
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=["10.0.0.2"])

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=wrapped, client=(peer, 1234)),
            base_url="http://fixture.invalid",
        ) as client:
            statuses = []
            for i in range(8):
                headers = {"X-Real-IP": f"203.0.113.{i}",
                           "X-Forwarded-For": "198.51.100.20" if trusted else f"203.0.113.{i}",
                           "Forwarded": f"for=203.0.113.{i}"}
                response = await client.post("/admin/api/login", headers=headers,
                                             json={"password": "wrong"})
                statuses.append(response.status_code)
                assert "set-cookie" not in response.headers
            assert statuses == [401] * 5 + [429] * 3
            assert len(app.state.gate._login_attempts) == 1
    asyncio.run(run())


def test_different_peers_have_independent_login_limits():
    app = make_app()

    async def run():
        for peer in ("198.51.100.20", "198.51.100.21"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=(peer, 1234)),
                base_url="https://fixture.invalid",
            ) as client:
                for _ in range(5):
                    assert (await client.post("/admin/api/login", json={"password": "wrong"})).status_code == 401
                assert (await client.post("/admin/api/login", json={"password": "fixture-password"})).status_code == 429
        assert len(app.state.gate._login_attempts) == 2
    asyncio.run(run())


def test_every_protected_admin_route_rejects_missing_or_forged_session():
    app = make_app()

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://fixture.invalid",
        ) as client:
            for route in router.routes:
                if route.path.endswith(("/login", "/logout")):
                    continue
                path = route.path.replace("{key_id}", "1")
                for method in route.methods:
                    for headers in ({}, {"Cookie": "nai_gate_admin=9999999999.forged"}):
                        response = await client.request(method, path, headers=headers)
                        assert response.status_code == 401, (method, path, response.status_code)
    asyncio.run(run())
