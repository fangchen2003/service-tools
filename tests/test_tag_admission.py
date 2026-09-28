"""Autocomplete queues briefly without reserving future image slots."""
import asyncio
import socket
import time
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from app import main
from app.config import Settings
from app.nai import NaiClient, UpstreamError
from app.state import GateState
from test_generation_integration import FakeDB, FakeNai, post, request


PATH = "/ai/generate-image/suggest-tags"


@pytest.fixture
def state(monkeypatch):
    st = GateState(Settings(key_image_min_interval=.03, image_min_interval=.03,
                            queue_timeout=1.5, global_concurrency=1, key_concurrency=1))
    st.db, st.nai = FakeDB(), FakeNai()
    monkeypatch.setattr(main, "STATE", st)
    return st


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_second_tag_request_queues_without_future_generation_reservations(state, method):
    state.nai.release = asyncio.Event()
    tasks = [asyncio.create_task(main.suggest_tags(request({}, method=method))) for _ in range(2)]
    try:
        await asyncio.wait_for(state.nai.entered.wait(), 1)
        await asyncio.sleep(.01)
        assert not tasks[1].done()
        assert not state._key_image_next_at and not state._rpm
        assert len(state.nai.calls) == 1
        state.nai.release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert all(result.status_code == 200 for result in results)
        assert len(state.nai.calls) == 2
    finally:
        state.nai.release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_rate_window_separate_from_generation_and_other_key(state):
    assert (await post(PATH, {})).status_code == 200
    assert (await post(PATH, {})).status_code == 200
    assert (await post(PATH, {}, "fixture-2")).status_code == 200
    assert await state.hit_rpm(1, 1)
    assert not await state.hit_rpm(1, 1)
    assert not state._tag_active and not state._key_image_next_at


@pytest.mark.asyncio
async def test_global_tag_capacity_bounds_distinct_keys(state):
    state.nai.release = asyncio.Event()
    first = asyncio.create_task(post(PATH, {}))
    await asyncio.wait_for(state.nai.entered.wait(), 1)
    try:
        second = asyncio.create_task(post(PATH, {}, "fixture-2"))
        await asyncio.sleep(.01)
        assert not second.done()
        assert len(state.nai.calls) == 1
    finally:
        state.nai.release.set()
        await first
    assert (await second).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "cancel", "upstream", "bad_json"])
async def test_every_exit_releases_tag_admission(state, failure):
    if failure in ("timeout", "cancel"):
        state.nai.release = asyncio.Event()
    if failure == "timeout":
        state.settings.queue_timeout = .03
    if failure == "upstream":
        state.nai.error = UpstreamError(503, "fixture unavailable")
    body = [] if failure == "bad_json" else {}
    task = asyncio.create_task(post(PATH, body))
    if failure == "cancel":
        await asyncio.wait_for(state.nai.entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        response = await task
        assert response.status_code == {"timeout": 429, "upstream": 503, "bad_json": 400}[failure]
    assert not state._tag_active
    assert state._tag_waiting == 0
    assert state.global_active == state.global_waiting == 0


@pytest.mark.asyncio
async def test_admission_happens_before_post_body_read(state):
    state._tag_next_at[1] = time.monotonic() + 60
    state.settings.queue_timeout = .03
    incoming = request({})

    async def forbidden():
        raise AssertionError("Queued autocomplete must not buffer its body")

    incoming._receive = forbidden
    with pytest.raises(main.GateError) as caught:
        await main.suggest_tags(incoming)
    assert caught.value.status == 429


@pytest.mark.asyncio
async def test_disconnected_waiter_never_dispatches_upstream(state):
    await state.global_sem.acquire()
    incoming = request({}, method="GET")
    task = asyncio.create_task(main.suggest_tags(incoming))
    await asyncio.sleep(.01)

    async def disconnect():
        return {"type": "http.disconnect"}

    incoming._receive = disconnect
    state.global_sem.release()
    with pytest.raises(main.GateError) as caught:
        await task
    assert caught.value.status == 499
    assert not state.nai.calls and not state._tag_active


@pytest.mark.asyncio
async def test_admin_cannot_bypass_tag_resource_limit(state):
    state.db.keys["fixture-1"]["is_admin"] = True
    assert (await post(PATH, {})).status_code == 200
    state._tag_next_at[1] = time.monotonic() + 60
    state.settings.queue_timeout = .03
    assert (await post(PATH, {})).status_code == 429


@pytest.mark.asyncio
async def test_tag_log_distinguishes_local_unavailable_from_upstream_http_503(state):
    state.nai.error = UpstreamError(503, "上游令牌全部被限流或不可用，请稍后再试")
    assert (await post(PATH, {})).status_code == 503
    await asyncio.sleep(0)
    assert state.db.logs[-1][1]["detail"] == "上游令牌全部被限流或不可用，请稍后再试"

    state._tag_next_at[1] = 0
    state.nai.error = None
    state.nai.status = 503
    assert (await post(PATH, {})).status_code == 503
    await asyncio.sleep(0)
    assert state.db.logs[-1][1]["detail"] == "上游标签接口返回 HTTP 503"


@pytest.mark.asyncio
async def test_busy_token_waits_without_reserving_future_image_slots():
    client = NaiClient(["fixture-only"], "https://fixture.invalid", "https://fixture.invalid",
                       "https://fixture.invalid", db=FakeDB(), day_fn=lambda: "2026-09-22",
                       v5_daily_limits=[100], allow_anlas=[True], image_min_interval=.03)
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"tags": []})

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        token = client.pool[0]
        token.image_next_at = time.monotonic() + .03
        before = token.image_next_at
        pending = asyncio.create_task(client.request(
            "GET", "https://fixture.invalid/tags", image_lane=True, wait_for_image_slot=False))
        await asyncio.sleep(.01)
        assert not pending.done()
        assert token.image_next_at == before and not calls
        response = await asyncio.wait_for(pending, 1)
        assert response.status_code == 200 and len(calls) == 1
        assert token.image_next_at > time.monotonic()
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_cancelled_token_wait_does_not_reserve_future_image_slot():
    client = NaiClient(["fixture-only"], "https://fixture.invalid", "https://fixture.invalid",
                       "https://fixture.invalid", db=FakeDB(), day_fn=lambda: "2026-09-22",
                       v5_daily_limits=[100], allow_anlas=[True], image_min_interval=.03)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"tags": []})))
    try:
        token = client.pool[0]
        token.image_next_at = time.monotonic() + 1
        before = token.image_next_at
        pending = asyncio.create_task(client.request(
            "GET", "https://fixture.invalid/tags", image_lane=True, wait_for_image_slot=False))
        await asyncio.sleep(.01)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert token.image_next_at == before
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_real_socket_disconnected_queue_does_not_reach_upstream(state):
    state.settings.queue_timeout = 1
    await state.global_sem.acquire()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(main.app, lifespan="off", log_level="critical", access_log=False))
    worker = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(.01)
        assert server.started
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                                     headers={"Authorization": "Bearer fixture-1"}) as client:
            with pytest.raises(httpx.ReadTimeout):
                await client.get(PATH + "?prompt=fixture", timeout=.05)
            assert (await client.get(PATH)).status_code == 429
            state.global_sem.release()
            for _ in range(100):
                if not state._tag_active:
                    break
                await asyncio.sleep(.01)
            assert not state._tag_active and not state.nai.calls
            assert state.global_waiting == state.global_active == 0
    finally:
        server.should_exit = True
        await asyncio.wait_for(worker, 5)
        sock.close()
