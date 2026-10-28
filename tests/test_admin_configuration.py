"""Authenticated panel configuration, defaults and credential management."""

import json
import time

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.admin import router
from app.config import Settings
from app.state import GateState


@pytest_asyncio.fixture
async def panel(tmp_path):
    state = GateState(Settings(
        data_dir=tmp_path, admin_password="fixture-password", secret_key="fixture-session-secret",
        admin_cookie_secure=False, nai_tokens=["fixture-upstream-original-credential"],
        key_inactivity_delete_days=0,
    ))
    await state.db.connect()
    await state.load_configuration()
    await state.nai.load_managed_tokens(tmp_path / "upstream_tokens.json")
    app = FastAPI()
    app.state.gate = state
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://panel.fixture"
    ) as client:
        yield state, client
    await state.db.close()


async def sign_in(client):
    assert (await client.post("/admin/api/login", json={"password": "fixture-password"})).status_code == 200
    response = await client.get("/admin/api/configuration")
    assert response.status_code == 200
    return {"X-NAI-Admin-CSRF": response.json()["csrf_token"]}


@pytest.mark.asyncio
async def test_configuration_auth_csrf_and_login_exclusion(panel):
    state, client = panel
    for path in ("/configuration", "/upstream-tokens", "/key-cleanup-preview?days=3"):
        assert (await client.get("/admin/api" + path)).status_code == 401
    assert (await client.put("/admin/api/configuration", json={"allow_img2img": True})).status_code == 401
    headers = await sign_in(client)
    snapshot = await client.get("/admin/api/configuration")
    assert snapshot.headers["cache-control"] == "no-store"
    for secret in (state.settings.admin_password, state.settings.secret_key, state.nai.pool[0].token):
        assert secret not in snapshot.text
    assert not any("login" in name or "password" in name or "cookie" in name
                   for name in snapshot.json()["values"])
    assert (await client.put("/admin/api/configuration", json={"allow_img2img": True})).status_code == 403
    assert (await client.put("/admin/api/configuration", headers=headers,
                             content=json.dumps({"allow_img2img": True}))).status_code == 415
    for forbidden in ("login_max_attempts", "login_window_seconds", "admin_password", "admin_cookie_secure"):
        assert (await client.put("/admin/api/configuration", headers=headers,
                                 json={forbidden: 1})).status_code == 422


@pytest.mark.asyncio
async def test_new_key_uses_saved_template_without_changing_existing_keys(panel):
    state, client = panel
    headers = await sign_in(client)
    existing = (await client.post("/admin/api/keys", json={"name": "existing"})).json()["key"]
    values = {"default_daily_images": 12, "default_daily_anlas": 25.5,
              "default_daily_v5": 8, "default_monthly_anlas": 300,
              "default_daily_text_tokens": 1500, "default_rpm": 6,
              "default_expires_days": 2, "default_image_model_scope": "all",
              "default_allow_anlas": True, "default_allow_img2img": True,
              "default_exclude_global_v5": True}
    response = await client.put("/admin/api/configuration", headers=headers, json=values)
    assert response.status_code == 200
    assert all(response.json()["sources"][name] == "panel" for name in values)
    key = (await client.post("/admin/api/keys", json={"name": "from template"})).json()["key"]
    for name, expected in values.items():
        if name != "default_expires_days":
            assert key[name.removeprefix("default_")] == expected
    assert abs(key["expires_at"] - time.time() - 2 * 86400) < 5
    stored = await state.db.get_key(existing["id"])
    assert stored["daily_images"] == existing["daily_images"]
    assert stored["allow_anlas"] == existing["allow_anlas"]


@pytest.mark.asyncio
async def test_cleanup_preview_is_read_only_and_never_exposes_key_tokens(panel):
    state, client = panel
    await sign_in(client)
    row = (await client.post("/admin/api/keys", json={"name": "old fixture"})).json()["key"]
    await state.db._db.execute("UPDATE api_keys SET created_at=? WHERE id=?", (time.time() - 10 * 86400, row["id"]))
    await state.db._db.commit()
    response = await client.get("/admin/api/key-cleanup-preview?days=3")
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["keys"][0]["name"] == "old fixture"
    assert row["token"] not in response.text
    assert await state.db.get_key(row["id"])
    assert state.settings.key_inactivity_delete_days == 0
    assert (await client.get("/admin/api/key-cleanup-preview?days=0")).json()["count"] == 0
    assert (await client.get("/admin/api/key-cleanup-preview?days=-1")).status_code == 422


@pytest.mark.asyncio
async def test_upstream_routes_preserve_identity_and_hide_credentials(panel):
    state, client = panel
    headers = await sign_in(client)
    raw_token = "fixture-second-upstream-private-credential"
    replacement = "fixture-replacement-upstream-private-credential"
    assert (await client.post("/admin/api/upstream-tokens", json={"token": raw_token})).status_code == 403
    response = await client.post("/admin/api/upstream-tokens", headers=headers,
                                 json={"token": raw_token, "allow_anlas": False, "v5_daily_limit": 30})
    assert response.status_code == 200
    assert raw_token not in response.text
    account = response.json()["accounts"][-1]
    identity = account["token_id"]
    path = f"/admin/api/upstream-tokens/{identity}"
    assert account["v5_daily_limit"] == 30 and not account["allow_anlas"]
    assert (await client.get("/admin/api/upstream-tokens")).json()["source"] == "panel"
    assert (await client.post("/admin/api/upstream-tokens", headers=headers,
                             json={"token": raw_token})).status_code == 409
    assert (await client.put(path + "/credential", headers=headers, json={"token": replacement})).status_code == 409
    assert (await client.request("DELETE", path, headers=headers, json={})).status_code == 409
    assert (await client.put(path + "/enabled", json={"enabled": False})).status_code == 200
    before_credentials = set(state.reconciliation.accounts())
    response = await client.put(path + "/credential", headers=headers, json={"token": replacement})
    assert response.status_code == 200
    assert replacement not in response.text
    account = response.json()["accounts"][-1]
    assert account["token_id"] == identity and not account["admin_enabled"]
    assert account["v5_daily_limit"] == 30
    assert set(state.reconciliation.accounts()) != before_credentials
    assert (await client.put(path + "/allow-anlas", headers=headers, json={"allow_anlas": True})).status_code == 200
    assert (await client.request("DELETE", path, headers=headers, json={})).status_code == 200
    assert all(row["token_id"] != identity for row in (await client.get("/admin/api/upstream-tokens")).json()["accounts"])


@pytest.mark.asyncio
async def test_upstream_api_rejects_bad_inputs_without_credentials_in_errors(panel):
    _state, client = panel
    headers = await sign_in(client)
    for body in ({}, {"token": ""}, {"token": "fixture\nprivate"},
                 {"token": "fixture-valid", "enabled": "yes"},
                 {"token": "fixture-valid", "image_concurrency": True},
                 {"token": "fixture-valid", "login_max_attempts": 100}):
        response = await client.post("/admin/api/upstream-tokens", headers=headers, json=body)
        assert response.status_code == 422
        if body.get("token"):
            assert body["token"] not in response.text
