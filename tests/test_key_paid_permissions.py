"""Explicit per-key paid access; all upstreams are offline fixtures."""
import pytest

from app import main
from test_generation_integration import image_body, post
from test_image_quotes import quote
from test_v5_effort import effort_env


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('model', ['nai-diffusion-4-5-full', 'nai-diffusion-5-full'])
async def test_enabled_key_preserves_paid_spec_quote_and_dispatch(effort_env, streaming, model):
    st = effort_env.state
    await st.db.update_key(1, {'allow_anlas': True})
    # Return two actual fixture outputs for the requested two-image batch.
    import httpx
    from test_nai_integration import image_zip
    from test_image_stream_routes import event
    original_transport = st.nai._client._transport
    async def two_outputs(request):
        response = await original_transport.handle_async_request(request)
        if request.method == 'POST':
            return httpx.Response(200, content=event(index=0) + event(index=1) if streaming else image_zip(2),
                                  headers={'content-type': 'text/event-stream' if streaming else 'application/zip'})
        return response
    await st.nai._client.aclose()
    st.nai._client = httpx.AsyncClient(transport=httpx.MockTransport(two_outputs))
    body = {**image_body(width=1536, height=1024, steps=40, n_samples=2, sm=True), 'model': model}
    quoted = await quote(body)
    assert quoted.status_code == 200, quoted.text
    q = quoted.json()
    assert q['effective_parameters']['steps'] == 40
    assert q['effective_parameters']['width'] == 1536
    assert q['effective_parameters']['height'] == 1024
    assert q['cost_limit']['anlas'] > 0
    response = await post('/ai/generate-image' + ('-stream' if streaming else ''),
                          {**body, 'cost_limit': q['cost_limit']})
    assert response.status_code == 200, response.text
    sent, = effort_env.calls
    for field in ('width', 'height', 'steps', 'n_samples', 'sm'):
        assert sent['parameters'][field] == body['parameters'][field]
    counter = await st.db.get_counter(1, st.day())
    assert counter['anlas'] == q['cost_limit']['anlas']
    assert counter['images'] == 2


@pytest.mark.asyncio
async def test_enabled_key_metadata_is_not_free_only(effort_env):
    import httpx
    from app.client_views import subscription_payload
    st = effort_env.state
    for paid in (False, True):
        await st.db.update_key(1, {'allow_anlas': paid})
        key = await st.db.get_key(1)
        data = await subscription_payload(st, key)
        assert data['naiGate']['anlasEnabled'] is paid
        assert data['naiGate']['freeOnly'] is (not paid)
        assert data['naiGate']['anlasQuotaEnabled'] is False
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            response = await client.get('/v1/me', headers={'Authorization': 'Bearer fixture-1'})
        policy = response.json()['image_policy']
        assert policy['free_only'] is (not paid)
        assert policy['max_steps'] == (None if paid else 28)


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_enabled_img2img_requires_separate_permission(effort_env, streaming):
    from test_generation_integration import PNG
    st = effort_env.state
    await st.db.update_key(1, {'allow_anlas': True, 'allow_img2img': True})
    body = image_body(image=PNG, steps=40)
    body['action'] = 'img2img'
    response = await post('/ai/generate-image' + ('-stream' if streaming else ''), body)
    assert response.status_code == 200, response.text
    assert effort_env.calls[0]['action'] == 'img2img'
    await st.db.update_key(1, {'allow_img2img': False})
    response = await post('/ai/generate-image' + ('-stream' if streaming else ''), body)
    assert response.status_code == 400
    assert len(effort_env.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['width', 'height', 'n_samples'])
@pytest.mark.parametrize('value', [0, -64, True, 1.5, None])
async def test_paid_permission_does_not_skip_numeric_validation(effort_env, field, value):
    await effort_env.state.db.update_key(1, {'allow_anlas': True})
    response = await post('/ai/generate-image', image_body(**{field: value}))
    assert response.status_code == 400
    assert effort_env.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('paid', [False, True])
