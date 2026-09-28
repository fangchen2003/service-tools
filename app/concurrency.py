"""Small adjustable async limiter for per-upstream image requests."""

from __future__ import annotations

import asyncio
from collections import deque


class AdjustableLimiter:
    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self.active = 0
        self._waiters: deque[asyncio.Future] = deque()
        self._idle_waiters: list[asyncio.Future] = []

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *_exc):
        self.release()

    async def acquire(self) -> None:
        if self.active < self.limit and not self._waiters:
            self.active += 1
            return
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        except BaseException:
            if waiter.done() and not waiter.cancelled():
                self.release()
            else:
                waiter.cancel()
                self._wake()
            raise
        finally:
            try:
                self._waiters.remove(waiter)
            except ValueError:
                pass

    def release(self) -> None:
        if self.active < 1:
            raise RuntimeError("image limiter released without acquisition")
        self.active -= 1
        if self.active == 0:
            for waiter in self._idle_waiters:
                if not waiter.done():
                    waiter.set_result(None)
            self._idle_waiters.clear()
        self._wake()

    def resize(self, limit: int) -> None:
        self.limit = max(1, limit)
        self._wake()

    async def wait_idle(self) -> None:
        if self.active:
            waiter = asyncio.get_running_loop().create_future()
            self._idle_waiters.append(waiter)
            try:
                await waiter
            finally:
                if waiter in self._idle_waiters:
                    self._idle_waiters.remove(waiter)

    def _wake(self) -> None:
        while self.active < self.limit and self._waiters:
            waiter = self._waiters.popleft()
            if waiter.done():
                continue
            self.active += 1
            waiter.set_result(None)
