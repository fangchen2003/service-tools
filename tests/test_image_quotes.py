"""Quotation and execution share policy; all upstream requests are local fakes."""
import base64
import copy
import io

import httpx
import pytest
from PIL import Image

from app import main
from app.v5_effort import MEDIUM_MODEL
from test_generation_integration import image_body, post
from test_v5_effort import effort_env


def picture(width=64, height=64):
    data = io.BytesIO()
    Image.new('RGB', (width, height), 'navy').save(data, format='PNG')
    return base64.b64encode(data.getvalue()).decode()


async def quote(body, operation='generate-image', pending=None):
    return await post('/v1/image/quote', {'operation': operation, 'payload': body,
                                        'pending_vibe_indices': pending or []})


@pytest.mark.asyncio
async def test_quote_does_not_touch_database_admission_or_upstream(effort_env):
    st = effort_env.state
    async def forbidden(_):
        raise AssertionError('A quotation contacted the upstream')
    await st.nai._client.aclose()
    st.nai._client = httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
    before = st.db._db.total_changes
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
        assert (await client.get('/v1/image/quote')).status_code == 401
        identity = await client.get('/v1/image/quote', headers={'Authorization': 'Bearer fixture-1'})
        assert identity.status_code == 200
    body = {**image_body(steps=50), 'model': 'nai-diffusion-5-full'}
    response = await quote(body)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value['cost_limit'] == {'anlas': 0, 'v5': 1}
    assert value['effective_parameters']['steps'] == 23
    assert body['parameters']['steps'] == 50
    assert st.db._db.total_changes == before
    assert not st.image_reservations and not st._rpm
    assert st.global_active == st.global_waiting == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_medium_quote_matches_the_real_route_ledger(effort_env, streaming):
    st = effort_env.state
    await st.db.bump_counters(2, st.day(), v5=8, images=8)
    body = {**image_body(steps=50, sampler='k_dpmpp_2m', cfg_rescale=.8), 'model': 'nai-diffusion-5-full'}
    value = (await quote(body)).json()
    assert value['effective_model'] == MEDIUM_MODEL
    assert value['effective_parameters']['steps'] == 14
    assert value['cost_limit'] == {'anlas': 0, 'v5': .6}
    response = await post('/ai/generate-image'+('-stream' if streaming else ''), {**body, 'cost_limit': value['cost_limit']})
    assert response.status_code == 200, response.text
    assert effort_env.calls[0]['model'] == value['effective_model']
    assert 'cost_limit' not in effort_env.calls[0]
    counter = await st.db.get_counter(1, st.day())
    assert counter['v5'] == .6 and counter['anlas'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('size,billing,output,anlas,v5', [
    ((1024, 1024), [1760, 1760], [1773, 1773], 45, 0),
    ((832, 1216), [1440, 2144], [1467, 2144], 45, 0),
    ((256, 512), [512, 1024], [512, 1024], 0, 1),
])
async def test_max_quotes_target_size_and_forwards_original_pixels(effort_env, size, billing, output, anlas, v5):
    st = effort_env.state
    await st.db.update_key(1, {'allow_anlas': True})
    body = {**image_body(width=size[0], height=size[1], steps=28, image=picture(*size),
                        strength=.5, upscaled_enhance=True), 'model': 'nai-diffusion-5-full', 'action': 'img2img'}
    original = copy.deepcopy(body)
    response = await quote(body)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value['billing_size'] == billing and value['output_size'] == output
    assert value['cost_limit'] == {'anlas': anlas, 'v5': v5}
    response = await post('/ai/generate-image', {**body, 'cost_limit': value['cost_limit']})
    assert response.status_code == 200, response.text
    sent = effort_env.calls[0]
    assert sent['parameters'] == original['parameters']
    counter = await st.db.get_counter(1, st.day())
    assert (counter['anlas'], counter['v5']) == (anlas, v5)
    assert body == original


@pytest.mark.asyncio
@pytest.mark.parametrize('patch', [
    {'upscaled_enhance': 'true'}, {'width': 512}, {'n_samples': 2},
    {'mask': 'mask'}, {'upscale': True}, {'img2img': {'strength': .5}},
])
async def test_invalid_max_is_rejected_by_quote_and_execution(effort_env, patch):
    await effort_env.state.db.update_key(1, {'allow_anlas': True})
    params = dict(width=1024, height=1024, image=picture(1024, 1024), upscaled_enhance=True)
    params.update(patch)
    body = {**image_body(**params), 'model': 'nai-diffusion-5-full', 'action': 'img2img'}
    assert (await quote(body)).status_code == 400
    assert (await post('/ai/generate-image', body)).status_code == 400
    assert effort_env.calls == []


@pytest.mark.asyncio
async def test_max_does_not_open_legacy_models_or_text_generation(effort_env):
    await effort_env.state.db.update_key(1, {'allow_anlas': True})
    body = image_body(width=256, height=256, image=picture(256, 256), upscaled_enhance=True)
    body['action'] = 'img2img'
    assert (await quote(body)).status_code == 400
    body['model'] = 'nai-diffusion-5-full'
    body['action'] = 'generate'
    assert (await quote(body)).status_code == 400


@pytest.mark.asyncio
async def test_price_increase_stops_before_dispatch(effort_env):
    st = effort_env.state
    await st.db.bump_counters(2, st.day(), v5=8)
    body = {**image_body(), 'model': 'nai-diffusion-5-full'}
    value = (await quote(body)).json()
    await st.db.set_setting('v5_auto_medium', False)
    response = await post('/ai/generate-image', {**body, 'cost_limit': value['cost_limit']})
    assert response.status_code == 409
    assert effort_env.calls == [] and not st.image_reservations
    assert (await st.db.get_counter(1, st.day()))['v5'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_exhausted_upstream_cannot_turn_confirmed_v5_into_anlas(effort_env, streaming):
    st = effort_env.state
    await st.db.update_key(1, {'allow_anlas': True})
    body = {**image_body(), 'model': 'nai-diffusion-5-full'}
    value = (await quote(body)).json()
    async def exhausted(*args):
        return True
    st.nai.allowance.resolve = exhausted
    response = await post('/ai/generate-image'+('-stream' if streaming else ''), {**body, 'cost_limit': value['cost_limit']})
    assert response.status_code == 409
    assert effort_env.calls == [] and not st.image_reservations
    counter = await st.db.get_counter(1, st.day())
    assert counter['v5'] == counter['anlas'] == counter['images'] == 0


@pytest.mark.asyncio
async def test_pending_vibes_are_deduplicated_without_encoding(effort_env):
    await effort_env.state.db.update_key(1, {'allow_anlas': True})
    raw = picture()
    body = image_body(reference_image_multiple=[raw]*5, reference_strength_multiple=[.2]*5,
                      reference_information_extracted_multiple=[1]*5)
    response = await quote(body, pending=list(range(5)))
    assert response.status_code == 200, response.text
    value = response.json()
    assert (value['generation_anlas'], value['encoding_count'], value['encoding_anlas'], value['anlas']) == (2, 1, 2, 4)
    assert value['cost_limit'] == {'anlas': 2, 'v5': 0}
    assert value['encoding_cost_limit'] == {'anlas': 2, 'v5': 0}
    assert body['parameters']['reference_image_multiple'] == [raw]*5
    assert effort_env.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,expected', [('upscale', 1), ('augment-image', 65), ('encode-vibe', 2)])
async def test_tool_and_encoding_quotes_use_gate_prices(effort_env, operation, expected):
    await effort_env.state.db.update_key(1, {'allow_anlas': True})
    body = {'image': picture(), 'model': 'nai-diffusion-5-curated'}
    if operation == 'augment-image':
        body['req_type'] = 'bg-removal'
    elif operation == 'encode-vibe':
        body.update(model='nai-diffusion-4-5-full', informationExtracted=1)
    response = await quote(body, operation)
    assert response.status_code == 200, response.text
    assert response.json()['cost_limit'] == {'anlas': expected, 'v5': 0}
    assert effort_env.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('model,strength,expected', [
    ('nai-diffusion-5-full', .5, 26),
    ('nai-diffusion-5-full-medium', .5, 25),
    ('nai-diffusion-5-full-medium', .99, 50),
])
async def test_medium_anlas_step_factor_matches_official_client(effort_env, model, strength, expected):
    st = effort_env.state
    await st.db.update_key(1, {'allow_anlas': True})
    body = {**image_body(width=1408, height=2176, steps=14, image=picture(), strength=strength),
            'model': model, 'action': 'img2img'}
    value = (await quote(body)).json()
    assert value['cost_limit'] == {'anlas': expected, 'v5': 0}
    response = await post('/ai/generate-image', {**body, 'cost_limit': value['cost_limit']})
    assert response.status_code == 200
    assert (await st.db.get_counter(1, st.day()))['anlas'] == expected


@pytest.mark.asyncio
async def test_max_medium_uses_its_own_step_price_factor(effort_env):
    await effort_env.state.db.update_key(1, {'allow_anlas': True})
    body = {**image_body(width=832, height=1216, image=picture(832, 1216),
                        upscaled_enhance=True, strength=.5, steps=28),
            'model': MEDIUM_MODEL, 'action': 'img2img'}
    value = (await quote(body)).json()
    assert value['effective_parameters']['steps'] == 14
    assert value['billing_size'] == [1440, 2144]
    assert value['cost_limit'] == {'anlas': 25, 'v5': 0}
