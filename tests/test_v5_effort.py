"""Effort routing against the real quota ledger and a strictly local fake upstream."""
import asyncio
import copy
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app import main
from app.config import Settings
from app.database import Database
from app.state import GateState
from app.v5_effort import HEAVY_UC, MEDIUM_MODEL
from test_generation_integration import image_body, post
from test_nai_integration import PNG
from test_image_stream_routes import event


@pytest_asyncio.fixture
async def effort_env(tmp_path, monkeypatch):
    config = Settings(data_dir=tmp_path, nai_tokens=["offline-token"],
                      key_image_min_interval=0, image_min_interval=0,
                      global_daily_v5=10, key_concurrency=2, max_steps=28,
                      image_host="https://offline.invalid", allow_img2img=True,
                      admin_password="offline-password", admin_cookie_secure=False)
    state = GateState(config)
    await state.db.connect()
    for index in (1, 2):
        await state.db.create_key(dict(token=f"fixture-{index}", name="test",
            daily_v5=10, image_model_scope="all", daily_images=100, rpm=100,
            daily_text_tokens=0, allow_anlas=True, allow_img2img=True,
            daily_anlas=1000, monthly_anlas=10000))
    env = SimpleNamespace(state=state, calls=[], wait=None, status=200, subscription_wait=None)

    async def upstream(request):
        if request.method == "GET":
            if env.subscription_wait:
                await env.subscription_wait.wait()
            return httpx.Response(200, json=dict(active=True, tier=3,
                usage=dict(percent=80, isNegative=False)))
        env.calls.append(json.loads(request.content))
        if env.wait:
            await env.wait.wait()
        return httpx.Response(env.status,
            content=event() if request.url.path.endswith("-stream") else PNG,
            headers={"content-type": "text/event-stream" if request.url.path.endswith("-stream") else "application/octet-stream"})

    state.nai._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    monkeypatch.setattr(main, "STATE", state)
    monkeypatch.setattr(main.app.state, "gate", state, raising=False)
    yield env
    await state.nai.close()
    await state.db.close()


