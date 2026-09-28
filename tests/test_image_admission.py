"""Concurrent image admission reserves cost without serializing upstream calls."""

import pytest

from app import main
from app.config import Settings
from app.state import GateState
from test_generation_integration import FakeState


@pytest.fixture
def state(monkeypatch):
    value = FakeState()
    value.settings.queue_timeout = .04
    monkeypatch.setattr(main, "STATE", value)
    return value


@pytest.mark.asyncio
async def test_image_admission_uses_key_limit_but_not_legacy_global_slot(state):
    key = state.db.keys["fixture-1"]
    state.settings.key_concurrency = 1
    for _ in range(3):
        await state.global_sem.acquire()
    async with main.acquire_concurrency(key, image=True):
        assert state.global_active == 1
        with pytest.raises(main.GateError) as caught:
            async with main.acquire_concurrency(key, image=True):
                pass
        assert caught.value.status == 429
    assert state.global_active == state.global_waiting == 0
    assert state.global_sem._value == 0


@pytest.mark.asyncio
async def test_concurrent_requests_cannot_overspend_one_key(state):
    key = state.db.keys["fixture-1"]
    key["daily_anlas"] = 2
    async with main.reserve_image_budget(key, {"anlas": 2, "v5": 0}):
        assert len(state.image_reservations) == 1
        with pytest.raises(main.GateError) as caught:
            async with main.reserve_image_budget(key, {"anlas": 2, "v5": 0}):
                pass
        assert caught.value.status == 402
    assert not state.image_reservations


@pytest.mark.asyncio
async def test_concurrent_keys_share_global_anlas_budget(state):
    state.settings.global_monthly_anlas = 2
    first, second = state.db.keys.values()
    async with main.reserve_image_budget(first, {"anlas": 2, "v5": 0}):
        with pytest.raises(main.GateError) as caught:
            async with main.reserve_image_budget(second, {"anlas": 2, "v5": 0}):
                pass
        assert caught.value.status == 402


@pytest.mark.asyncio
async def test_v5_cost_change_is_checked_against_pending_paid_budget(state):
    state.settings.global_monthly_anlas = 2
    first, second = state.db.keys.values()
    async with main.reserve_image_budget(first, {"anlas": 2, "v5": 0}):
        async with main.reserve_image_budget(second, {"anlas": 0, "v5": 1}) as reservation:
            with pytest.raises(main.GateError) as caught:
                await reservation.update(second, {"anlas": 2, "v5": 0})
            assert caught.value.status == 402
            assert reservation.v5 == 1 and reservation.anlas == 0


@pytest.mark.asyncio
async def test_user_key_pacing_timeout_does_not_reserve_future_slots(tmp_path):
    gate = GateState(Settings(data_dir=tmp_path, nai_tokens=[]))
    gate.settings.key_image_min_interval = 1
    gate.settings.queue_timeout = .02
    await gate.wait_for_key_image_slot(7)
    reserved = gate._key_image_next_at[7]
    with pytest.raises(TimeoutError):
        await gate.wait_for_key_image_slot(7)
    assert gate._key_image_next_at[7] == reserved
    assert gate._image_pacing_waiting == 0
