from contextlib import asynccontextmanager

import pytest

from app.nai import UpstreamError
from test_generation_integration import image_body, post
from test_image_stream_routes import state


@pytest.mark.asyncio
async def test_stream_dispatch_records_real_wait_and_excludes_generation(state):
    events = []
    state.image_queue._observe = events.append
    response = await post('/ai/generate-image-stream', image_body())
    assert response.status_code == 200
    assert events[-1]['outcome'] == 'finished'
    assert events[-1]['wait_seconds'] is not None
    assert events[-1]['run_seconds'] is not None
    assert events[-1]['ended_at'] >= events[-1]['dispatched_at'] >= events[-1]['created_at']


@pytest.mark.asyncio
async def test_stream_upstream_slot_timeout_not_misreported_as_cancel_or_finish(state):
    events = []
    state.image_queue._observe = events.append
    @asynccontextmanager
    async def no_slot(url, body, **kwargs):
        raise UpstreamError(429, '上游图片任务排队超时，请稍后再试')
        yield
    state.nai.image_stream = no_slot
    await post('/ai/generate-image-stream', image_body())
    assert events[-1]['outcome'] == 'timed_out'
    assert events[-1]['wait_seconds'] is None
