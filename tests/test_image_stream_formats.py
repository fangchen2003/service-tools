"""Launcher framing and error filtering, with synthetic upstream frames only."""
import base64
import json
import struct

import httpx
import msgpack
import pytest

from app.image_events import ImageEventTracker, ImageStreamProtocolError, MAX_EVENT_BYTES
from test_generation_integration import image_body, post
from test_image_stream_routes import state, Frames, PNG
from test_nai_integration import make_client


def frame(payload, mode):
    if mode == "msgpack":
        data = msgpack.packb(payload, use_bin_type=True)
        return struct.pack(">I", len(data)) + data
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


def unpack_frames(data):
    result = []
    while data:
        size, = struct.unpack(">I", data[:4])
        assert size > 0 and len(data) >= size + 4
        result.append(msgpack.unpackb(data[4:4 + size], raw=False))
        data = data[size + 4:]
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("binary", [True, False])
@pytest.mark.parametrize("content_type", ["application/x-msgpack", "application/octet-stream", "text/event-stream"])
async def test_msgpack_binary_and_base64_images_preserve_wire_and_settle(state, binary, content_type):
    image = base64.b64decode(PNG) if binary else PNG
    frames = frame({"event_type": "intermediate", "step_ix": 1, "image": image}, "msgpack")
    frames += frame({"event_type": "final", "samp_ix": 0, "image": image}, "msgpack")
    state.nai.frames = Frames([bytes([byte]) for byte in frames])
    state.nai.content_type = content_type
    response = await post("/ai/generate-image-stream", image_body(stream="msgpack"))
    assert response.content == frames
    assert response.headers["content-type"] == "application/x-msgpack"
    assert unpack_frames(response.content)[-1]["image"] == image
    assert state.nai.calls[0][1]["parameters"]["stream"] == "msgpack"
    assert state.nai.counts == [1] and state.db.charges[0][1]["images"] == 1
    assert state.nai.frames.closed and not state.image_budget_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,chunk_size", [("sse", 1), ("sse", 65536), ("msgpack", 1), ("msgpack", 65536)])
async def test_error_event_never_leaks_and_keeps_completed_prefix(state, mode, chunk_size):
    final = {"event_type": "final", "samp_ix": 0, "image": PNG}
    wire = frame(final, mode)
    wire += frame({"event_type": "error", "message": "fixture-private-upstream"}, mode)
    wire += frame({**final, "samp_ix": 1}, mode)
    state.nai.frames = Frames([wire[i:i + chunk_size] for i in range(0, len(wire), chunk_size)])
    state.nai.content_type = "application/x-msgpack" if mode == "msgpack" else "text/event-stream"
    response = await post("/ai/generate-image-stream", image_body(stream=mode, n_samples=2))
    assert b"fixture-private" not in response.content
    assert state.nai.counts == [1] and state.db.charges[0][1]["images"] == 1
    if mode == "msgpack":
        events = unpack_frames(response.content)
    else:
        events = [json.loads(line[6:]) for line in response.content.splitlines() if line.startswith(b"data: ")]
    assert [item["event_type"] for item in events] == ["final", "error"]
    assert events[-1]["status_code"] == 502


@pytest.mark.parametrize("wire", [struct.pack(">I", 0), struct.pack(">I", MAX_EVENT_BYTES + 1),
                                  b"\0\0\0\x01\xc1", frame(["fixture-private"], "msgpack")])
def test_invalid_binary_frames_fail_safely(wire):
    tracker = ImageEventTracker(1, "msgpack")
    with pytest.raises(ImageStreamProtocolError):
        tracker.feed(wire)
    assert tracker.failed and not tracker.completed_images and not tracker._binary


def test_binary_eof_does_not_complete_truncated_final():
    wire = frame({"event_type": "final", "samp_ix": 0, "image": base64.b64decode(PNG)}, "msgpack")
    tracker = ImageEventTracker(1, "msgpack")
    tracker.feed(wire[:-1])
    tracker.finish()
    assert tracker.completed_images == 0 and not tracker._binary


@pytest.mark.asyncio
async def test_msgpack_http_rejection_is_json_and_bad_mode_never_dispatches(state):
    from app.nai import UpstreamError
    state.nai.error = UpstreamError(429, "上游限流")
    response = await post("/ai/generate-image-stream", image_body(stream="msgpack"))
    assert response.status_code == 429 and response.headers["content-type"] == "application/json"
    state.nai.calls.clear()
    response = await post("/ai/generate-image-stream", image_body(stream="unsupported"))
    assert response.status_code == 400 and not state.nai.calls


@pytest.mark.asyncio
async def test_nai_client_requests_selected_binary_format():
    async def handler(request):
        assert request.headers["accept"] == "application/x-msgpack"
        assert json.loads(request.content)["parameters"]["stream"] == "msgpack"
        return httpx.Response(200, content=b"", headers={"content-type": "application/x-msgpack"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        nai, _ = make_client(http=http)
        async with nai.image_stream("https://fixture.invalid/ai/generate-image-stream",
                                    image_body(stream="msgpack")):
            pass
