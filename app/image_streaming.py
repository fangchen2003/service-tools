"""ASGI streaming lifetime: queued work cancels, dispatched work settles safely."""

import asyncio
import json

import anyio
from starlette.responses import Response

from .nai import _wait_cleanup


class ImageStreamResponse(Response):
    media_type = "text/event-stream"

    def __init__(self, run):
        super().__init__(content=None, media_type=self.media_type)
        self.run = run
        self.started = False
        self.disconnected = asyncio.Event()
        self._send = None

    async def write(self, message):
        if self.disconnected.is_set():
            return
        try:
            # A slow/disconnected consumer must not block upstream settlement.
            await asyncio.wait_for(self._send(message), timeout=10)
        except (OSError, TimeoutError):
            self.disconnected.set()

    async def start(self, status=200, content_type="text/event-stream"):
        if self.started:
            return
        self.started = True
        await self.write({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", content_type.encode()), (b"cache-control", b"no-store"),
            (b"x-accel-buffering", b"no"),
        ]})

    async def chunk(self, data):
        await self.write({"type": "http.response.body", "body": data, "more_body": True})

    async def error(self, status, message):
        if not self.started:
            await self.start(status, "application/json")
            await self.chunk(json.dumps({"error": message, "message": message}, ensure_ascii=False).encode())
        else:
            data = json.dumps({"event_type": "error", "error": message,
                               "message": message, "status_code": status}, ensure_ascii=False)
            await self.chunk(("event: error\ndata: " + data + "\n\n").encode())

    async def __call__(self, scope, receive, send):
        self._send = send

        async def listen():
            while True:
                if (await receive())["type"] == "http.disconnect":
                    self.disconnected.set()
                    return

        worker = asyncio.create_task(self.run(self))
        listener = asyncio.create_task(listen())
        try:
            done, _ = await asyncio.wait((worker, listener), return_when=asyncio.FIRST_COMPLETED)
            if listener in done and not worker.done():
                worker.cancel()
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                if not self.disconnected.is_set():
                    raise
            if self.started:
                await self.write({"type": "http.response.body", "body": b"", "more_body": False})
        except asyncio.CancelledError:
            self.disconnected.set()
            worker.cancel()
            try:
                await _wait_cleanup(worker)
            except asyncio.CancelledError:
                pass
            raise
        finally:
            # Direct cancellation and Starlette/AnyIO scopes both await cleanup.
            with anyio.CancelScope(shield=True):
                if not worker.done():
                    worker.cancel()
                    try:
                        await _wait_cleanup(worker)
                    except asyncio.CancelledError:
                        pass
                listener.cancel()
                try:
                    await listener
                except asyncio.CancelledError:
                    pass
