"""Service settings persist, apply to live gates, and never alter admin login."""

import asyncio
import json
import time
import uuid

import pytest
import pytest_asyncio

from app.config import Settings
from app.configuration import CONFIGURATION_FIELDS, initial_sources
from app.state import GateState


@pytest.mark.parametrize("raw", ["", "   ", "invalid", "2.5"])
def test_invalid_integer_environment_is_reported_as_default(monkeypatch, raw):
    monkeypatch.setenv("DEFAULT_RPM", raw)
    assert initial_sources()["default_rpm"] == "default"


@pytest_asyncio.fixture
async def state(tmp_path):
    value = GateState(Settings(
        data_dir=tmp_path,
        admin_password="configuration-test-password",
        secret_key="configuration-test-secret",
        nai_tokens=["configuration-test-upstream"],
        key_inactivity_delete_days=0,
    ))
    await value.db.connect()
    try:
        yield value
    finally:
        await value.db.close()


@pytest.mark.asyncio
async def test_saved_values_survive_restart_and_leave_other_settings_unchanged(state, monkeypatch):
    monkeypatch.setenv("KEY_CONCURRENCY", "2")
    monkeypatch.delenv("DEFAULT_KEY_EXPIRES_DAYS", raising=False)
    restored = GateState(Settings(data_dir=state.settings.data_dir, key_concurrency=2))
    baseline = restored.configuration_snapshot()
    assert baseline["sources"]["key_concurrency"] == "environment"
    assert baseline["sources"]["default_expires_days"] == "default"
    assert baseline["values"]["default_image_model_scope"] == "legacy"
    assert baseline["image_policy"]["free_max_steps_by_tier"] == {"v5": 23, "legacy": 28}
    assert baseline["image_policy"]["paid_max_steps"] is None
    assert set(baseline["values"]) == set(CONFIGURATION_FIELDS)
    assert "password" not in json.dumps(baseline)
    assert "secret" not in json.dumps(baseline)
    assert "configuration-test-upstream" not in json.dumps(baseline)

    original_login = (state.settings.admin_password, state.settings.secret_key,
                      state.settings.login_max_attempts, state.settings.login_window_seconds)
    values = {
        "key_inactivity_delete_days": 14,
        "allow_img2img": True,
        "safe_clamp": False,
        "max_pixels": 512 * 512,
        "max_steps": 20,
        "default_daily_images": 36,
        "default_daily_anlas": 12.5,
        "default_daily_v5": 12,
        "default_monthly_anlas": 250.25,
        "default_daily_text_tokens": 24000,
        "default_rpm": 8,
        "default_expires_days": 0,
        "default_image_model_scope": "all",
        "default_allow_anlas": True,
        "default_allow_img2img": True,
        "default_exclude_global_v5": True,
        "global_concurrency": 3,
        "key_concurrency": 2,
        "max_text_output_tokens": 2048,
        "max_input_chars": 36000,
    }
    updated = await state.update_configuration(values)
    assert updated["values"] == values
    assert set(updated["sources"].values()) == {"panel"}
    assert updated["image_policy"]["free_max_steps_by_tier"] == {"v5": 20, "legacy": 20}
    assert (state.settings.admin_password, state.settings.secret_key,
            state.settings.login_max_attempts, state.settings.login_window_seconds) == original_login
    await restored.db.connect()
    try:
        await restored.load_configuration()
        assert restored.configuration_snapshot() == updated
        assert restored.global_sem.limit == 3
        assert restored.key_sem(12, restored.settings.key_concurrency).limit == 2
    finally:
        await restored.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [
    {}, [], {"unknown": 1}, {"login_max_attempts": 5}, {"login_window_seconds": 300},
    {"admin_password": "replacement"}, {"secret_key": "replacement"},
    {"key_concurrency": True}, {"key_concurrency": 1.5}, {"key_concurrency": "2"},
    {"key_concurrency": 0}, {"max_steps": 29}, {"max_pixels": 4095},
    {"default_daily_anlas": float("nan")}, {"default_daily_anlas": float("inf")},
    {"default_daily_anlas": 10 ** 500}, {"default_daily_images": -1},
    {"allow_img2img": 1}, {"default_image_model_scope": "unknown"},
    {"default_image_model_scope": {}},
    {"allow_img2img": True, "default_rpm": 0},
])
async def test_invalid_values_do_not_partially_apply(state, values):
    before = state.configuration_snapshot()
    with pytest.raises(ValueError):
        await state.update_configuration(values)
    assert state.configuration_snapshot() == before
    assert await state.db.get_setting("configuration_allow_img2img") is None


