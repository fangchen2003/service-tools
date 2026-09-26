"""Image streaming integration: wire frames, quotas and ASGI disconnect lifetime."""
import asyncio
import copy
import json
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio
import httpx
import pytest
import uvicorn

from app import main
from app.nai import UpstreamError
from app.policy import estimate_image_cost
from test_generation_integration import FakeState, image_body, request, post


PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII="


def event(kind="final", index=0):
    return ("event: " + kind + "\ndata: " + json.dumps({
        "event_type": kind, "samp_ix": index, "gen_id": 123, "image": PNG,
    }) + "\n\n").encode()


class Frames(httpx.AsyncByteStream):
    def __init__(self, items):
        self.items = items
        self.closed = False

    async def __aiter__(self):
        for item in self.items:
            if isinstance(item, asyncio.Event):
                await item.wait()
            elif isinstance(item, Exception):
                raise item
            else:
                yield item

    async def aclose(self):
        self.closed = True


class StreamingNai:
    image_host = "https://fixture.invalid"

    def __init__(self):
        self.frames = Frames([event("intermediate"), event()])
        self.calls = []
        self.entered = asyncio.Event()
        self.counts = []
        self.error = None
        self.content_type = "text/event-stream; charset=utf-8"
        self.slot_entered = asyncio.Event()
        self.slot_release = None
        self.accounting_entered = asyncio.Event()
        self.accounting_release = None

    @asynccontextmanager
    async def image_stream(self, url, body, **kwargs):
        self.slot_entered.set()
        if self.slot_release:
            await self.slot_release.wait()
        kwargs["on_dispatch"]()
        self.calls.append((url, copy.deepcopy(body), kwargs))
        self.entered.set()
        if self.error:
            if self.error.status == 429:
                await kwargs["on_rate_limited"](30)
            raise self.error
        response = httpx.Response(200, headers={"content-type": self.content_type}, stream=self.frames)
        handle = SimpleNamespace(response=response, completed_images=0)
        try:
            yield handle
        finally:
            await response.aclose()
            self.accounting_entered.set()
            if self.accounting_release:
                await self.accounting_release.wait()
            self.counts.append(handle.completed_images)


@pytest.fixture
def state(monkeypatch):
    state = FakeState()
    state.nai = StreamingNai()
    monkeypatch.setattr(main, "STATE", state)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/ai/generate-image-stream", "/nai/ai/generate-image-stream"])
async def test_stream_reuses_reference_validation_cost_and_sse_wire_format(state, path):
    response = await post(path, image_body(precise=1))
    assert response.status_code == 200
    assert response.content == event("intermediate") + event()
    assert response.headers["x-accel-buffering"] == "no"
    assert state.nai.calls[0][1]["parameters"]["stream"] == "sse"
    assert state.nai.calls[0][2]["requires_anlas"] is True
    assert state.nai.counts == [1]
    assert state.db.charges[0][1]["anlas"] == 5
    assert state.nai.frames.closed and not state.image_budget_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("frames", [[], [event("intermediate")], [b'data: {"event_type":"error"}\n\n'],
                                          [event()[:-1]], [b'data: invalid\n\n']])
async def test_empty_progress_error_truncated_and_malformed_never_charge(state, frames):
    state.nai.frames = Frames(frames)
    response = await post("/ai/generate-image-stream", image_body(precise=1))
    assert b'"event_type": "error"' in response.content
    assert not state.db.charges and state.nai.counts == [0]
    assert state.global_active == 0 and not state.image_budget_lock.locked()


