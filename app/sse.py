"""Bound complete SSE events before callers inspect or forward their data."""
import re


_LINE_END = re.compile(rb"\r\n?|\n")


class SSEProtocolError(ValueError):
    pass


class SSEDecoder:
    def __init__(self, event_limit: int, stream_limit: int):
        self.event_limit, self.stream_limit = event_limit, stream_limit
        self._line = bytearray()
        self._data = []
        self._event = b""
        self._event_bytes = self._total_bytes = 0
        self._skip_lf = False
        self._first_line = True

    def feed(self, chunk: bytes):
        self._total_bytes += len(chunk)
        if self._total_bytes > self.stream_limit:
            raise SSEProtocolError()
        start = 0
        if self._skip_lf and chunk:
            start = int(chunk[0] == 10)
            self._skip_lf = False
        for match in _LINE_END.finditer(chunk, start):
            self._append(chunk[start:match.start()], match.end() - match.start())
            line = bytes(self._line)
            self._line.clear()
            if self._first_line:
                line = line.removeprefix(b"\xef\xbb\xbf")
                self._first_line = False
            start = match.end()
            self._skip_lf = match.group() == b"\r" and start == len(chunk)
            if not line:
                data, event = self._data, self._event
                self._data, self._event, self._event_bytes = [], b"", 0
                if data:
                    yield b"\n".join(data), event
            elif not line.startswith(b":"):
                name, separator, value = line.partition(b":")
                if separator and value.startswith(b" "):
                    value = value[1:]
                if name == b"data":
                    self._data.append(value)
                elif name == b"event":
                    self._event = value
        self._append(chunk[start:])

    def _append(self, fragment: bytes, delimiter_bytes: int = 0):
        self._event_bytes += len(fragment) + delimiter_bytes
        if self._event_bytes > self.event_limit:
            raise SSEProtocolError()
        self._line.extend(fragment)

    def finish(self):
        # EOF does not dispatch a partially received event.
        self._line.clear()
        self._data.clear()
        self._event = b""
        self._event_bytes = 0


def encode_sse(data: bytes, event: bytes = b"") -> bytes:
    prefix = b"event: " + event + b"\n" if event else b""
    return prefix + b"\n".join(b"data: " + line for line in data.split(b"\n")) + b"\n\n"