@pytest.mark.asyncio
async def test_resize_preserves_live_slots_and_wakes_waiters(state):
    global_limit = state.global_sem
    key_limit = state.key_sem(5, 1)
    await global_limit.acquire()
    await key_limit.acquire()
    global_waiter = asyncio.create_task(global_limit.acquire())
    key_waiter = asyncio.create_task(key_limit.acquire())
    await asyncio.sleep(0)
    assert not global_waiter.done() and not key_waiter.done()
    await state.update_configuration({"global_concurrency": 2, "key_concurrency": 2})
    await asyncio.wait_for(asyncio.gather(global_waiter, key_waiter), 1)
    assert state.global_sem is global_limit
    assert state.key_sem(5, 2) is key_limit
    assert global_limit.active == key_limit.active == 2

    await state.update_configuration({"global_concurrency": 1, "key_concurrency": 1})
    next_global = asyncio.create_task(global_limit.acquire())
    next_key = asyncio.create_task(key_limit.acquire())
    await asyncio.sleep(0)
    global_limit.release()
    key_limit.release()
    await asyncio.sleep(0)
    assert not next_global.done() and not next_key.done()
    global_limit.release()
    key_limit.release()
    await asyncio.wait_for(asyncio.gather(next_global, next_key), 1)
    global_limit.release()
    key_limit.release()
    assert global_limit.active == key_limit.active == 0

    await key_limit.acquire()
    cancelled = asyncio.create_task(key_limit.acquire())
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    key_limit.release()
    async with asyncio.timeout(1):
        async with key_limit:
            assert key_limit.active == 1
    assert key_limit.active == 0


async def create_old_key(state, name, *, admin=False):
    row = await state.db.create_key({
        "name": name, "token": "fixture-" + name, "is_admin": admin,
        "daily_images": 100, "monthly_anlas": 2500,
        "daily_text_tokens": 150000, "rpm": 10,
    })
    await state.db._db.execute("UPDATE api_keys SET created_at=? WHERE id=?",
                              (time.time() - 10 * 86400, row["id"]))
    await state.db._db.commit()
    return row["id"]


@pytest.mark.asyncio
async def test_cleanup_preview_is_read_only_and_reuses_inactivity_filter(state):
    old = await create_old_key(state, "unused")
    admin = await create_old_key(state, "administrator", admin=True)
    recent = await create_old_key(state, "recent")
    await state.db.touch_key(recent)
    preview = await state.key_cleanup_preview(3)
    assert preview["days"] == 3 and preview["count"] == 1
    assert preview["keys"][0]["id"] == old
    assert set(preview["keys"][0]) == {"id", "name", "created_at", "last_used_at"}
    assert await state.db.get_key(old) is not None
    assert state.settings.key_inactivity_delete_days == 0
    assert await state.key_cleanup_preview(0) == {"days": 0, "count": 0, "keys": []}
    await state.db.set_setting("key_inactivity_grace_started_at", time.time())
    assert (await state.key_cleanup_preview(3))["count"] == 0
    assert await state.db.get_key(admin) is not None


@pytest.mark.asyncio
async def test_disabling_cleanup_waits_for_inflight_pass_and_blocks_later_deletions(state, monkeypatch):
    first = await create_old_key(state, "first")
    ticket = state.image_queue.create(first, str(uuid.uuid4()), "generate-image")
    await state.update_configuration({"key_inactivity_delete_days": 3})
    assert await state.db.get_key(first) is not None  # Saving never runs a cleanup pass.
    entered, release = asyncio.Event(), asyncio.Event()
    delete = state.db.delete_key

    async def delayed_delete(key_id):
        entered.set()
        await release.wait()
        await delete(key_id)

    monkeypatch.setattr(state.db, "delete_key", delayed_delete)
    cleanup = asyncio.create_task(state.delete_inactive_keys())
    await asyncio.wait_for(entered.wait(), 1)
    disabling = asyncio.create_task(state.update_configuration({"key_inactivity_delete_days": 0}))
    await asyncio.sleep(0)
    assert not disabling.done()
    release.set()
    assert await cleanup == 1
    assert ticket.state == "cancelled"
    await disabling
    later = await create_old_key(state, "later")
    assert await state.delete_inactive_keys() == 0
    assert await state.db.get_key(later) is not None
