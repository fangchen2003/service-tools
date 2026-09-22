"""Bounded SSE accounting for NovelAI image streams; never retain image results."""
from __future__ import annotations

import base64
import binascii
import json
import re


MAX_EVENT_BYTES = 32 * 1024 * 1024
MAX_STREAM_BYTES = 128 * 1024 * 1024
_LINE_END = re.compile(rb"\r\n?|\n")
_SAFE_ERROR = "上游图片事件流格式无效或超过大小限制"


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

    def __init__(self, expected_images: int):
        if type(expected_images) is not int or expected_images < 1:
            raise ValueError("expected_images must be a positive integer")
        self.expected_images = expected_images
        self.failed = False
        self._completed: set[int] = set()
        self._line = bytearray()
        self._data: list[bytes] = []
        self._event = b""
        self._event_bytes = 0
        self._total_bytes = 0
        self._skip_lf = False
        self._first_line = True
        self._closed = False

    @property
    def completed_images(self) -> int:
        return len(self._completed)

    def _reject(self) -> None:
        self.failed = True
        self.finish()
        raise ImageStreamProtocolError()

    def feed(self, chunk: bytes) -> None:
        if self._closed:
            self._reject()
        self._total_bytes += len(chunk)
        if self._total_bytes > MAX_STREAM_BYTES:
            self._reject()
        start = 0
        if self._skip_lf and chunk:
            start = int(chunk[0] == 10)
            self._skip_lf = False
        for match in _LINE_END.finditer(chunk, start):
            self._append(chunk[start:match.start()], match.end() - match.start())
            line = bytes(self._line)
            self._line.clear()
            self._accept_line(line)
            start = match.end()
            self._skip_lf = match.group() == b"\r" and start == len(chunk)
        self._append(chunk[start:])

    def _append(self, fragment: bytes, delimiter_bytes: int = 0) -> None:
        self._event_bytes += len(fragment) + delimiter_bytes
        if self._event_bytes > MAX_EVENT_BYTES:
            self._reject()
        self._line.extend(fragment)

    def _accept_line(self, line: bytes) -> None:
        if self._first_line:
            line = line.removeprefix(b"\xef\xbb\xbf")
            self._first_line = False
        if not line:
            data, event = self._data, self._event
            self._data, self._event, self._event_bytes = [], b"", 0
            if data:
                self._dispatch(b"\n".join(data), event)
            return
        if line.startswith(b":"):
            return
        name, separator, value = line.partition(b":")
        if separator and value.startswith(b" "):
            value = value[1:]
        if name == b"data":
            self._data.append(value)
        elif name == b"event":
            self._event = value

    def _dispatch(self, raw: bytes, event: bytes) -> None:
        if self.failed:
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                self._reject()
            event_name = event.decode("utf-8")
            event_type = payload.get("event_type", event_name)
            if not isinstance(event_type, str):
                self._reject()
            if event_name not in ("", "message", event_type):
                self._reject()
        except (ValueError, UnicodeError, RecursionError):
            self._reject()
        if event_type == "error":
            self.failed = True
            return
        if event_type != "final":
            return
        sample = payload.get("samp_ix")
        if type(sample) is not int or not 0 <= sample < self.expected_images:
            self._reject()
        image = payload.get("image")
        if not isinstance(image, str) or not image:
            self._reject()
        try:
            decoded = base64.b64decode(image, validate=True)
        except (binascii.Error, ValueError):
            self._reject()
        if not _image_envelope(decoded):
            self._reject()
        self._completed.add(sample)

    def finish(self) -> None:
        """Discard unframed data without counting it; already counted finals remain."""
        self._line.clear()
        self._data.clear()
        self._event = b""
        self._event_bytes = 0
        self._closed = True
