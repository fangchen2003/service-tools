"""In-memory image admission; upstream slots and accounting remain authoritative."""
from __future__ import annotations

import asyncio
import heapq
import math
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass
from typing import Awaitable, Callable


OPERATIONS = {"generate-image", "upscale", "augment-image", "encode-vibe"}
WAIT_LEASE_SECONDS = 120
READY_LEASE_SECONDS = 120
TERMINAL_SECONDS = 15 * 60
POLL_AFTER = 3
MAX_PENDING_PER_KEY = 50
TICKET_HEADER = "X-NAI-Gate-Queue-Ticket"
REQUEST_HEADER = "X-NAI-Gate-Queue-Request"


class QueueError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def request_uuid(value) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError()
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise QueueError(400, "queue_invalid_request", "排队请求需要有效的 UUID") from None


async def check_disconnected(probe: Callable[[], Awaitable[bool]]) -> bool:
    """Preserve caller cancellation that the disconnect probe may handle."""
    task = asyncio.current_task()
    if task.cancelling():
        raise asyncio.CancelledError()
    disconnected = await probe()
    if task.cancelling():
        raise asyncio.CancelledError()
    return disconnected


@dataclass
class Ticket:
    ticket_id: str
    key_id: int
    request_id: str
    operation: str
    is_admin: bool
    state: str
    expires_at: float
    requires_anlas: bool = False