@pytest.mark.parametrize('streaming', [False, True])
async def test_v5_exhaustion_follows_paid_key_permission(effort_env, paid, streaming):
    st = effort_env.state
    await st.db.update_key(1, {'allow_anlas': paid})
    async def exhausted(*args):
        return True
    st.nai.allowance.resolve = exhausted
    response = await post('/ai/generate-image' + ('-stream' if streaming else ''),
                          {**image_body(), 'model': 'nai-diffusion-5-full'})
    assert response.status_code == (200 if paid else 402), response.text
    assert len(effort_env.calls) == int(paid)
    counter = await st.db.get_counter(1, st.day())
    assert counter['v5'] == 0
    assert (counter['anlas'] > 0) is paid


@pytest.mark.asyncio
async def test_paid_v5_never_automatically_switches_medium(effort_env):
    st = effort_env.state
    await st.db.update_key(1, {'allow_anlas': True})
    await st.db.bump_counters(2, st.day(), v5=8, images=8)
    body = {**image_body(steps=40), 'model': 'nai-diffusion-5-full'}
    response = await post('/ai/generate-image', body)
    assert response.status_code == 200, response.text
    assert effort_env.calls[0]['model'] == 'nai-diffusion-5-full'
    assert effort_env.calls[0]['parameters']['steps'] == 40


def test_admin_ui_exposes_only_paid_permission_not_numeric_budgets():
    from pathlib import Path
    static = Path(__file__).resolve().parents[1] / 'app/static'
    html = (static / 'index.html').read_text()
    script = (static / 'assets/gate.js').read_text()
    assert 'id="f_allow_anlas"' in html
    assert "$('#f_allow_anlas').checked = k.allow_anlas" in script
    assert "allow_anlas: $('#f_allow_anlas').checked" in script
    assert "default_allow_anlas" in script
    for field in ('f_daily_anlas', 'f_monthly_anlas', 'default_daily_anlas', 'default_monthly_anlas'):
        assert field not in html + script


@pytest.mark.asyncio
@pytest.mark.parametrize('paid', [False, True])
@pytest.mark.parametrize('reference', ['precise', 'five-vibes'])
async def test_paid_references_require_explicit_key_permission(effort_env, paid, reference):
    from test_policy_integration import payload
    st = effort_env.state
    await st.db.update_key(1, {'allow_anlas': paid})
    body = image_body(precise=1) if reference == 'precise' else payload(vibes=5)
    response = await post('/ai/generate-image', body)
    assert response.status_code == (200 if paid else 400), response.text
    assert len(effort_env.calls) == int(paid)
    counter = await st.db.get_counter(1, st.day())
    assert counter['anlas'] == ((5 if reference == 'precise' else 2) if paid else 0)


@pytest.mark.asyncio
async def test_paid_key_cannot_override_upstream_paid_permission(effort_env):
    st = effort_env.state
    await st.db.update_key(1, {'allow_anlas': True})
    st.nai.pool[0].allow_anlas = False
    st.settings.queue_timeout = .02
    response = await post('/ai/generate-image', image_body(steps=40))
    assert response.status_code in (402, 429, 503), response.text
    assert effort_env.calls == []
    assert (await st.db.get_counter(1, st.day()))['anlas'] == 0


@pytest.mark.asyncio
async def test_permission_default_is_off_and_admin_can_toggle_one_key(effort_env):
    import httpx
    st = effort_env.state
    assert st.settings.default_allow_anlas is False
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
        assert (await client.post('/admin/api/login', json={'password': 'offline-password'})).status_code == 200
        response = await client.post('/admin/api/keys', json={'name': 'default-off-fixture'})
        assert response.status_code == 200, response.text
        created = response.json()['key']
        assert created['allow_anlas'] is False
        for paid in (True, False):
            changed = await client.patch('/admin/api/keys/' + str(created['id']), json={'allow_anlas': paid})
            assert changed.status_code == 200
            assert changed.json()['key']['allow_anlas'] is paid
            assert bool((await st.db.get_key(created['id']))['allow_anlas']) is paid
    assert not (await st.db.get_key(1))['allow_anlas']
    assert not (await st.db.get_key(2))['allow_anlas']
