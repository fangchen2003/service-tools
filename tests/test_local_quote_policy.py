"""Merged quote/receipt paths honor this deployment's free-only and claim policy."""
import pytest
from app import main
from test_image_quotes import quote, picture
from test_generation_integration import image_body, post
from test_v5_effort import effort_env


async def claim(st):
    await st.db._db.execute("INSERT INTO discord_registrations(discord_id,key_id,created_at) VALUES('777',1,0)")
    await st.db._db.commit()
    await st.db.claim_registered_vibe('777')


@pytest.mark.asyncio
@pytest.mark.parametrize('paid', [False, True])
@pytest.mark.parametrize('safe', [False, True])
async def test_quote_and_execution_clamp_all_ordinary_keys(effort_env, paid, safe):
    st = effort_env.state
    st.settings.safe_clamp = safe
    await st.db.update_key(1, {'allow_anlas': paid})
    body = {**image_body(steps=50, width=2048, height=2048, n_samples=4, sm=True), 'model': 'nai-diffusion-5-full'}
    before = st.db._db.total_changes
    response = await quote(body)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value['effective_parameters']['steps'] == 28
    assert value['cost_limit'] == {'anlas': 0, 'v5': 1}
    assert st.db._db.total_changes == before
    result = await post('/ai/generate-image', {**body, 'cost_limit': value['cost_limit']})
    assert result.status_code == 200
    assert effort_env.calls[0]['parameters']['steps'] == 28
    assert effort_env.calls[0]['parameters']['n_samples'] == 1


@pytest.mark.asyncio
async def test_encoding_quote_checks_claim_read_only_then_execution_consumes_once(effort_env):
    st = effort_env.state
    body = {'model': 'nai-diffusion-4-5-full', 'image': picture(), 'informationExtracted': .7}
    assert (await quote(body, 'encode-vibe')).status_code == 429
    await claim(st)
    before = st.db._db.total_changes
    value = (await quote(body, 'encode-vibe')).json()
    assert value['cost_limit'] == {'anlas': 2, 'v5': 0}
    assert value['vibe_remaining'] == 3 and value['vibe_required'] == 1
    assert st.db._db.total_changes == before
    assert await st.db.get_vibe_remaining(1) == 3
    result = await post('/ai/encode-vibe', {**body, 'cost_limit': value['cost_limit']})
    assert result.status_code == 200
    assert await st.db.get_vibe_remaining(1) == 2
    assert len(effort_env.calls) == 1


@pytest.mark.asyncio
async def test_pending_vibes_use_claim_not_numeric_anlas_permission(effort_env):
    st = effort_env.state
    raw = picture()
    body = image_body(reference_image_multiple=[raw]*2, reference_strength_multiple=[.2]*2,
                      reference_information_extracted_multiple=[1]*2)
    assert (await quote(body, pending=[0, 1])).status_code == 429
    await claim(st)
    before = st.db._db.total_changes
    value = (await quote(body, pending=[0, 1])).json()
    assert value['generation_anlas'] == 0 and value['encoding_count'] == 1
    assert value['vibe_required'] == 1 and value['vibe_remaining'] == 3
    assert st.db._db.total_changes == before and effort_env.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['upscale', 'augment-image'])
async def test_ordinary_paid_tool_quote_matches_runtime_denial(effort_env, operation):
    st = effort_env.state
    await st.db.update_key(1, {'allow_anlas': True})
    body = {'image': picture(), 'model': 'nai-diffusion-5-curated', 'req_type': 'bg-removal'}
    assert (await quote(body, operation)).status_code == 402
    assert (await post('/ai/'+operation, body)).status_code == 402
    assert effort_env.calls == []


@pytest.mark.asyncio
async def test_claimed_encoding_stays_visible_to_reconciliation_until_settled(effort_env, monkeypatch):
    import asyncio
    st = effort_env.state
    await claim(st)
    effort_env.wait = asyncio.Event()
    body = {'model': 'nai-diffusion-4-5-full', 'image': picture(), 'informationExtracted': .7}
    pending = asyncio.create_task(post('/ai/encode-vibe', body))
    try:
        for _ in range(100):
            if effort_env.calls:
                break
            await asyncio.sleep(.001)
        assert len(effort_env.calls) == 1
        assert len(st.image_reservations) == 1
        assert not st.image_budget_idle.is_set()
    finally:
        effort_env.wait.set()
        await pending
    assert not st.image_reservations and st.image_budget_idle.is_set()


@pytest.mark.asyncio
async def test_encoding_cost_limit_rejection_preserves_claim(effort_env):
    st = effort_env.state
    await claim(st)
    body = {'model': 'nai-diffusion-4-5-full', 'image': picture(), 'informationExtracted': .7,
            'cost_limit': {'anlas': 0, 'v5': 0}}
    result = await post('/ai/encode-vibe', body)
    assert result.status_code == 409
    assert await st.db.get_vibe_remaining(1) == 3
    assert effort_env.calls == []