class ImageQueue:
    def __init__(self, capacity: Callable[[], int], key_limit: Callable[[], int],
                 key_busy: Callable[[int], bool], *, paid_capacity: Callable[[], int] | None = None,
                 clock=time.monotonic):
        self._capacity = capacity
        self._key_limit = key_limit
        self._key_busy = key_busy
        self._paid_capacity = paid_capacity or capacity
        self._clock = clock
        self._tickets: dict[str, Ticket] = {}
        self._requests: dict[tuple[int, str], str] = {}
        self._pending: dict[str, Ticket] = {}
        self._occupied: dict[str, Ticket] = {}
        self._pending_by_key: Counter[int] = Counter()
        self._leases: list[tuple[float, str]] = []
        self._terminal: deque[Ticket] = deque()
        self._changed = asyncio.Event()

    def _notify(self):
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def _end(self, ticket: Ticket, state: str):
        ticket.state = state
        ticket.expires_at = self._clock() + TERMINAL_SECONDS
        self._pending.pop(ticket.ticket_id)
        self._occupied.pop(ticket.ticket_id, None)
        self._pending_by_key[ticket.key_id] -= 1
        if not self._pending_by_key[ticket.key_id]:
            del self._pending_by_key[ticket.key_id]
        self._terminal.append(ticket)

    def _update(self):
        now = self._clock()
        changed = False
        while self._terminal and self._terminal[0].expires_at <= now:
            ticket = self._terminal.popleft()
            del self._tickets[ticket.ticket_id]
            self._requests.pop((ticket.key_id, ticket.request_id), None)
        while self._leases and self._leases[0][0] <= now:
            expiry, identity = heapq.heappop(self._leases)
            ticket = self._pending.get(identity)
            if ticket is not None and ticket.state in {"queued", "ready"} and ticket.expires_at == expiry:
                self._end(ticket, "expired")
                changed = True
        occupied = self._occupied
        capacity = max(0, self._capacity())
        paid_capacity = max(0, self._paid_capacity())
        paid_occupied = sum(ticket.requires_anlas for ticket in occupied.values())
        for ticket in self._pending.values():
            if len(occupied) >= capacity:
                break
            if ticket.state != "queued":
                continue
            if ticket.requires_anlas and paid_occupied >= paid_capacity:
                continue
            if not ticket.is_admin and (
                sum(other.key_id == ticket.key_id for other in occupied.values()) >= max(1, self._key_limit())
                or self._key_busy(ticket.key_id)
            ):
                continue
            ticket.state = "ready"
            expiry = min(ticket.expires_at, now + READY_LEASE_SECONDS)
            if expiry != ticket.expires_at:
                ticket.expires_at = expiry
                heapq.heappush(self._leases, (expiry, ticket.ticket_id))
            occupied[ticket.ticket_id] = ticket
            paid_occupied += ticket.requires_anlas
            changed = True
        if changed:
            self._notify()

    def _owned(self, ticket_id: str, key_id: int) -> Ticket:
        ticket = self._tickets.get(ticket_id)
        if ticket is None or ticket.key_id != key_id:
            raise QueueError(404, "queue_ticket_not_found", "排队票据不存在")
        return ticket

    def create(self, key_id: int, request_id: str, operation: str, *, is_admin=False, internal=False,
               requires_anlas=False) -> Ticket:
        request_id = request_uuid(request_id)
        if not isinstance(operation, str) or (operation not in OPERATIONS and not (internal and operation == "tags")):
            raise QueueError(400, "queue_invalid_request", "排队操作无效")
        if type(requires_anlas) is not bool:
            raise QueueError(400, "queue_invalid_request", "付费标记必须为布尔值")
        requires_anlas = requires_anlas or operation in {"upscale", "encode-vibe"}
        self._update()
        existing = self._requests.get((key_id, request_id))
        if existing is not None:
            ticket = self._tickets[existing]
            if ticket.operation != operation or ticket.requires_anlas != requires_anlas:
                raise QueueError(409, "queue_request_conflict", "同一排队请求不能更换操作或付费标记")
            return ticket
        if self._pending_by_key[key_id] >= MAX_PENDING_PER_KEY:
            raise QueueError(429, "queue_full", "本 Key 已有 50 个未完成排队任务，请先完成或取消")
        ticket = Ticket(str(uuid.uuid4()), key_id, request_id, operation, bool(is_admin), "queued",
                        self._clock() + WAIT_LEASE_SECONDS, requires_anlas)
        self._tickets[ticket.ticket_id] = ticket
        self._requests[(key_id, request_id)] = ticket.ticket_id
        self._pending[ticket.ticket_id] = ticket
        self._pending_by_key[key_id] += 1
        heapq.heappush(self._leases, (ticket.expires_at, ticket.ticket_id))
        self._update()
        self._notify()
        return ticket

    def view(self, ticket_id: str, key_id: int) -> dict:
        self._update()
        ticket = self._owned(ticket_id, key_id)
        position = 0
        if ticket.state == "queued":
            for other in self._pending.values():
                position += 1
                if other is ticket:
                    break
        return {"ticket_id": ticket.ticket_id, "request_id": ticket.request_id,
                "operation": ticket.operation, "state": ticket.state, "position": position,
                "expires_in": max(0, math.ceil(ticket.expires_at - self._clock()))
                if ticket.state in {"queued", "ready"} else 0, "poll_after": POLL_AFTER}

    def heartbeat(self, ticket_id: str, key_id: int) -> dict:
        ticket = self._owned(ticket_id, key_id)
        now = self._clock()
        if ticket.state == "queued" and ticket.expires_at > now:
            ticket.expires_at = now + WAIT_LEASE_SECONDS
            heapq.heappush(self._leases, (ticket.expires_at, ticket.ticket_id))
        return self.view(ticket_id, key_id)

    def cancel(self, ticket_id: str, key_id: int) -> dict:
        self._update()
        ticket = self._owned(ticket_id, key_id)
        if ticket.state == "active":
            raise QueueError(409, "queue_ticket_active", "任务已经开始提交，无法取消排队")
        if ticket.state in {"queued", "ready"}:
            self._end(ticket, "cancelled")
            self._notify()
        return self.view(ticket_id, key_id)

    def cancel_key(self, key_id: int):
        changed = False
        for ticket in list(self._pending.values()):
            if ticket.key_id == key_id and ticket.state in {"queued", "ready"}:
                self._end(ticket, "cancelled")
                changed = True
        if changed:
            self._notify()
            self._update()

    def claim(self, ticket_id: str, key_id: int, request_id: str, operation: str) -> Ticket:
        self._update()
        ticket = self._owned(ticket_id, key_id)
        if ticket.request_id != request_uuid(request_id) or ticket.operation != operation:
            raise QueueError(409, "queue_ticket_mismatch", "票据与本次请求或操作不一致")
        if ticket.state == "expired":
            raise QueueError(410, "queue_ticket_expired", "排队票据已过期，请重新排队")
        if ticket.state == "cancelled":
            raise QueueError(409, "queue_ticket_cancelled", "排队票据已取消")
        if ticket.state == "queued":
            raise QueueError(409, "queue_ticket_not_ready", "尚未轮到本次请求，请继续等待")
        if ticket.state != "ready":
            raise QueueError(409, "queue_ticket_used", "排队票据已使用，请勿重复提交")
        ticket.state = "active"
        ticket.expires_at = 0
        self._notify()
        return ticket

    def finish(self, ticket: Ticket):
        if ticket.state == "active":
            self._end(ticket, "finished")
            self._notify()
            self._update()

    async def wait_claim(self, ticket: Ticket, timeout: float, *,
                         disconnected: Callable[[], Awaitable[bool]] | None = None) -> Ticket:
        deadline = self._clock() + timeout
        try:
            while True:
                if disconnected is not None and await check_disconnected(disconnected):
                    raise QueueError(499, "queue_request_disconnected", "等待提交的图片请求已断开")
                self._update()
                if ticket.state != "queued" and disconnected is not None:
                    if await check_disconnected(disconnected):
                        raise QueueError(499, "queue_request_disconnected", "等待提交的图片请求已断开")
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise QueueError(429, "queue_timeout", "图片任务排队超时，请稍后再试")
                if ticket.state != "queued":
                    return self.claim(ticket.ticket_id, ticket.key_id, ticket.request_id, ticket.operation)
                ticket.expires_at = self._clock() + WAIT_LEASE_SECONDS
                heapq.heappush(self._leases, (ticket.expires_at, ticket.ticket_id))
                changed = self._changed
                try:
                    await asyncio.wait_for(changed.wait(), min(1, remaining))
                except TimeoutError:
                    pass
        except BaseException:
            if ticket.state in {"queued", "ready"}:
                self.cancel(ticket.ticket_id, ticket.key_id)
            raise

    def try_tag(self, key_id: int, *, is_admin=False) -> Ticket:
        self._update()
        occupied = self._occupied
        if (len(self._pending) > len(occupied)
                or len(occupied) >= max(0, self._capacity())
                or (not is_admin and (self._key_busy(key_id) or
                    sum(ticket.key_id == key_id for ticket in occupied.values()) >= max(1, self._key_limit())))):
            raise QueueError(503, "queue_unavailable", "图片队列正在使用名额，标签补全暂不可用")
        ticket = self.create(key_id, str(uuid.uuid4()), "tags", is_admin=is_admin, internal=True)
        return self.claim(ticket.ticket_id, key_id, ticket.request_id, "tags")

    def snapshot(self) -> dict:
        self._update()
        active = sum(ticket.state == "active" for ticket in self._occupied.values())
        counts = {"queued": len(self._pending) - len(self._occupied),
                  "ready": len(self._occupied) - active, "active": active}
        return {**counts, "total": sum(counts.values()), "capacity": max(0, self._capacity())}


class ImageAdmission:
    """One execution owns admission until its upstream/stream cleanup has finished."""
    def __init__(self, queue: ImageQueue, key: dict, operation: str, headers, timeout: float):
        self.queue, self.key, self.operation, self.timeout = queue, key, operation, timeout
        self.ticket = None
        self._closed = False
        identity, request_id = headers.get(TICKET_HEADER), headers.get(REQUEST_HEADER)
        if identity is not None or request_id is not None:
            if not identity or not request_id:
                raise QueueError(400, "queue_invalid_request", "执行请求必须同时提供排队票据和请求 UUID")
            self.ticket = queue.claim(identity, key["id"], request_id, operation)

    async def wait(self, *, disconnected=None, requires_anlas=False):
        if self.ticket is None:
            self.ticket = self.queue.create(self.key["id"], str(uuid.uuid4()), self.operation,
                                            is_admin=self.key["is_admin"], requires_anlas=requires_anlas)
            await self.queue.wait_claim(self.ticket, self.timeout, disconnected=disconnected)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.ticket is not None:
            if self.ticket.state == "active":
                self.queue.finish(self.ticket)
            elif self.ticket.state in {"queued", "ready"}:
                self.queue.cancel(self.ticket.ticket_id, self.ticket.key_id)
