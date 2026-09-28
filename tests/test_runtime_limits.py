"""Safe panel limits persist and apply without restarting the gateway."""

from fastapi import FastAPI
import httpx
import pytest

from app.admin import router
from app.config import Settings
from app.state import GateState


@pytest.mark.asyncio
async def test_admin_runtime_limits_are_validated_and_survive_restart(tmp_path):
    def make_state():
        return GateState(Settings(
            admin_password="fixture-password", secret_key="fixture-secret",
            admin_cookie_secure=False, nai_tokens=["fixture-upstream"],
            data_dir=tmp_path,
        ))

    state = make_state()
    await state.db.connect()
    app = FastAPI()
    app.state.gate = state
    app.include_router(router)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fixture.invalid"
        ) as client:
            path = "/admin/api/runtime-limits"
            assert (await client.get(path)).status_code == 401
            assert (await client.post("/admin/api/login", json={"password": "fixture-password"})).status_code == 200
            for bad in ({}, {"queue_timeout": 0}, {"queue_timeout": True},
                        {"unknown": 15}, {"image_429_cooldown_seconds": 59}):
                assert (await client.put(path, json=bad)).status_code == 422
            values = {"queue_timeout": 120, "key_image_min_interval": 25,
                      "image_min_interval": 25, "image_429_cooldown_seconds": 90}
            assert (await client.put(path, json=values)).json() == values
            assert state.settings.queue_timeout == 120
            assert state.nai._image_min_interval == 25
            assert (await client.get(path)).json() == values
    finally:
        await state.db.close()

    restored = make_state()
    await restored.db.connect()
    try:
        await restored.load_runtime_limits()
        assert restored.runtime_limits_snapshot() == values
        assert restored.nai._image_min_interval == 25
    finally:
        await restored.db.close()
