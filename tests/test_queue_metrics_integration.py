"""Telemetry hooks run at actual mock upstream dispatch, under existing auth."""
import asyncio
import time

from fastapi import FastAPI
import httpx
import pytest

from app import main
from app.admin import router
from app.config import Settings
from app.nai import NaiClient
from app.state import GateState
from test_generation_integration import FakeDB, image_body
from test_nai_integration import PNG


@pytest.mark.asyncio
async def test_nonstream_dispatch_hook_runs_after_pacing_before_http():
    seen = []
    client = NaiClient(['fixture'], 'https://fixture.invalid', 'https://fixture.invalid',
                       'https://fixture.invalid', db=FakeDB(), day_fn=lambda: '2026-11-06',
                       image_min_interval=.01, v5_daily_limits=[100], allow_anlas=[True])
    async def handler(request):
        assert len(seen) == 1
        return httpx.Response(200, json={})
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    start = time.monotonic()
    client.pool[0].image_next_at = start + .03
    try:
        await client.request('POST', 'https://fixture.invalid/image', image_lane=True,
                             on_dispatch=lambda: seen.append(time.monotonic()))
        assert seen[0] - start >= .02
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_admin_metrics_auth_validation_and_private_no_store(tmp_path):
    state = GateState(Settings(data_dir=tmp_path, admin_password='fixture-password',
                               secret_key='fixture-secret', admin_cookie_secure=False))
    app = FastAPI()
    app.state.gate = state
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture.invalid') as client:
        assert (await client.get('/admin/api/queue-metrics')).status_code == 401
        await client.post('/admin/api/login', json={'password': 'fixture-password'})
        await state.queue_metrics.start()
        try:
            response = await client.get('/admin/api/queue-metrics')
            assert response.status_code == 200, response.text
            assert response.headers['cache-control'] == 'no-store'
            assert response.json()['summary']['wait_p95_seconds'] is None
            assert response.json()['assessment']['level'] == 'insufficient'
            assert (await client.get('/admin/api/queue-metrics?hours=168')).status_code == 200
            for value in ('0', '1', '999999', 'nope'):
                assert (await client.get('/admin/api/queue-metrics?hours=' + value)).status_code == 422
        finally:
            await state.queue_metrics.close()


@pytest.mark.asyncio
async def test_real_generation_route_records_upstream_wait_without_payloads(tmp_path, monkeypatch):
    state = GateState(Settings(data_dir=tmp_path, nai_tokens=['fixture'],
                               key_image_min_interval=0, image_min_interval=0))
    await state.queue_metrics.start()
    await state.db.connect()
    await state.db.create_key(dict(name='fixture', token='fixture-key', daily_images=100,
                                   monthly_anlas=0, daily_text_tokens=0, rpm=10,
                                   image_model_scope='all'))
    state.nai._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=PNG, headers={'content-type': 'image/png'})))
    state.nai.pool[0].image_next_at = time.monotonic() + .1
    monkeypatch.setattr(main, 'STATE', state)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://fixture.invalid') as client:
            response = await client.post('/ai/generate-image', json=image_body(),
                                         headers={'Authorization': 'Bearer fixture-key'})
            assert response.status_code == 200, response.text
        sample = state.queue_metrics_sample()
        assert sample['busy_seconds'] >= .05
        sample['ts'] = time.time()
        await state.queue_metrics.sample(sample)
        report = await state.queue_metrics.report()
        assert report['summary']['dispatched'] == 1
        assert report['summary']['finished'] == 1
        assert report['summary']['wait_p95_seconds'] >= .05
        cursor = await state.queue_metrics._db.execute('SELECT * FROM queue_tasks')
        row = dict(await cursor.fetchone())
        assert row['dispatched_at'] >= row['admitted_at'] >= row['created_at']
        assert row['ended_at'] >= row['dispatched_at']
        assert 'input' not in row and 'token' not in row and 'model' not in row
    finally:
        await state.queue_metrics.close()
        await state.nai._client.aclose()
        await state.db.close()
