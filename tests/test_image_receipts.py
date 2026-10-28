import json

import httpx
import pytest

from app import main
from test_generation_integration import image_body
from test_image_stream_routes import state, event
from test_v5_effort import effort_env


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_receipt_matches_dispatched_medium_and_settled_ledger(effort_env, streaming):
    st = effort_env.state
    await st.db.bump_counters(2, st.day(), v5=8, images=8)
    body = {**image_body(steps=50, sampler='k_dpmpp_2m'), 'model': 'nai-diffusion-5-full'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
        response = await client.post('/ai/generate-image'+('-stream' if streaming else ''), json=body,
                                    headers={'Authorization': 'Bearer fixture-1', 'x-nai-gate-receipt': '1'})
    assert response.status_code == 200
    if streaming:
        frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        receipt = next(frame['receipt'] for frame in frames if frame.get('event_type') == 'gate_receipt')
        assert response.text.index('event: gate_receipt') < response.text.index('event: final')
    else:
        receipt = json.loads(response.headers['x-nai-gate-receipt'])
    assert receipt['model'] == effort_env.calls[0]['model'] == 'nai-diffusion-5-full-medium'
    assert receipt['parameters']['steps'] == 14
    assert receipt['parameters']['sampler'] == 'k_euler_ancestral'
    counter = await st.db.get_counter(1, st.day())
    assert receipt['cost']['v5'] == counter['v5'] == .6
    assert receipt['cost']['anlas'] == counter['anlas'] == 0
    assert receipt['completed_images'] == counter['images'] == 1
    assert 'fixture' not in json.dumps(receipt)


@pytest.mark.asyncio
async def test_receipt_stream_releases_final_only_after_accounting(state):
    import asyncio
    state.db.accounting_release = asyncio.Event()
    sent = []
    async def receive():
        await asyncio.Future()
    async def send(message):
        sent.append(message)
    from test_generation_integration import request
    req = request(image_body())
    req.scope['headers'].append((b'x-nai-gate-receipt', b'1'))
    response = await main.generate_image_stream(req)
    task = asyncio.create_task(response(req.scope, receive, send))
    await asyncio.wait_for(state.db.accounting_entered.wait(), 2)
    assert event() not in [message.get('body') for message in sent]
    state.db.accounting_release.set()
    await task
    wire = b''.join(message.get('body', b'') for message in sent)
    assert wire.index(b'event: gate_receipt') < wire.index(b'event: final')
