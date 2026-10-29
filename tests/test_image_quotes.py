"""Quotation and execution share policy; all upstream requests are local fakes."""
import base64
import copy
import io

import httpx
import pytest
from PIL import Image

from app import main, policy
from app.v5_effort import MEDIUM_MODEL
from test_generation_integration import image_body, post
from test_v5_effort import effort_env


def picture(width=64, height=64):
    data = io.BytesIO()
    Image.new('RGB', (width, height), 'navy').save(data, format='PNG')
    return base64.b64encode(data.getvalue()).decode()


def metadata(body, operation='generate-image', pending=None):
    p = body.get('parameters', body)
    if operation == 'generate-image':
        values = {name: p[name] for name in ('width', 'height', 'steps', 'n_samples', 'sm', 'sm_dyn',
                                            'strength', 'upscaled_enhance') if name in p}
        values.update(mode='inpaint' if p.get('mask') else 'img2img' if p.get('image') else 'generate',
                      inpaint_strength=(p.get('img2img') or {}).get('strength', 1),
                      has_controlnet=bool(p.get('controlnet_model') or p.get('controlnet_condition')),
                      vibe_count=len(p.get('reference_image_multiple') or p.get('reference_image_multiple_cached') or []),
                      precise_count=len(p.get('director_reference_images_cached') or []))
        refs = p.get('reference_image_multiple', [])
        extracted = p.get('reference_information_extracted_multiple', [])
        values['pending_vibe_encoding_count'] = len({(refs[i], extracted[i]) for i in pending or []})
    elif operation == 'encode-vibe':
        values = {}
    else:
        values = dict(width=p.get('width', 64), height=p.get('height', 64))
        if operation == 'augment-image':
            values['req_type'] = p['req_type']
    return dict(operation=operation, model=body['model'], parameters=values)


async def quote(body, operation='generate-image', pending=None):
    return await post('/v1/image/quote', metadata(body, operation, pending))


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
    assert value['effective_parameters']['steps'] == 28
    assert body['parameters']['steps'] == 50
    assert st.db._db.total_changes == before
    assert not st.image_reservations and not st._rpm
    assert st.global_active == st.global_waiting == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('paid', [False, True])
async def test_medium_quote_matches_the_real_route_ledger(effort_env, streaming, paid):
    st = effort_env.state
    await st.db.update_key(1, {'allow_anlas': paid})
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
    await effort_env.state.db._db.execute("UPDATE api_keys SET is_admin=1 WHERE id=1")
    await effort_env.state.db._db.commit()
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
    {'upscaled_enhance': 'true'}, {'n_samples': 2}, {'mask': 'mask'},
])
async def test_invalid_max_is_rejected_by_quote_and_execution(effort_env, patch):
    await effort_env.state.db._db.execute("UPDATE api_keys SET is_admin=1 WHERE id=1")
    await effort_env.state.db._db.commit()
    await effort_env.state.db.update_key(1, {'allow_anlas': True})
    params = dict(width=1024, height=1024, image=picture(1024, 1024), upscaled_enhance=True)
    params.update(patch)
    body = {**image_body(**params), 'model': 'nai-diffusion-5-full', 'action': 'img2img'}
    assert (await quote(body)).status_code == 400
    assert (await post('/ai/generate-image', body)).status_code == 400
    assert effort_env.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('patch', [{'width': 512}, {'upscale': True}, {'img2img': {'strength': .5}}, {'image': 'invalid'}])
async def test_max_execution_still_validates_actual_source_and_protocol(effort_env, patch):
    await effort_env.state.db._db.execute("UPDATE api_keys SET is_admin=1 WHERE id=1")
    await effort_env.state.db._db.commit()
    await effort_env.state.db.update_key(1, {'allow_anlas': True})
    params = dict(width=1024, height=1024, image=picture(1024, 1024), upscaled_enhance=True)
    params.update(patch)
    body = {**image_body(**params), 'model': 'nai-diffusion-5-full', 'action': 'img2img'}
    assert (await quote(body)).status_code == 200
    assert (await post('/ai/generate-image', body)).status_code == 400
    assert effort_env.calls == []