@pytest.mark.asyncio
async def test_partial_batch_charges_completed_reference_fee_and_counts_duplicate_once(state):
    body = image_body(precise=1, n_samples=2)
    state.nai.frames = Frames([event(), event(), httpx.ReadError("private transport detail")])
    response = await post("/ai/generate-image-stream", body)
    assert b"private transport detail" not in response.content
    assert state.nai.counts == [1]
    assert state.db.charges[0][1]["images"] == 1
    assert state.db.charges[0][1]["anlas"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize('completed,anlas', [(2, 9), (3, 18)])
async def test_stream_reference_batch_discount_uses_completed_count(state, completed, anlas):
    body = image_body(precise=1, width=512, height=512, steps=20,
                      image='fixture', strength=1, n_samples=3)
    state.nai.frames = Frames([*(event(index=i) for i in range(completed)),
                              httpx.ReadError('fixture')])
    await post('/ai/generate-image-stream', body)
    assert state.db.charges[0][1]['anlas'] == anlas
    assert state.db.charges[0][1]['images'] == completed


@pytest.mark.asyncio
@pytest.mark.parametrize('indices,anlas', [([0], 0), ([1], 0), ([1, 0, 1], 4)])
async def test_partial_batch_applies_one_discount_to_completed_images(state, indices, anlas):
    # Gate 只对完整结果结算；折扣按已完成张数重算，不由到达顺序或重复事件决定。
    body = image_body(width=512, height=512, steps=20, image='fixture', strength=1, n_samples=3)
    state.nai.frames = Frames([*(event(index=i) for i in indices), httpx.ReadError('fixture')])
    await post('/ai/generate-image-stream', body)
    charge, = state.db.charges
    assert charge[1]['anlas'] == anlas
    assert charge[1]['images'] == len(set(indices))
    assert charge[1]['legacy_free_images'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('paid,finals,expected_charge,expected_uncertain', [
    (True, [], 0, 2), (True, [0], 2, 0), (False, [], 0, 0),
])
async def test_stream_anomaly_is_estimated_separately_from_user_charge(
        state, paid, finals, expected_charge, expected_uncertain):
    state.nai.frames = Frames([event('intermediate'), *(event(index=i) for i in finals),
                              httpx.ReadError('private failure')])
    await post('/ai/generate-image-stream', image_body(width=256, height=256, steps=29 if paid else 28))
    assert sum(row['anlas'] for _, row in state.db.charges) == expected_charge
    assert sum(row.get('unconfirmed_anlas', 0) for _, row in state.db.logs) == expected_uncertain
    assert sum(row.get('unconfirmed_anlas', 0) > 0 for _, row in state.db.logs) == bool(expected_uncertain)
    assert state.global_active == 0 and not state.image_budget_lock.locked()


@pytest.mark.asyncio
async def test_partial_reference_batch_records_only_unsettled_difference_once(state):
    state.nai.frames = Frames([event(), event(), httpx.ReadError('fixture')])
    await post('/ai/generate-image-stream', image_body(
        precise=1, width=512, height=512, steps=20, image='fixture', strength=1, n_samples=3))
    assert sum(row['anlas'] for _, row in state.db.charges) == 5
    pending = [row for _, row in state.db.logs if row.get('unconfirmed_anlas', 0) > 0]
    assert len(pending) == 1 and pending[0]['unconfirmed_anlas'] == 13  # 整单18，完整首张5。


@pytest.mark.asyncio
@pytest.mark.parametrize('uncertain', [False, True])
async def test_stream_before_headers_preserves_upstream_outcome_flag(state, uncertain):
    state.nai.error = UpstreamError(502, 'fixture', billing_uncertain=uncertain)
    await post('/ai/generate-image-stream', image_body(width=256, height=256, steps=29))
    assert not state.db.charges
    assert sum(row.get('unconfirmed_anlas', 0) for _, row in state.db.logs) == (2 if uncertain else 0)


@pytest.mark.asyncio
async def test_http_failure_before_stream_and_cooldown_stays_http_error(state):
    state.nai.error = UpstreamError(429, "fixture rate limit")
    response = await post("/ai/generate-image-stream", image_body())
    assert response.status_code == 429 and response.headers["content-type"] == "application/json"
    assert state.cooldowns == [60] and not state.db.charges
    assert len(state.db.logs) == 1 and state.db.logs[0][1]['unconfirmed_anlas'] == 0


@pytest.mark.asyncio
async def test_wrong_content_type_rejected_before_headers_and_no_charge(state):
    state.nai.content_type = "application/json"
    response = await post("/ai/generate-image-stream", image_body())
    assert response.status_code == 502 and not state.db.charges


@pytest.mark.asyncio
async def test_stream_preserves_permission_and_parameter_rejections(state):
    state.db.keys["fixture-1"]["allow_anlas"] = False
    assert (await post("/ai/generate-image-stream", image_body(precise=1))).status_code == 402
    assert (await post("/ai/generate-image-stream", image_body(), "unknown")).status_code == 401
    invalid = image_body() | {"model": "unknown"}
    assert (await post("/ai/generate-image-stream", invalid)).status_code == 400
    assert not state.nai.calls


async def start_asgi(state, *, send_failure=False):
    response = await main.generate_image_stream(request(image_body(precise=1)))
    disconnect = asyncio.Event()
    messages = []

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if send_failure:
            raise OSError("client gone")
        messages.append(message)

    task = asyncio.create_task(response({"type": "http"}, receive, send))
    return task, disconnect, messages


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["headers", "progress", "accounting", "token_accounting", "send_failure", "cancel"])
async def test_disconnect_does_not_release_budget_or_skip_final_settlement(state, when):
    release = asyncio.Event()
    state.nai.frames = Frames([release, event()]) if when == "headers" else Frames([event("intermediate"), release, event()])
    if when == "accounting":
        state.db.accounting_release = asyncio.Event()
        release.set()
    if when == "token_accounting":
        state.nai.accounting_release = asyncio.Event()
        release.set()
    task, disconnected, messages = await start_asgi(state, send_failure=when == "send_failure")
    await state.nai.entered.wait()
    if when == "accounting":
        await state.db.accounting_entered.wait()
    if when == "token_accounting":
        await state.nai.accounting_entered.wait()
    if when == "cancel":
        task.cancel()
    else:
        disconnected.set()
    await asyncio.sleep(0.01)
    assert not task.done() and state.image_budget_lock.locked() and state.global_active == 1
    assert not state.db.charges
    release.set()
    if state.db.accounting_release:
        state.db.accounting_release.set()
    if state.nai.accounting_release:
        state.nai.accounting_release.set()
    if when == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert state.nai.frames.closed and state.nai.counts == [1]
    assert state.db.charges[0][1]["anlas"] == 5
    assert state.global_active == 0 and not state.image_budget_lock.locked()
    assert not any(row.get('unconfirmed_anlas', 0) for _, row in state.db.logs)


@pytest.mark.asyncio
async def test_cancel_anyio_scope_drains_upstream_and_settles(state):
    release = asyncio.Event()
    state.nai.frames = Frames([release, event()])
    scope = anyio.CancelScope()
    response = await main.generate_image_stream(request(image_body(precise=1)))

    async def receive():
        await asyncio.Event().wait()

    async def send(_message):
        pass

    async def run():
        with scope:
            await response({"type": "http"}, receive, send)

    task = asyncio.create_task(run())
    await state.nai.entered.wait()
    scope.cancel()
    await asyncio.sleep(0.01)
    assert state.image_budget_lock.locked() and not task.done()
    release.set()
    await task
    assert state.nai.counts == [1] and len(state.db.charges) == 1
    assert not state.image_budget_lock.locked()


@pytest.mark.asyncio
async def test_disconnect_while_queued_never_dispatches(state):
    state.global_sem = asyncio.Semaphore(0)
    task, disconnected, _ = await start_asgi(state)
    await asyncio.sleep(0.01)
    assert state.global_waiting == 1
    disconnected.set()
    await task
    assert not state.nai.calls and state.global_waiting == 0
    assert state.semaphores[1]._value == state.settings.key_concurrency


@pytest.mark.asyncio
async def test_disconnect_during_token_pacing_cancels_before_http_dispatch(state):
    state.nai.slot_release = asyncio.Event()
    task, disconnected, _ = await start_asgi(state)
    await state.nai.slot_entered.wait()
    disconnected.set()
    await asyncio.wait_for(task, 1)
    assert not state.nai.calls and not state.db.charges
    assert state.global_active == 0 and not state.image_budget_lock.locked()


@pytest.mark.asyncio
async def test_waiting_stream_rechecks_quota_after_prior_settlement(state):
    state.db.keys["fixture-1"].update(daily_anlas=5)
    release = asyncio.Event()
    state.nai.frames = Frames([release, event()])
    first = asyncio.create_task(post("/ai/generate-image-stream", image_body(precise=1)))
    await state.nai.entered.wait()
    second = asyncio.create_task(post("/ai/generate-image-stream", image_body(precise=1)))
    await asyncio.sleep(0.01)
    assert len(state.nai.calls) == 1
    release.set()
    assert (await first).status_code == 200
    assert (await second).status_code == 402
    assert len(state.nai.calls) == 1 and len(state.db.charges) == 1


@pytest.mark.asyncio
async def test_real_http_delivers_preview_before_final_without_buffering(state):
    release = asyncio.Event()
    state.nai.frames = Frames([event("intermediate"), release, event()])
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(main.app, lifespan="off", log_level="critical"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        async with httpx.AsyncClient(trust_env=False) as client:
            async with client.stream("POST", f"http://127.0.0.1:{port}/ai/generate-image-stream",
                                     json=image_body(precise=1), headers={"Authorization": "Bearer fixture-1"}) as response:
                chunks = response.aiter_bytes()
                first = await asyncio.wait_for(anext(chunks), 2)
                assert response.status_code == 200 and b"intermediate" in first
                assert not state.db.charges and state.image_budget_lock.locked()
                release.set()
                remaining = b"".join([chunk async for chunk in chunks])
                assert b'"event_type": "final"' in remaining
        assert state.nai.counts == [1] and len(state.db.charges) == 1
    finally:
        release.set()
        server.should_exit = True
        await serving
        listener.close()