def v5_body(**parameters):
    return {**image_body(**parameters), "model": "nai-diffusion-5-full"}


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_low_budget_medium_normalization_and_all_three_ledgers(effort_env, streaming):
    st = effort_env.state
    await st.db.bump_counters(2, st.day(), v5=8, images=8)
    body = v5_body(steps=45, sampler="k_dpmpp_2m", cfg_rescale=.8,
        negative_prompt="remove hat", uc="remove hat", ucPreset=3,
        v4_negative_prompt={"caption": {"base_caption": "remove hat", "char_captions": [
            {"char_caption": "no red dress", "centers": [{"x": .2, "y": .5}]}]}})
    original = copy.deepcopy(body)
    r = await post("/ai/generate-image" + ("-stream" if streaming else ""), body)
    assert r.status_code == 200, r.text
    sent, = effort_env.calls
    assert sent["model"] == MEDIUM_MODEL
    p = sent["parameters"]
    assert p["steps"] == 14 and p["sampler"] == "k_euler_ancestral"
    assert "cfg_rescale" not in p and "uc" not in p and "ucPreset" not in p
    assert p["negative_prompt"] == p["v4_negative_prompt"]["caption"]["base_caption"] == HEAVY_UC
    assert p["v4_negative_prompt"]["caption"]["char_captions"][0]["char_caption"] == ""
    assert body == original
    personal = await st.db.get_counter(1, st.day())
    assert personal["v5"] == .6 and personal["images"] == 1
    assert await st.db.day_v5_total(st.day()) == 8.6
    token = st.nai.pool[0]
    assert await st.db.get_upstream_v5_counter(token.token_id, st.day()) == .6
    assert token.pending_v5 == 0
    logs = await st.db.list_logs(key_id=1)
    assert logs[0]["model"] == MEDIUM_MODEL and "Rescale" in logs[0]["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["nai-diffusion-3", "nai-diffusion-4-5-full", "nai-diffusion-5-full", "nai-diffusion-5-curated"])
@pytest.mark.parametrize("paid", [False, True])
async def test_all_models_step_cap_even_without_safe_clamp(effort_env, model, paid):
    st = effort_env.state
    st.settings.safe_clamp = False
    await st.db.update_key(1, {"allow_anlas": paid})
    body = {**image_body(steps=50), "model": model}
    r = await post("/ai/generate-image", body)
    assert r.status_code == 200, r.text
    assert effort_env.calls[0]["parameters"]["steps"] == 23


@pytest.mark.asyncio
async def test_admin_exemption_and_curated_not_changed(effort_env):
    st = effort_env.state
    await st.db.bump_counters(2, st.day(), v5=8)
    await st.db._db.execute("UPDATE api_keys SET is_admin=1 WHERE id=1")
    await st.db._db.commit()
    assert (await post("/ai/generate-image", v5_body(steps=25))).status_code == 200
    assert effort_env.calls[0]["parameters"]["steps"] == 25
    assert effort_env.calls[0]["model"] == "nai-diffusion-5-full"
    await st.db._db.execute("UPDATE api_keys SET is_admin=0 WHERE id=1")
    await st.db._db.commit()
    body = {**v5_body(), "model": "nai-diffusion-5-curated"}
    assert (await post("/ai/generate-image", body)).status_code == 200
    assert effort_env.calls[-1]["model"] == "nai-diffusion-5-curated"


@pytest.mark.asyncio
async def test_fractional_quota_cannot_be_overdrawn_by_two_users(effort_env):
    st = effort_env.state
    await st.db.bump_counters(2, st.day(), v5=9.4)
    results = await asyncio.gather(*(post("/ai/generate-image", v5_body(), f"fixture-{i}") for i in (1, 2)))
    assert sorted(r.status_code for r in results) == [200, 402]
    assert len(effort_env.calls) == 1 and await st.db.day_v5_total(st.day()) == 10


@pytest.mark.asyncio
async def test_upstream_fractional_limit_reserves_requested_cost(effort_env):
    st = effort_env.state
    token = st.nai.pool[0]
    token.v5_daily_limit = 1
    await st.db.bump_upstream_v5_counter(token.token_id, st.day(), .4)
    body = {**v5_body(), "model": MEDIUM_MODEL}
    assert (await post("/ai/generate-image", body)).status_code == 200
    assert (await post("/ai/generate-image", body)).status_code == 429
    assert await st.db.get_upstream_v5_counter(token.token_id, st.day()) == 1
    assert len(effort_env.calls) == 1


@pytest.mark.asyncio
async def test_switch_rechecked_after_upstream_queue_wait(effort_env):
    st = effort_env.state
    token = st.nai.pool[0]
    await token.image_slots.acquire()
    task = asyncio.create_task(post("/ai/generate-image", v5_body()))
    try:
        for _ in range(100):
            if token.pending_v5:
                break
            await asyncio.sleep(.001)
        assert token.pending_v5 == 1
        await st.db.bump_counters(2, st.day(), v5=8)
    finally:
        token.image_slots.release()
    response = await task
    assert response.status_code == 200
    assert effort_env.calls[0]["model"] == MEDIUM_MODEL
    assert await st.db.get_upstream_v5_counter(token.token_id, st.day()) == .6


@pytest.mark.asyncio
async def test_reset_preserves_global_usage_and_migration_is_idempotent(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as old:
        old.execute("CREATE TABLE counters (key_id INTEGER,day TEXT,images INTEGER DEFAULT 0,anlas REAL DEFAULT 0,text_tokens INTEGER DEFAULT 0,requests INTEGER DEFAULT 0,v5 INTEGER DEFAULT 0,legacy_free_images INTEGER DEFAULT 0,PRIMARY KEY(key_id,day))")
        old.execute("INSERT INTO counters(key_id,day,images,v5) VALUES(1,'2026-10-10',3,3)")
    for _ in range(2):
        db = Database(str(path))
        await db.connect()
        assert (await db.get_counter(1, "2026-10-10"))["v5"] == 3
        await db.close()
    db = Database(str(path))
    await db.connect()
    key = await db.create_key(dict(token="test", name="test", daily_images=100,
                                  daily_text_tokens=0, monthly_anlas=10000, rpm=100))
    assert key["id"] == 1
    await db.bump_counters(1, "2026-10-10", v5=.6, images=1)
    await db.reset_daily_image_quota(1, "2026-10-10")
    assert (await db.get_counter(1, "2026-10-10"))["v5"] == 0
    assert await db.day_v5_total("2026-10-10") == 3.6
    await db.bump_counters(1, "2026-10-10", v5=.6, images=1)
    counter = await db.get_counter(1, "2026-10-10")
    assert counter["v5"] == .6 and counter["images"] == 5
    await db.close()


@pytest.mark.asyncio
async def test_failure_does_not_retry_or_count_success(effort_env):
    effort_env.status = 503
    body = {**v5_body(), "model": MEDIUM_MODEL}
    assert (await post("/ai/generate-image", body)).status_code == 503
    assert len(effort_env.calls) == 1
    assert (await effort_env.state.db.get_counter(1, effort_env.state.day()))["v5"] == 0
    assert effort_env.state.nai.pool[0].pending_v5 == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_admin_settings_auth_validation_persistence_and_multiplier(effort_env, streaming):
    st = effort_env.state
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        assert (await client.put("/admin/api/settings", json={"v5_medium_multiplier": .8})).status_code == 401
        assert (await client.post("/admin/api/login", json={"password": "offline-password"})).status_code == 200
        for changes in ({"v5_auto_medium": "true"}, {"v5_medium_multiplier": 0},
                        {"v5_medium_multiplier": .555}, {"v5_medium_threshold": 101}):
            assert (await client.put("/admin/api/settings", json=changes)).status_code == 422
        saved = await client.put("/admin/api/settings", json={"v5_medium_threshold": 100, "v5_medium_multiplier": .8})
        assert saved.status_code == 200
        config = (await client.get("/admin/api/settings")).json()
        assert config["v5_medium_multiplier"] == .8 and config["global_daily_v5"] == 10
        assert (await post("/ai/generate-image" + ("-stream" if streaming else ""), v5_body())).status_code == 200
        assert (await st.db.get_counter(1, st.day()))["v5"] == .8
        assert await st.db.get_upstream_v5_counter(st.nai.pool[0].token_id, st.day()) == .8


@pytest.mark.asyncio
async def test_disabled_auto_mode_preserves_high_settings(effort_env):
    st = effort_env.state
    await st.db.set_setting("v5_auto_medium", False)
    await st.db.bump_counters(2, st.day(), v5=8)
    assert (await post("/ai/generate-image", v5_body(cfg_rescale=.8, negative_prompt="hat"))).status_code == 200
    assert effort_env.calls[0]["model"] == "nai-diffusion-5-full"
    assert effort_env.calls[0]["parameters"]["cfg_rescale"] == .8
