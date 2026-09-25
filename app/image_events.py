"""Validate SSE/MessagePack image events before forwarding or counting finals."""
from __future__ import annotations

import base64
import binascii
import json
import msgpack

from .sse import SSEDecoder, encode_sse


MAX_EVENT_BYTES = 32 * 1024 * 1024
MAX_STREAM_BYTES = 128 * 1024 * 1024
_SAFE_ERROR = "上游图片事件流格式无效或超过大小限制"
STREAM_MEDIA_TYPES = {"sse": "text/event-stream", "msgpack": "application/x-msgpack"}


def encode_image_event(payload: dict, wire_format: str) -> bytes:
    if wire_format == "msgpack":
        data = msgpack.packb(payload, use_bin_type=True)
        return len(data).to_bytes(4, "big") + data
    return encode_sse(json.dumps(payload, ensure_ascii=False).encode(),
                      payload["event_type"].encode())


class ImageStreamProtocolError(ValueError):
    """A protocol failure whose message contains no upstream data."""

    def __init__(self):
        super().__init__(_SAFE_ERROR)


def _image_envelope(data: bytes) -> bool:
    """Check format and basic completeness, without decoding pixels/dependencies."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return (
            len(data) >= 45 and data[8:16] == b"\0\0\0\rIHDR"
            and int.from_bytes(data[16:20], "big") > 0
            and int.from_bytes(data[20:24], "big") > 0
            and data[-12:] == b"\0\0\0\0IEND\xaeB`\x82"
        )
    if data.startswith(b"\xff\xd8\xff"):
        return len(data) >= 5 and data.endswith(b"\xff\xd9")
    if data.startswith(b"RIFF"):
        return (
            len(data) >= 20 and int.from_bytes(data[4:8], "little") == len(data) - 8
            and data[8:12] == b"WEBP" and data[12:16] in (b"VP8 ", b"VP8L", b"VP8X")
        )
    return False


class ImageEventTracker:
    """Count unique complete ``final`` samples; progress and EOF never imply success.

    One event is buffered at a time. ``finish`` discards an unterminated SSE
    event, as required by SSE dispatch semantics. An explicit error marks the
    stream failed but preserves finals that arrived before it.
    """

    def __init__(self, expected_images: int, wire_format: str = "sse"):
        if type(expected_images) is not int or expected_images < 1:
            raise ValueError("expected_images must be a positive integer")
        self.expected_images = expected_images
        self.wire_format = wire_format
        self.failed = False
        self._completed: set[int] = set()
        self._sse = SSEDecoder(MAX_EVENT_BYTES, MAX_STREAM_BYTES)
        self._binary = bytearray()
        self._total_bytes = 0
        self._closed = False

    @property
    def completed_images(self) -> int:
        return len(self._completed)

    def _reject(self) -> None:
        self.failed = True
        self.finish()
        raise ImageStreamProtocolError()

    def feed(self, chunk: bytes) -> None:
        for _ in self.frames(chunk):
            pass

    def frames(self, chunk: bytes):
        """Yield checked events one at a time, so a later error keeps prior finals."""
        if self._closed:
            self._reject()
        if self.failed:
            return
        self._total_bytes += len(chunk)
        if self._total_bytes > MAX_STREAM_BYTES:
            self._reject()
        try:
            if self.wire_format == "msgpack":
                # Launcher uses a four-byte big-endian length before each map.
                # Bound the declared length before retaining the message body.
                start = 0
                while start < len(chunk):
                    target = 4
                    if len(self._binary) >= 4:
                        size = int.from_bytes(self._binary[:4], "big")
                        if not 0 < size <= MAX_EVENT_BYTES:
                            self._reject()
                        target += size
                    take = min(target - len(self._binary), len(chunk) - start)
                    self._binary.extend(chunk[start:start + take])
                    start += take
                    if len(self._binary) == target and target > 4:
                        wire = bytes(self._binary)
                        self._binary.clear()
                        payload = msgpack.unpackb(wire[4:], raw=False)
                        self._dispatch(payload)
                        if self.failed:
                            return
                        yield wire
                if len(self._binary) == 4 and not 0 < int.from_bytes(self._binary, "big") <= MAX_EVENT_BYTES:
                    self._reject()
            else:
                for raw, event in self._sse.feed(chunk):
                    self._dispatch(json.loads(raw.decode("utf-8")), event.decode("utf-8"))
                    if self.failed:
                        return
                    yield encode_sse(raw, event)
        except (ValueError, TypeError, UnicodeError, RecursionError, msgpack.UnpackException):
            self._reject()

    def _dispatch(self, payload, event_name: str = "") -> None:
        try:
            if not isinstance(payload, dict):
                self._reject()
            event_type = payload.get("event_type", event_name)
            if not isinstance(event_type, str):
                self._reject()
            if event_name not in ("", "message", event_type):
                self._reject()
        except (ValueError, UnicodeError, RecursionError):
            self._reject()
        if event_type == "error" or "error" in payload:
            self.failed = True
            return
        if event_type != "final":
            return
        sample = payload.get("samp_ix")
        if type(sample) is not int or not 0 <= sample < self.expected_images:
            self._reject()
        image = payload.get("image")
        if not isinstance(image, (str, bytes)) or not image:
            self._reject()
        try:
            decoded = base64.b64decode(image, validate=True) if isinstance(image, str) else image
        except (binascii.Error, ValueError):
            self._reject()
        if not _image_envelope(decoded):
            self._reject()
        self._completed.add(sample)

    def finish(self) -> None:
        """Discard unframed data without counting it; already counted finals remain."""
        self._sse.finish()
        self._binary.clear()
        self._closed = True
