"""Image stream completion is established by framed finals, never transport bytes."""
import base64
import json

import pytest

from app import image_events
from app.image_events import ImageEventTracker, ImageStreamProtocolError


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/"
    "iZk9HQAAAABJRU5ErkJggg=="
)


def frame(event_type="final", sample=0, image=PNG, *, newline=b"\n", **fields):
    payload = {"event_type": event_type, "samp_ix": sample, "gen_id": 123,
               "image": base64.b64encode(image).decode(), **fields}
    return (b"event: " + event_type.encode() + newline + b"data: "
            + json.dumps(payload).encode() + newline * 2)


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
@pytest.mark.parametrize("chunk_size", [1, 2, 7, 31, 4096])
def test_fragmented_progress_and_final(newline, chunk_size):
    tracker = ImageEventTracker(1)
    stream = b"\xef\xbb\xbf: heartbeat" + newline * 2
    stream += frame("intermediate", newline=newline) + frame(newline=newline)
    for pos in range(0, len(stream), chunk_size):
        tracker.feed(stream[pos:pos + chunk_size])
    tracker.finish()
    assert tracker.completed_images == 1 and not tracker.failed
    assert not tracker._sse._line and not tracker._sse._data


def test_multiple_out_of_order_finals_duplicate_sample_and_generation():
    tracker = ImageEventTracker(3)
    tracker.feed(frame(sample=2) + frame(sample=0))
    tracker.feed(frame(sample=0, gen_id=999) + frame(sample=2))
    assert tracker.completed_images == 2
    tracker.feed(frame(sample=1))
    assert tracker.completed_images == 3


def test_multiline_data_and_event_name_fallback():
    tracker = ImageEventTracker(1)
    data = (b"event: final\ndata: {\ndata: \"samp_ix\": 0,\n"
            b"data: \"image\": \"" + base64.b64encode(PNG) + b"\"}\n\n")
    tracker.feed(data)
    assert tracker.completed_images == 1


def test_json_event_type_without_sse_event_header():
    tracker = ImageEventTracker(1)
    tracker.feed(frame().split(b"\n", 1)[1])
    assert tracker.completed_images == 1


@pytest.mark.parametrize("tail", [b"", b"\n"])
def test_unterminated_final_is_not_dispatchable(tail):
    tracker = ImageEventTracker(1)
    tracker.feed(frame().rstrip(b"\n") + tail)
    tracker.finish()
    assert tracker.completed_images == 0 and not tracker.failed


def test_progress_and_success_aliases_are_not_final_images():
    tracker = ImageEventTracker(1)
    tracker.feed(frame("intermediate") + frame("done")
                 + b'data: {"success": true}\n\n')
    tracker.finish()
    assert tracker.completed_images == 0


def test_explicit_error_preserves_completed_samples():
    tracker = ImageEventTracker(2)
    tracker.feed(frame() + b'event: error\ndata: {"message":"private-error"}\n\n')
    assert tracker.failed and tracker.completed_images == 1


def test_error_in_json_body():
    tracker = ImageEventTracker(1)
    tracker.feed(b'data: {"event_type":"error","message":"private-error"}\n\n')
    assert tracker.failed and tracker.completed_images == 0


@pytest.mark.parametrize("same_chunk", [True, False])
def test_terminal_error_ignores_later_finals(same_chunk):
    tracker = ImageEventTracker(2)
    first = frame() + b'event: error\ndata: {"message":"private-error"}\n\n'
    if same_chunk:
        tracker.feed(first + frame(sample=1))
    else:
        tracker.feed(first)
        tracker.feed(frame(sample=1))
    assert tracker.failed and tracker.completed_images == 1


@pytest.mark.parametrize("sample", [None, -1, 1, True, False, 0.0, "0"])
def test_final_requires_valid_index(sample):
    tracker = ImageEventTracker(1)
    with pytest.raises(ImageStreamProtocolError):
        tracker.feed(frame(sample=sample))
    assert tracker.failed and tracker.completed_images == 0


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"null", b"\xff",
                                  b'{"event_type":true}', b'{"image":"secret"}'])
def test_malformed_or_untyped_final_has_safe_error(body):
    tracker = ImageEventTracker(1)
    with pytest.raises(ImageStreamProtocolError) as exc:
        tracker.feed(b"event: final\ndata: " + body + b"\n\n")
    assert str(exc.value) == "上游图片事件流格式无效或超过大小限制"
    assert tracker.failed and tracker.completed_images == 0


@pytest.mark.parametrize("image", ["", "not-base64%", "é", "data:image/png;base64,AAAA",
                                   None, 42, base64.b64encode(b"private-error").decode()])
def test_invalid_final_image_does_not_count(image):
    tracker = ImageEventTracker(1)
    payload = {"event_type": "final", "samp_ix": 0, "image": image}
    with pytest.raises(ImageStreamProtocolError):
        tracker.feed(b"data: " + json.dumps(payload).encode() + b"\n\n")
    assert tracker.completed_images == 0


@pytest.mark.parametrize("image", [PNG[:-1], PNG[:8], b"\xff\xd8\xff\x00",
                                   b"RIFF\xff\xff\xff\xffWEBPVP8L\x00\x00\x00\x00"])
def test_truncated_or_bad_envelope_is_not_complete(image):
    tracker = ImageEventTracker(1)
    with pytest.raises(ImageStreamProtocolError):
        tracker.feed(frame(image=image))
    assert tracker.completed_images == 0


@pytest.mark.parametrize("image", [PNG, b"\xff\xd8\xff\x00\xff\xd9",
                                   b"RIFF\x0c\x00\x00\x00WEBPVP8L\x00\x00\x00\x00"])
def test_supported_image_envelopes(image):
    tracker = ImageEventTracker(1)
    tracker.feed(frame(image=image))
    assert tracker.completed_images == 1


def test_event_type_disagreement_is_not_success():
    tracker = ImageEventTracker(1)
    with pytest.raises(ImageStreamProtocolError):
        tracker.feed(frame().replace(b"event: final", b"event: intermediate"))
    assert tracker.completed_images == 0


def test_per_event_limit_includes_fragmented_lines(monkeypatch):
    monkeypatch.setattr(image_events, "MAX_EVENT_BYTES", 24)
    tracker = ImageEventTracker(1)
    tracker.feed(b"data: " + b"x" * 18)
    with pytest.raises(ImageStreamProtocolError):
        tracker.feed(b"x")
    assert tracker.failed and not tracker._sse._line and not tracker._sse._data


def test_total_limit_bounds_unending_progress(monkeypatch):
    monkeypatch.setattr(image_events, "MAX_STREAM_BYTES", 16)
    tracker = ImageEventTracker(1)
    tracker.feed(b": beat\n\n" * 2)
    with pytest.raises(ImageStreamProtocolError):
        tracker.feed(b": beat\n\n")
    assert tracker.failed


def test_protocol_error_after_final_does_not_uncharge_it():
    tracker = ImageEventTracker(2)
    tracker.feed(frame())
    with pytest.raises(ImageStreamProtocolError):
        tracker.feed(b"data: private-invalid\n\n")
    assert tracker.failed and tracker.completed_images == 1


@pytest.mark.parametrize("count", [0, -1, True, 1.0, "1"])
def test_invalid_expected_count(count):
    with pytest.raises(ValueError):
        ImageEventTracker(count)