@pytest.mark.asyncio
async def test_max_does_not_open_legacy_models_or_text_generation(effort_env):
    await effort_env.state.db._db.execute("UPDATE api_keys SET is_admin=1 WHERE id=1")
    await effort_env.state.db._db.commit()
    await effort_env.state.db.update_key(1, {'allow_anlas': True})
    body = image_body(width=256, height=256, image=picture(256, 256), upscaled_enhance=True)
    body['action'] = 'img2img'
    assert (await quote(body)).status_code == 400
    body['model'] = 'nai-diffusion-5-full'
    body['action'] = 'generate'
    query = metadata(body)
    query['parameters']['mode'] = 'generate'
    assert (await post('/v1/image/quote', query)).status_code == 400
    assert (await post('/ai/generate-image', body)).status_code == 400


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
    await effort_env.state.db._db.execute("UPDATE api_keys SET is_admin=1 WHERE id=1")
    await effort_env.state.db._db.commit()
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
    await effort_env.state.db._db.execute("UPDATE api_keys SET is_admin=1 WHERE id=1")
    await effort_env.state.db._db.commit()
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
    await effort_env.state.db._db.execute("UPDATE api_keys SET is_admin=1 WHERE id=1")
    await effort_env.state.db._db.commit()
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
    await effort_env.state.db._db.execute("UPDATE api_keys SET is_admin=1 WHERE id=1")
    await effort_env.state.db._db.commit()
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
    await effort_env.state.db._db.execute("UPDATE api_keys SET is_admin=1 WHERE id=1")
    await effort_env.state.db._db.commit()
    await effort_env.state.db.update_key(1, {'allow_anlas': True})
    body = {**image_body(width=832, height=1216, image=picture(832, 1216),
                        upscaled_enhance=True, strength=.5, steps=28),
            'model': MEDIUM_MODEL, 'action': 'img2img'}
    value = (await quote(body)).json()
    assert value['effective_parameters']['steps'] == 14
    assert value['billing_size'] == [1440, 2144]
    assert value['cost_limit'] == {'anlas': 25, 'v5': 0}


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('image', 'base64'), ('mask', 'base64'), ('prompt', 'text'), ('sampler', 'k_euler'),
    ('width', True), ('height', 63), ('steps', '28'), ('n_samples', 0),
    ('strength', 10 ** 500), ('inpaint_strength', -1), ('sm', 1),
    ('vibe_count', -1), ('precise_count', 17), ('pending_vibe_encoding_count', 1),
])
async def test_quote_rejects_content_and_invalid_metadata(effort_env, field, value):
    query = metadata(image_body())
    query['parameters'][field] = value
    assert (await post('/v1/image/quote', query)).status_code == 400
    assert effort_env.calls == []


@pytest.mark.asyncio
async def test_quote_rejects_old_payload_and_limits_body_to_four_kib(effort_env):
    assert (await post('/v1/image/quote', {'operation': 'generate-image', 'payload': image_body()})).status_code == 400
    query = metadata(image_body())
    query['parameters']['image'] = 'x' * 4096
    assert (await post('/v1/image/quote', query)).status_code == 413
    assert effort_env.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,model,parameters', [
    ('generate-image', 'nai-diffusion-4-5-full', dict(width=1024, height=1024, steps=28, precise_count=1)),
    ('generate-image', 'nai-diffusion-4-5-full', dict(width=1024, height=1024, steps=28, vibe_count=5,
                                                  pending_vibe_encoding_count=1)),
    ('generate-image', 'nai-diffusion-5-full', dict(mode='img2img', width=832, height=1216, steps=28,
                                                strength=.5, upscaled_enhance=True)),
    ('upscale', 'nai-diffusion-5-curated', dict(width=1024, height=1024)),
    ('augment-image', 'nai-diffusion-5-curated', dict(width=64, height=64, req_type='bg-removal')),
    ('encode-vibe', 'nai-diffusion-4-5-full', {}),
])
async def test_all_quote_operations_skip_image_processing_and_writes(effort_env, monkeypatch, operation, model, parameters):
    await effort_env.state.db._db.execute("UPDATE api_keys SET is_admin=1 WHERE id=1")
    await effort_env.state.db._db.commit()
    st = effort_env.state
    await st.db.update_key(1, {'allow_anlas': True})
    def forbidden(*args, **kwargs):
        raise AssertionError('Metadata quotation attempted image processing or an upstream call')
    for name in ('prepare_image_generation', 'prepare_tool', 'dimensions'):
        monkeypatch.setattr(main, name, forbidden)
    monkeypatch.setattr(policy, '_base64_data', forbidden)
    monkeypatch.setattr(Image, 'open', forbidden)
    await st.nai._client.aclose()
    st.nai._client = httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
    before = st.db._db.total_changes
    response = await post('/v1/image/quote', dict(operation=operation, model=model, parameters=parameters))
    assert response.status_code == 200, response.text
    assert st.db._db.total_changes == before
    assert not st.image_reservations and not st._rpm
    assert st.global_active == st.global_waiting == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['size', 'reference', 'mode', 'max', 'upscale'])
