"""Regression tests: Discord re-registration after cleanup, pacing refund, strength validation."""
import time

import pytest
import pytest_asyncio

from app.config import Settings
from app.database import Database
from app.policy import estimate_image_cost
from app.state import GateState


@pytest_asyncio.fixture
async def db():
    value = Database(":memory:")
    await value.connect()
    try:
        yield value
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_inactivity_cleanup_releases_discord_registration(db):
    row = await db.create_key(dict(name="d", token="t", daily_images=1, monthly_anlas=0,
                                   daily_text_tokens=0, rpm=1))
    await db._db.execute("UPDATE api_keys SET created_at=? WHERE id=?", (time.time() - 10 * 86400, row["id"]))
    await db._db.execute("INSERT INTO discord_registrations(discord_id,key_id,created_at) VALUES ('42',?,?)",
                         (row["id"], time.time()))
    await db._db.commit()
    state = GateState(Settings(key_inactivity_delete_days=3))
    state.db = db
    assert await state.delete_inactive_keys() == 1
    left = await db._db.execute_fetchall("SELECT 1 FROM discord_registrations WHERE discord_id='42'")
    assert left == []


@pytest.mark.asyncio
async def test_refund_restores_pacing_only_if_not_overwritten():
    state = GateState(Settings(key_image_min_interval=15, queue_timeout=1))
    await state.wait_for_key_image_slot(1)
    assert state._key_image_next_at[1] > time.monotonic() + 10
    await state.refund_key_image_slot(1)
    assert state._key_image_next_at[1] == 0.0
    await state.wait_for_key_image_slot(1)          # slot free again straight away
    state._key_image_next_at[1] += 100               # a later request moved it
    await state.refund_key_image_slot(1)
    assert state._key_image_next_at[1] > time.monotonic() + 100


@pytest.mark.parametrize("strength", [-1, 2, float("inf"), float("nan"), "0.5", True])
def test_img2img_strength_must_be_unit_interval(strength):
    body = {"model": "nai-diffusion-4-5-full",
            "parameters": {"width": 512, "height": 512, "image": "x", "strength": strength}}
    with pytest.raises(ValueError):
        estimate_image_cost(body)
