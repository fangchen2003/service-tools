"""Text SSE framing and upstream rejection cleanup; no real upstream calls."""
import asyncio
import json

import httpx
import pytest

from test_nai_integration import make_client
from test_text_stream_lifetime import state, body, post
from test_image_stream_routes import Frames


@pytest.mark.asyncio
async def test_chat_emits_standard_sse_data_events(state):
    state.nai.frames.release.set()
    response = await post("/v1/chat/completions", body(True))
    events = [block.removeprefix("data: ")
              for block in response.text.strip().split("\n\n")]
    assert all(block.startswith("data: ") for block in response.text.strip().split("\n\n"))
    assert events[-1] == "[DONE]"
    chunks = [json.loads(event) for event in events[:-1]]
    assert "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks) == "hello"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


class ErrorBody(httpx.AsyncByteStream):
    def __init__(self, *, block_close=False):
        self.read = False
        self.closed = False
        self.closing = asyncio.Event()
        self.release = asyncio.Event() if block_close else None

    async def __aiter__(self):
        self.read = True
        raise httpx.ReadError("private upstream rejection body")
        yield b""  # Make this a stream whose first read fails.

    async def aclose(self):
        self.closing.set()
        if self.release:
            await self.release.wait()
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", [False, True])
@pytest.mark.parametrize("status,expected", [
    (400, 400), (422, 422), (401, 502), (403, 502), (404, 502),
    (429, 429), (500, 502), (502, 502), (503, 503), (302, 502),
])
async def test_upstream_rejections_are_not_success_streams(state, chat, status, expected):
    frames = ErrorBody()

    async def handler(_request):
        return httpx.Response(status, stream=frames)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        nai, _ = make_client(http=http)
        state.nai = nai
        path = "/v1/chat/completions" if chat else "/ai/generate-stream"
        response = await post(path, body(chat))
        assert response.status_code == expected
        assert response.headers["content-type"] == "application/json"
        assert "private" not in response.text
        assert frames.closed and not frames.read
        assert nai.pool[0].last_ok == 0
        assert nai.pool[0].disabled is (status == 401)
        assert state.global_active == 0 and state.global_sem._value == 3


@pytest.mark.asyncio
async def test_cancel_during_rejection_close_finishes_cleanup():
    frames = ErrorBody(block_close=True)

    async def handler(_request):
        return httpx.Response(503, stream=frames)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        nai, _ = make_client(http=http)
        task = asyncio.create_task(nai.stream("https://offline.invalid/ai/generate-stream", {}))
        await asyncio.wait_for(frames.closing.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not frames.closed
        frames.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert frames.closed and not frames.read


@pytest.mark.asyncio
@pytest.mark.parametrize("chat,stream", [(False, True), (True, True), (True, False)])
@pytest.mark.parametrize("error", [
    b'event: error\r\ndata: {"message":"fixture-private-upstream"}\r\n\r\n',
    b'data: {"error":"fixture-private-upstream"}\r\n\r\n',
    b'data: <html>fixture-private-upstream</html>\r\n\r\n',
])
async def test_stream_errors_filtered_before_delivery_and_count_only_prior_tokens(state, chat, stream, error):
    wire = b'data: {"token":"hello"}\r\n\r\n' + error
    frames = Frames([bytes([byte]) for byte in wire])

    async def open_stream(*args):
        return httpx.Response(200, stream=frames)

    state.nai.stream = open_stream
    path = "/v1/chat/completions" if chat else "/ai/generate-stream"
    response = await post(path, body(chat, stream))
    await asyncio.sleep(0)
    assert response.status_code == (200 if stream else 502)
    assert "fixture-private" not in response.text
    assert "error" in response.text
    if stream:
        assert "hello" in response.text
    if chat and stream:
        assert '"finish_reason": "stop"' not in response.text
        assert response.text.endswith("data: [DONE]\n\n")
    assert sum(values.get("text_tokens", 0) for _, values in state.db.charges) == 1
    assert frames.closed and state.global_active == 0
    assert all("fixture-private" not in str(log) for log in state.db.logs)