async def test_underreported_metadata_cannot_lower_execution_cost(effort_env, kind):
    await effort_env.state.db._db.execute("UPDATE api_keys SET is_admin=1 WHERE id=1")
    await effort_env.state.db._db.commit()
    st = effort_env.state
    await st.db.update_key(1, {'allow_anlas': True})
    body = image_body(width=1408, height=2176, steps=28)
    operation = 'generate-image'
    if kind == 'reference':
        body = image_body(precise=1, precise_count=0, vibe_count=0, has_controlnet=False)
    elif kind == 'mode':
        body = image_body(width=1408, height=2176, image=picture(), strength=1, mode='generate')
        body['action'] = 'img2img'
    elif kind == 'max':
        body = {**image_body(width=832, height=1216, steps=28, image=picture(832, 1216),
                            strength=.5, upscaled_enhance=True), 'model': 'nai-diffusion-5-full', 'action': 'img2img'}
    elif kind == 'upscale':
        operation = 'upscale'
        body = dict(model='nai-diffusion-5-curated', image=picture(1536, 1024), width=1536, height=1024)
    query = metadata(body, operation)
    if kind == 'reference':
        query['parameters']['precise_count'] = 0
    elif kind == 'mode':
        query['parameters']['strength'] = .1
    else:
        query['parameters'].update(width=512, height=512)
    response = await post('/v1/image/quote', query)
    assert response.status_code == 200, response.text
    path = '/ai/upscale' if kind == 'upscale' else '/ai/generate-image'
    response = await post(path, {**body, 'cost_limit': response.json()['cost_limit']})
    assert response.status_code == 409, response.text
    assert effort_env.calls == [] and not st.image_reservations
    counter = await st.db.get_counter(1, st.day())
    assert counter['anlas'] == counter['v5'] == counter['images'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('model,parameters,expected', [
    ('nai-diffusion-3', dict(width=512, height=512, steps=29, sm=True), {'anlas': 8, 'v5': 0}),
    ('nai-diffusion-3', dict(width=512, height=512, steps=29, sm=True, sm_dyn=True), {'anlas': 9, 'v5': 0}),
    ('nai-diffusion-4-5-full', dict(precise=1), {'anlas': 5, 'v5': 0}),
    ('nai-diffusion-4-5-full', dict(reference_image_multiple=['ZW5jb2RlZA=='] * 5,
                                  reference_strength_multiple=[.6] * 5,
                                  reference_information_extracted_multiple=[1] * 5), {'anlas': 2, 'v5': 0}),
    ('nai-diffusion-4-5-full-inpainting', dict(steps=29, image='source', mask='mask', img2img={'strength': .5}),
     {'anlas': 11, 'v5': 0}),
    ('nai-diffusion-5-full-inpainting', dict(steps=29, image='source', mask='mask', img2img={'strength': .5}),
     {'anlas': 32, 'v5': 0}),
])
async def test_metadata_quotes_match_execution_prices(effort_env, model, parameters, expected):
    await effort_env.state.db._db.execute("UPDATE api_keys SET is_admin=1 WHERE id=1")
    await effort_env.state.db._db.commit()
    st = effort_env.state
    await st.db.update_key(1, {'allow_anlas': True})
    body = {**image_body(**parameters), 'model': model}
    value = (await quote(body)).json()
    assert value['cost_limit'] == expected
    assert policy.estimate_image_cost(body) == expected
    response = await post('/ai/generate-image', {**body, 'cost_limit': value['cost_limit']})
    assert response.status_code == 200, response.text
    counter = await st.db.get_counter(1, st.day())
    assert (counter['anlas'], counter['v5']) == (expected['anlas'], expected['v5'])
