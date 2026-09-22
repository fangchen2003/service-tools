"""Text stream slots must cover upstream lifetime, including disconnect cleanup."""
import asyncio
import socket
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from app import main
from app.nai import UpstreamError
from test_generation_integration import FakeState, request, post


class TextFrames(httpx.AsyncByteStream):
    def __init__(self):
        self.release = asyncio.Event()
        self.reading = asyncio.Event()
        self.closing = asyncio.Event()
        self.close_release = None
        self.closed = False
        self.failure = False

    async def __aiter__(self):
        yield b'data: {"token":"hello"}\n\n'
        self.reading.set()
        await self.release.wait()
        if self.failure:
            raise httpx.ReadError("fixture failure")
        yield b'data: [DONE]\n\n'

    async def aclose(self):
        self.closing.set()
        if self.close_release:
            await self.close_release.wait()
        self.closed = True


@pytest.fixture
def state(monkeypatch):
    value = FakeState()
    value.settings.max_text_output_tokens = 200
    value.settings.max_input_chars = 10000
    value.settings.queue_timeout = .03
    value.settings.key_concurrency = 1
    for key in value.db.keys.values():
        key.update(daily_text_tokens=-1, rpm=100)
    original_counter = value.db.get_counter

    async def counter(*args):
        return {**await original_counter(*args), "text_tokens": 0}

    value.db.get_counter = counter
    frames = TextFrames()
    calls = []

    async def stream(*args):
        calls.append(args)
        if value.nai.error:
            raise value.nai.error
        return httpx.Response(200, stream=frames)

    value.nai = SimpleNamespace(stream=stream, calls=calls, frames=frames, error=None,
                                text_host="https://fixture.invalid", legacy_text_host="https://fixture.invalid")
    monkeypatch.setattr(main, "STATE", value)
    return value


def body(chat, stream=True):
    if chat:
        return {"messages": [{"role": "user", "content": "fixture"}], "stream": stream}
    return {"model": "llama-3-erato-v1", "input": "fixture", "parameters": {"max_length": 20}}


async def response_for(chat):
    return await (main.v1_chat if chat else main.generate_stream)(request(body(chat)))


async def start_response(response, *, fail_start=False, spec="2.4"):
    disconnected = asyncio.Event()
    messages = []

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if fail_start and message["type"] == "http.response.start":
            raise OSError("fixture send failure")
        messages.append(message)

    scope = {"type": "http", "method": "POST", "asgi": {"spec_version": spec}}
    task = asyncio.create_task(response(scope, receive, send))
    return task, disconnected, messages


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", [False, True])
@pytest.mark.parametrize("limit", ["key", "global"])
async def test_open_stream_retains_slot_and_next_request_times_out(state, chat, limit):
    if limit == "global":
        state.global_sem = asyncio.Semaphore(1)
    response = await response_for(chat)
    task, _, messages = await start_response(response)
    try:
        await asyncio.wait_for(state.nai.frames.reading.wait(), 1)
        assert state.global_active == 1
        path = "/v1/chat/completions" if chat else "/ai/generate-stream"
        second = await post(path, body(chat), "fixture-2" if limit == "global" else "fixture-1")
        assert second.status_code == 429
        assert len(state.nai.calls) == 1
        assert state.global_active == 1 and state.global_waiting == 0
    finally:
        state.nai.frames.release.set()
        await task
    assert state.global_active == 0 and state.nai.frames.closed
    assert state.key_sem(1, 1)._value == 1
    assert b"hello" in b"".join(message.get("body", b"") for message in messages)
    assert (await post(path, body(chat))).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", [False, True])
@pytest.mark.parametrize("failure", ["cancel", "disconnect", "send", "read"])
async def test_failure_closes_upstream_and_releases_slots(state, chat, failure):
    response = await response_for(chat)
    task, disconnected, _ = await start_response(response, fail_start=failure == "send",
                                                spec="2.0" if failure == "disconnect" else "2.4")
    if failure != "send":
        await asyncio.wait_for(state.nai.frames.reading.wait(), 1)
        if failure == "cancel":
            task.cancel()
        elif failure == "disconnect":
            disconnected.set()
        else:
            state.nai.frames.failure = True
            state.nai.frames.release.set()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    assert state.nai.frames.closed
    assert state.global_active == state.global_waiting == 0
    assert state.key_sem(1, 1)._value == 1 and state.global_sem._value == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", [False, True])
async def test_repeated_cancel_keeps_slot_until_close_finishes(state, chat):
    state.nai.frames.close_release = asyncio.Event()
    response = await response_for(chat)
    task, _, _ = await start_response(response)
    await asyncio.wait_for(state.nai.frames.reading.wait(), 1)
    task.cancel()
    await asyncio.wait_for(state.nai.frames.closing.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    try:
        assert not task.done() and state.global_active == 1
    finally:
        state.nai.frames.close_release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert state.nai.frames.closed and state.global_active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", [False, True])
async def test_upstream_rejection_preserves_http_error_and_releases_slot(state, chat):
    state.nai.error = UpstreamError(503, "fixture unavailable")
    path = "/v1/chat/completions" if chat else "/ai/generate-stream"
    response = await post(path, body(chat))
    assert response.status_code == 503
    assert state.global_active == 0 and state.key_sem(1, 1)._value == 1


@pytest.mark.asyncio
async def test_nonstream_chat_still_collects_text_and_releases_slot(state):
    task = asyncio.create_task(post("/v1/chat/completions", body(True, stream=False)))
    await asyncio.wait_for(state.nai.frames.reading.wait(), 1)
    assert state.global_active == 1
    state.nai.frames.release.set()
    response = await task
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hello"
    assert state.global_active == 0 and state.nai.frames.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", [False, True])
async def test_cancelled_queue_waiter_does_not_dispatch_or_leak(state, chat):
    first = await response_for(chat)
    state.settings.queue_timeout = 10
    second = asyncio.create_task(response_for(chat))
    await asyncio.sleep(0)
    assert state.global_waiting == 1
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert len(state.nai.calls) == 1 and state.global_waiting == 0
    task, _, _ = await start_response(first)
    state.nai.frames.release.set()
    await task
    assert state.global_active == 0 and state.key_sem(1, 1)._value == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path,chat", [("/nai/ai/generate-stream", False), ("/v1/chat/completions", True)])
async def test_real_socket_enforces_limit_and_disconnect_cleans_up(state, path, chat):
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
            async with client.stream("POST", path, json=body(chat)) as response:
                assert response.status_code == 200
                chunks = response.aiter_bytes()
                async for chunk in chunks:
                    if b"hello" in chunk:
                        break
                assert state.global_active == 1
                assert (await client.post(path, json=body(chat))).status_code == 429
                assert len(state.nai.calls) == 1
            # Exiting the response closes the socket while upstream is blocked.
            for _ in range(100):
                if state.nai.frames.closed and state.global_active == 0:
                    break
                await asyncio.sleep(.01)
            assert state.nai.frames.closed and state.global_active == 0
            assert state.key_sem(1, 1)._value == 1
    finally:
        state.nai.frames.release.set()
        server.should_exit = True
        await asyncio.wait_for(worker, 5)
        sock.close()
