"""Image budget admission shares the queue deadline without consuming run slots."""
import asyncio
import socket
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from app import main
from test_generation_integration import FakeState, encoding_body, image_body, post
from test_image_stream_routes import StreamingNai, Frames, event, start_asgi


@pytest.fixture
def state(monkeypatch):
    value = FakeState()
    value.settings.queue_timeout = .04
    monkeypatch.setattr(main, "STATE", value)
    return value


def payload(path):
    return encoding_body() if path.endswith("encode-vibe") else image_body(precise=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/ai/generate-image", "/ai/generate-image-stream", "/ai/encode-vibe"])
async def test_budget_wait_times_out_without_execution_slot_or_upstream(state, path):
    await state.image_budget_lock.acquire()
    task = asyncio.create_task(post(path, payload(path)))
    try:
        await asyncio.sleep(.01)
        assert state.global_waiting == 1 and state.global_active == 0
        assert state.global_sem._value == 3
        assert state.key_sem(1, 2)._value == 2
        response = await asyncio.wait_for(task, .5)
        assert response.status_code == 429
        assert not state.nai.calls and not state.db.charges
        assert state.image_budget_lock.locked()  # Waiter must not release another request's lock.
        assert state.global_active == state.global_waiting == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        state.image_budget_lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["budget", "key", "global"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_partial_admission_timeout_or_cancel_restores_owned_resources(state, stage, cancel):
    key = state.db.keys["fixture-1"]
    state.settings.key_concurrency = 1
    lock = {"budget": state.image_budget_lock, "key": state.key_sem(1, 1), "global": state.global_sem}[stage]
    # Exhaust the selected semaphore/lock so admission stops at this stage.
    held = 3 if stage == "global" else 1
    for _ in range(held):
        await lock.acquire()

    async def enter():
        async with main.acquire_concurrency(key, image=True):
            raise AssertionError("Blocked admission must never reach execution")

    task = asyncio.create_task(enter())
    try:
        await asyncio.sleep(.01)
        assert state.global_waiting == 1 and state.global_active == 0
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(main.GateError) as caught:
                await asyncio.wait_for(task, .5)
            assert caught.value.status == 429
        assert state.global_active == state.global_waiting == 0
        assert state.image_budget_lock.locked() is (stage == "budget")
        assert state.key_sem(1, 1)._value == (0 if stage == "key" else 1)
        assert state.global_sem._value == (0 if stage == "global" else 3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for _ in range(held):
            lock.release()


@pytest.mark.asyncio
async def test_budget_waiter_does_not_block_nonimage_request(state):
    await state.image_budget_lock.acquire()
    task = asyncio.create_task(post("/ai/generate-image", image_body(precise=1)))
    try:
        await asyncio.sleep(.01)
        async with main.acquire_concurrency(state.db.keys["fixture-2"]):
            assert state.global_active == 1 and state.global_waiting == 1
        assert (await task).status_code == 429
    finally:
        state.image_budget_lock.release()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_entire_lock_admission_has_one_deadline(state):
    state.settings.queue_timeout = .2
    await state.image_budget_lock.acquire()
    for _ in range(3):
        await state.global_sem.acquire()
    start = asyncio.get_running_loop().time()

    async def enter():
        async with main.acquire_concurrency(state.db.keys["fixture-1"], image=True):
            raise AssertionError("Global slot is held")

    task = asyncio.create_task(enter())
    await asyncio.sleep(.12)
    state.image_budget_lock.release()
    try:
        with pytest.raises(main.GateError):
            await asyncio.wait_for(task, .15)
        assert asyncio.get_running_loop().time() - start < .3
        assert not state.image_budget_lock.locked()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for _ in range(3):
            state.global_sem.release()


@pytest.mark.asyncio
async def test_cancel_stream_waiting_for_budget_never_dispatches(state):
    state.nai = StreamingNai()
    await state.image_budget_lock.acquire()
    task, disconnected, _ = await start_asgi(state)
    try:
        await asyncio.sleep(.01)
        assert state.global_waiting == 1 and state.global_active == 0
        disconnected.set()
        await task
        assert not state.nai.calls and not state.db.charges
        assert state.global_active == state.global_waiting == 0
    finally:
        state.image_budget_lock.release()


@pytest.mark.asyncio
async def test_successful_stream_holds_budget_and_slot_through_settlement(state):
    state.nai = StreamingNai()
    state.db.accounting_release = asyncio.Event()
    task = asyncio.create_task(post("/ai/generate-image-stream", image_body(precise=1)))
    await asyncio.wait_for(state.db.accounting_entered.wait(), 1)
    try:
        assert state.image_budget_lock.locked() and state.global_active == 1
        assert state.global_waiting == 0
        # The queue deadline must not terminate work that has already started.
        await asyncio.sleep(.06)
        assert not task.done()
    finally:
        state.db.accounting_release.set()
    assert (await task).status_code == 200
    assert len(state.db.charges) == 1 and not state.image_budget_lock.locked()
    assert state.global_active == 0


@pytest.mark.asyncio
async def test_real_http_budget_timeout_is_http_429_before_sse_starts(state):
    await state.image_budget_lock.acquire()
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
            response = await client.post("/nai/ai/generate-image-stream", json=image_body(precise=1))
            assert response.status_code == 429
            assert response.headers["content-type"].startswith("application/json")
        assert not state.nai.calls and state.global_active == state.global_waiting == 0
    finally:
        state.image_budget_lock.release()
        server.should_exit = True
        await asyncio.wait_for(worker, 5)
        sock.close()
