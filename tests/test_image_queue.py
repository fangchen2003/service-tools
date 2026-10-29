"""Image admission tickets, legacy coexistence and stream ownership; no real upstream."""
import asyncio
import json
import time
import uuid

import httpx
import pytest
from starlette.requests import Request

from app import main
from app.image_queue import ImageQueue, QueueError, REQUEST_HEADER, TICKET_HEADER
from app.image_streaming import ImageStreamResponse
from test_generation_integration import image_body, state, post
from test_image_stream_routes import Frames, StreamingNai, event
from test_image_tools import body as tool_body, zipped


def new(queue, key=1, operation="generate-image", request_id=None):
    return queue.create(key, request_id or str(uuid.uuid4()), operation)


def headers(ticket, token="fixture-1"):
    return {"Authorization": "Bearer " + token, TICKET_HEADER: ticket.ticket_id,
            REQUEST_HEADER: ticket.request_id}


def test_fifo_positions_key_capacity_and_cancellation():
    queue = ImageQueue(lambda: 2, lambda: 1, lambda _: False)
    first, same_key, other_key = new(queue), new(queue), new(queue, 2)
    assert [first.state, same_key.state, other_key.state] == ["ready", "queued", "ready"]
    assert queue.view(same_key.ticket_id, 1)["position"] == 2
    assert queue.snapshot() == {"queued": 1, "ready": 2, "active": 0, "total": 3, "capacity": 2}
    queue.claim(first.ticket_id, 1, first.request_id, first.operation)
    with pytest.raises(QueueError, match="已经开始") as error:
        queue.cancel(first.ticket_id, 1)
    assert error.value.code == "queue_ticket_active"
    queue.cancel(other_key.ticket_id, 2)
    assert same_key.state == "queued"  # A second Key cannot enlarge the first Key's allowance.
    queue.finish(first)
    assert queue.view(same_key.ticket_id, 1)["state"] == "ready"
    assert queue.snapshot()["total"] == 1


def test_paid_waiters_leave_free_only_upstream_capacity_available():
    paid_capacity = [1]
    queue = ImageQueue(lambda: 2, lambda: 1, lambda _: False,
                       paid_capacity=lambda: paid_capacity[0])
    first = queue.create(1, str(uuid.uuid4()), "generate-image", requires_anlas=True)
    second = queue.create(2, str(uuid.uuid4()), "generate-image", requires_anlas=True)
    free = new(queue, 3)
    assert [first.state, second.state, free.state] == ["ready", "queued", "ready"]
    queue.claim(first.ticket_id, 1, first.request_id, first.operation)
    queue.finish(first)
    assert second.state == "ready" and free.state == "ready"
    paid_capacity[0] = 2
    third = queue.create(4, str(uuid.uuid4()), "encode-vibe")
    queue.cancel(free.ticket_id, 3)
    assert third.state == "ready" and third.requires_anlas


def test_key_backlog_bound_preserves_idempotence_and_releases_on_cancel_and_expiry():
    now = [0.0]
    queue = ImageQueue(lambda: 0, lambda: 1, lambda _: False, clock=lambda: now[0])
    tickets = [new(queue) for _ in range(50)]
    assert new(queue, request_id=tickets[0].request_id) is tickets[0]
    with pytest.raises(QueueError) as error:
        new(queue)
    assert error.value.code == "queue_full"
    assert new(queue, 2).state == "queued"
    queue.cancel(tickets[0].ticket_id, 1)
    assert new(queue).state == "queued"
    now[0] = 121
    assert queue.snapshot()["total"] == 0
    assert new(queue, request_id=tickets[1].request_id) is tickets[1]
    assert new(queue).state == "queued"
    now[0] = 1022
    assert new(queue, request_id=tickets[1].request_id) is not tickets[1]


def test_terminal_history_does_not_take_queue_capacity_or_break_renewed_leases():
    now = [0.0]
    queue = ImageQueue(lambda: 1, lambda: 1, lambda _: False, clock=lambda: now[0])
    finished = []
    for _ in range(1000):
        ticket = new(queue)
        queue.claim(ticket.ticket_id, 1, ticket.request_id, ticket.operation)
        queue.finish(ticket)
        finished.append(ticket)
    active, waiting = new(queue), new(queue, 2)
    now[0] = 100
    queue.heartbeat(waiting.ticket_id, 2)
    now[0] = 121
    assert queue.view(waiting.ticket_id, 2)["state"] == "ready"
    assert active.state == "expired" and queue.snapshot()["total"] == 1
    assert new(queue, request_id=finished[0].request_id) is finished[0]
    now[0] = 901
    assert new(queue, request_id=finished[0].request_id) is not finished[0]


def test_fifty_keys_keep_positions_and_cancelled_tickets_never_run():
    queue = ImageQueue(lambda: 1, lambda: 1, lambda _: False)
    tickets = [new(queue, key) for key in range(1, 51)]
    assert queue.view(tickets[-1].ticket_id, 50)["position"] == 50
    for index in (1, 24):
        queue.cancel(tickets[index].ticket_id, index + 1)
    assert queue.view(tickets[-1].ticket_id, 50)["position"] == 48
    executed = []
    for key, ticket in enumerate(tickets, 1):
        if ticket.state == "cancelled":
            continue
        assert queue.view(ticket.ticket_id, key)["state"] == "ready"
        queue.claim(ticket.ticket_id, key, ticket.request_id, ticket.operation)
        executed.append(key)
        queue.finish(ticket)
    assert executed == [key for key in range(1, 51) if key not in (2, 25)]
    assert queue.snapshot() == {"queued": 0, "ready": 0, "active": 0, "total": 0, "capacity": 1}


def test_heartbeat_only_extends_queued_and_terminals_prevent_replay():
    now, capacity = [0.0], [0]
    queue = ImageQueue(lambda: capacity[0], lambda: 1, lambda _: False, clock=lambda: now[0])
    ticket = new(queue)
    now[0] = 100
    assert queue.view(ticket.ticket_id, 1)["expires_in"] == 20
    assert queue.heartbeat(ticket.ticket_id, 1)["expires_in"] == 120
    capacity[0] = 1
    assert queue.view(ticket.ticket_id, 1)["state"] == "ready"
    now[0] = 200
    assert queue.heartbeat(ticket.ticket_id, 1)["expires_in"] == 20
    now[0] = 221
    assert queue.view(ticket.ticket_id, 1)["state"] == "expired"
    assert new(queue, request_id=ticket.request_id) is ticket
    with pytest.raises(QueueError) as error:
        queue.claim(ticket.ticket_id, 1, ticket.request_id, ticket.operation)
    assert error.value.code == "queue_ticket_expired"
    now[0] += 901
    assert new(queue, request_id=ticket.request_id).ticket_id != ticket.ticket_id


def test_ready_transition_does_not_extend_a_lost_waiters_lease():
    now, capacity = [0.0], [0]
    queue = ImageQueue(lambda: capacity[0], lambda: 1, lambda _: False, clock=lambda: now[0])
    ticket = new(queue)
    now[0], capacity[0] = 119, 1
    ready = queue.view(ticket.ticket_id, 1)
    assert ready["state"] == "ready" and ready["expires_in"] == 1
    now[0] = 121
    assert queue.view(ticket.ticket_id, 1)["state"] == "expired"
    assert queue.snapshot()["total"] == 0


def test_queued_heartbeat_renews_before_becoming_ready_without_reviving_expiry():
    now, capacity = [0.0], [0]
    queue = ImageQueue(lambda: capacity[0], lambda: 1, lambda _: False, clock=lambda: now[0])
    live, lost = new(queue), new(queue, 2)
    now[0], capacity[0] = 119, 1
    ready = queue.heartbeat(live.ticket_id, 1)
    assert ready["state"] == "ready" and ready["expires_in"] == 120
    now[0] = 121
    assert queue.heartbeat(live.ticket_id, 1)["expires_in"] == 118
    assert queue.heartbeat(lost.ticket_id, 2)["state"] == "expired"
    now[0] = 240
    assert queue.view(live.ticket_id, 1)["state"] == "expired"


def test_active_ticket_does_not_expire_and_is_claimed_once():
    now = [0.0]
    queue = ImageQueue(lambda: 1, lambda: 1, lambda _: False, clock=lambda: now[0])
    ticket = new(queue)
    queue.claim(ticket.ticket_id, 1, ticket.request_id, ticket.operation)
    now[0] = 10000
    assert queue.heartbeat(ticket.ticket_id, 1)["state"] == "active"
    for stage in ("active", "finished"):
        with pytest.raises(QueueError) as error:
            queue.claim(ticket.ticket_id, 1, ticket.request_id, ticket.operation)
        assert error.value.code == "queue_ticket_used"
        queue.finish(ticket)
    assert queue.snapshot()["total"] == 0


def test_capacity_changes_and_busy_key_do_not_block_eligible_peers():
    capacity, busy = [0], {1}
    queue = ImageQueue(lambda: capacity[0], lambda: 1, lambda key: key in busy)
    first, second, third = new(queue), new(queue, 2), new(queue, 3)
    assert queue.snapshot()["queued"] == 3
    capacity[0] = 1
    assert queue.view(second.ticket_id, 2)["state"] == "ready"
    assert first.state == third.state == "queued"
    capacity[0] = 2
    assert queue.view(third.ticket_id, 3)["state"] == "ready"
    busy.clear()
    queue.cancel(second.ticket_id, 2)
    assert queue.view(first.ticket_id, 1)["state"] == "ready"
    assert queue.snapshot()["ready"] == 2


@pytest.mark.asyncio
async def test_api_auth_idempotence_ownership_and_read_only_state(state):
    before = state.rpm_hits, list(state.db.charges), list(state.key_slots)
    request_id = str(uuid.uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        query = {"request_id": request_id, "operation": "generate-image"}
        assert (await client.post("/v1/image/queue", json=query)).status_code == 401
        auth = {"Authorization": "Bearer fixture-1"}
        first = await client.post("/v1/image/queue", json=query, headers=auth)
        assert first.status_code == 200
        assert set(first.json()) == {"ticket_id", "request_id", "operation", "state", "position", "expires_in", "poll_after"}
        assert (await client.post("/v1/image/queue", json=query, headers=auth)).json() == first.json()
        identity = first.json()["ticket_id"]
        conflict = await client.post("/v1/image/queue", json={**query, "operation": "upscale"}, headers=auth)
        assert conflict.status_code == 409 and conflict.json()["code"] == "queue_request_conflict"
        foreign = await client.get(f"/v1/image/queue/{identity}", headers={"Authorization": "Bearer fixture-2"})
        assert foreign.status_code == 404
        invalid = await client.post("/v1/image/queue", json={**query, "prompt": "private"}, headers=auth)
        assert invalid.status_code == 400
        cancelled = await client.delete(f"/v1/image/queue/{identity}", headers=auth)
        assert cancelled.json()["state"] == "cancelled"
        assert (await client.get(f"/v1/image/queue/{identity}", headers=auth)).json()["state"] == "cancelled"
    assert (state.rpm_hits, state.db.charges, state.key_slots) == before
    assert not state.nai.calls


@pytest.mark.asyncio
async def test_queue_api_uses_paid_hint_without_bypassing_conflicts_or_input_validation(state):
    state.image_queue = ImageQueue(lambda: 2, lambda: 1, lambda _: False, paid_capacity=lambda: 1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        query = dict(request_id=str(uuid.uuid4()), operation="generate-image", requires_anlas=True)
        auth = {"Authorization": "Bearer fixture-1"}
        assert (await client.post("/v1/image/queue", headers=auth, json=query)).json()["state"] == "ready"
        other = {"Authorization": "Bearer fixture-2"}
        queued = await client.post("/v1/image/queue", headers=other,
                                   json={**query, "request_id": str(uuid.uuid4())})
        assert queued.json()["state"] == "queued"
        conflict = await client.post("/v1/image/queue", headers=auth, json={**query, "requires_anlas": False})
        assert conflict.status_code == 409 and conflict.json()["code"] == "queue_request_conflict"
        for value in (1, "true", None):
            invalid = await client.post("/v1/image/queue", headers=auth, json={**query, "requires_anlas": value})
            assert invalid.status_code == 400 and invalid.json()["code"] == "queue_invalid_request"
    assert not state.nai.calls and not state.db.charges


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["not_ready", "wrong_key", "wrong_operation", "wrong_request", "cancelled", "used", "expired"])
async def test_ticket_rejection_never_reads_image_body(state, reason):
    now = [0.0]
    state.image_queue = ImageQueue(lambda: 1, lambda: 1, lambda _: False, clock=lambda: now[0])
    if reason == "not_ready":
        new(state.image_queue, 2)
    ticket = new(state.image_queue)
    auth = headers(ticket, "fixture-2" if reason == "wrong_key" else "fixture-1")
    if reason == "wrong_operation":
        ticket.operation = "upscale"
    elif reason == "wrong_request":
        auth[REQUEST_HEADER] = str(uuid.uuid4())
    elif reason == "cancelled":
        state.image_queue.cancel(ticket.ticket_id, 1)
    elif reason == "used":
        state.image_queue.claim(ticket.ticket_id, 1, ticket.request_id, ticket.operation)
    elif reason == "expired":
        now[0] = 121
    async def receive():
        raise AssertionError("Rejected ticket consumed image bytes")
    request = Request({"type": "http", "method": "POST", "path": "/ai/generate-image",
                       "headers": [(k.lower().encode(), v.encode()) for k, v in auth.items()]}, receive)
    with pytest.raises(QueueError) as error:
        await main.generate_image(request)
    expected = {"wrong_key": "not_found", "wrong_operation": "mismatch", "wrong_request": "mismatch"}.get(reason, reason)
    assert error.value.code == "queue_ticket_" + expected
    assert not state.nai.calls and state.rpm_hits == 0


@pytest.mark.asyncio
async def test_legacy_request_waits_behind_ticket_before_key_and_budget(state):
    state.image_queue = ImageQueue(lambda: 1, lambda: 1, lambda _: False)
    ticket = new(state.image_queue)
    waiting = asyncio.create_task(post("/ai/generate-image", image_body(), token="fixture-2"))
    async with asyncio.timeout(1):
        while state.image_queue.snapshot()["queued"] != 1:
            await asyncio.sleep(.001)
    assert not state.nai.calls and not state.semaphores and not state.key_slots
    state.image_queue.cancel(ticket.ticket_id, 1)
    assert (await waiting).status_code == 200
    assert len(state.nai.calls) == 1 and state.image_queue.snapshot()["total"] == 0


@pytest.mark.asyncio
async def test_legacy_wait_timeout_does_not_take_key_or_budget(state):
    state.image_queue = ImageQueue(lambda: 1, lambda: 1, lambda _: False)
    state.settings.queue_timeout = .01
    new(state.image_queue)
    response = await post("/ai/generate-image", image_body(), token="fixture-2")
    assert response.status_code == 429 and response.json()["code"] == "queue_timeout"
    assert not state.nai.calls and not state.key_slots and not state.semaphores
    assert state.image_queue.snapshot()["queued"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["disabled", "expired"])
async def test_invalid_key_releases_unclaimed_tickets_but_keeps_active(state, invalid):
    state.image_queue = ImageQueue(lambda: 2, lambda: 2, lambda _: False)
    active = new(state.image_queue)
    state.image_queue.claim(active.ticket_id, 1, active.request_id, active.operation)
    ready, waiting, other = new(state.image_queue), new(state.image_queue), new(state.image_queue, 2)
    assert ready.state == "ready" and waiting.state == other.state == "queued"
    if invalid == "disabled":
        state.db.keys["fixture-1"]["enabled"] = False
    else:
        state.db.keys["fixture-1"]["expires_at"] = time.time() - 1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.delete(f"/v1/image/queue/{ready.ticket_id}", headers={"Authorization": "Bearer fixture-1"})
    assert response.status_code == 403
    assert active.state == "active"
    assert ready.state == waiting.state == "cancelled"
    assert state.image_queue.view(other.ticket_id, 2)["state"] == "ready"
    assert not state.nai.calls and not state.db.charges


@pytest.mark.asyncio
async def test_disconnected_legacy_nonstream_waiter_never_claims_or_dispatches(state):
    state.image_queue = ImageQueue(lambda: 1, lambda: 1, lambda _: False)
    blocking = new(state.image_queue)
    disconnected = asyncio.Event()
    raw = json.dumps(image_body()).encode()
    reads, sent = [], []
    async def receive():
        if not reads:
            reads.append("body")
            return {"type": "http.request", "body": raw, "more_body": False}
        await disconnected.wait()
        reads.append("disconnect")
        return {"type": "http.disconnect"}
    async def send(message):
        sent.append(message)
    scope = {"type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": "/ai/generate-image", "query_string": b"",
             "headers": [(b"authorization", b"Bearer fixture-2"), (b"content-type", b"application/json")]}
    task = asyncio.create_task(main.app(scope, receive, send))
    async with asyncio.timeout(1):
        while state.image_queue.snapshot()["queued"] != 1:
            await asyncio.sleep(.001)
    assert reads == ["body"]
    disconnected.set()
    state.image_queue.cancel(blocking.ticket_id, 1)
    await asyncio.wait_for(task, 2)
    assert not state.nai.calls and not state.db.charges and not state.key_slots
    assert state.image_queue.snapshot()["total"] == 0
    assert next(message["status"] for message in sent if message["type"] == "http.response.start") == 499


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_probe", [1, 2])
@pytest.mark.parametrize("paid", [False, True])
async def test_legacy_cancel_during_disconnect_probe_never_claims_or_dispatches(state, cancel_probe, paid):
    state.db.keys["fixture-1"]["is_admin"] = paid
    raw = json.dumps(image_body(precise=int(paid))).encode()
    body_read, probes = False, 0

    async def receive():
        nonlocal body_read, probes
        if not body_read:
            body_read = True
            return {"type": "http.request", "body": raw, "more_body": False}
        probes += 1
        if probes == cancel_probe:
            asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
            await asyncio.Event().wait()
        return {"type": "http.request", "body": b"", "more_body": False}

    incoming = Request({"type": "http", "method": "POST", "path": "/ai/generate-image",
                        "headers": [(b"authorization", b"Bearer fixture-1")]}, receive)
    task = asyncio.create_task(main.generate_image(incoming))
    try:
        done, _ = await asyncio.wait({task}, timeout=1)
        assert task in done
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not state.nai.calls and not state.db.charges
        assert not state.key_slots and not state.semaphores
        assert state.image_queue.snapshot()["total"] == 0
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("late_probe", [1, 2])
async def test_legacy_wait_rejects_capacity_available_after_deadline(late_probe):
    now, capacity, probes = [0.0], [0 if late_probe == 1 else 1], 0
    queue = ImageQueue(lambda: capacity[0], lambda: 1, lambda _: False, clock=lambda: now[0])
    ticket = new(queue)

    async def disconnected():
        nonlocal probes
        probes += 1
        if probes == late_probe:
            now[0], capacity[0] = 2.0, 1
        return False

    with pytest.raises(QueueError) as caught:
        await queue.wait_claim(ticket, 1, disconnected=disconnected)
    assert caught.value.code == "queue_timeout"
    assert ticket.state == "cancelled" and queue.snapshot()["total"] == 0


@pytest.mark.asyncio
async def test_claimed_upload_timeout_releases_admission(state, monkeypatch):
    ticket = new(state.image_queue)
    timeout = asyncio.timeout
    monkeypatch.setattr(main.asyncio, "timeout", lambda seconds: timeout(.01 if seconds == 30 else seconds))
    async def receive():
        await asyncio.Event().wait()
    request = Request({"type": "http", "method": "POST", "path": "/ai/generate-image",
                       "headers": [(k.lower().encode(), v.encode()) for k, v in headers(ticket).items()]}, receive)
    with pytest.raises(QueueError) as error:
        await main.generate_image(request)
    assert error.value.code == "queue_upload_timeout" and error.value.status == 408
    assert ticket.state == "finished" and state.image_queue.snapshot()["total"] == 0
    assert not state.nai.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/ai/generate-image", "/ai/generate-image-stream"])
async def test_queue_ticket_does_not_bypass_existing_cost_limit(state, path):
    state.db.keys["fixture-1"]["is_admin"] = True
    ticket = new(state.image_queue)
    body = {**image_body(precise=1), "cost_limit": {"anlas": 0, "v5": 0}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.post(path, json=body, headers=headers(ticket))
    assert response.status_code == 409
    assert not state.nai.calls and not state.db.charges
    assert ticket.state == "finished" and state.image_queue.snapshot()["total"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,path,body", [
    ("generate-image", "/ai/generate-image", image_body()),
    ("generate-image", "/nai/ai/generate-image", image_body()),
    ("upscale", "/ai/upscale", tool_body()),
    ("augment-image", "/ai/augment-image", tool_body("lineart")),
    ("encode-vibe", "/ai/encode-vibe", {**tool_body(), "model": "nai-diffusion-4-5-full", "informationExtracted": 1}),
])
async def test_ticket_execution_finishes_once_without_own_deadlock(state, operation, path, body):
    if operation == "upscale":
        state.db.keys["fixture-1"]["is_admin"] = True
    state.image_queue = ImageQueue(lambda: 1, lambda: 1, lambda _: False)
    ticket = new(state.image_queue, operation=operation)
    state.nai.content = zipped() if operation in {"upscale", "augment-image"} else state.nai.content
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await asyncio.wait_for(client.post(path, json=body, headers=headers(ticket)), 1)
        assert response.status_code == 200, response.text
        repeated = await client.post(path, json=body, headers=headers(ticket))
        assert repeated.status_code == 409 and repeated.json()["code"] == "queue_ticket_used"
    assert ticket.state == "finished" and len(state.nai.calls) == 1
    assert state.image_queue.snapshot()["total"] == 0


@pytest.mark.asyncio
async def test_tag_lookup_cannot_bypass_waiting_image_tickets(state):
    state.image_queue = ImageQueue(lambda: 2, lambda: 1, lambda _: False)
    new(state.image_queue)
    new(state.image_queue)
    response = await post("/ai/generate-image/suggest-tags", {"prompt": "blue", "model": "nai-diffusion-4-5-full"}, token="fixture-2")
    assert response.status_code == 503 and response.json()["code"] == "queue_unavailable"
    assert not state.nai.calls


@pytest.mark.asyncio
async def test_stream_ticket_held_until_worker_and_accounting_finish(state):
    state.image_queue = ImageQueue(lambda: 1, lambda: 1, lambda _: False)
    state.nai = StreamingNai()
    release = asyncio.Event()
    state.nai.frames = Frames([event("intermediate"), release, event()])
    ticket = new(state.image_queue)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        running = asyncio.create_task(client.post("/ai/generate-image-stream", json=image_body(), headers=headers(ticket)))
        await state.nai.entered.wait()
        assert ticket.state == "active"
        next_ticket = new(state.image_queue, 2)
        assert next_ticket.state == "queued"
        release.set()
        assert (await running).status_code == 200
    assert ticket.state == "finished" and next_ticket.state == "ready"
    assert len(state.db.charges) == 1


@pytest.mark.asyncio
async def test_stream_cancellation_before_worker_start_releases_ticket(monkeypatch):
    queue = ImageQueue(lambda: 1, lambda: 1, lambda _: False)
    ticket = new(queue)
    queue.claim(ticket.ticket_id, 1, ticket.request_id, ticket.operation)
    def create(coro):
        task = original_create(coro)
        if coro.cr_code.co_name == "worker":
            task.cancel()
        return task
    async def worker(_):
        raise AssertionError("Cancelled worker started")
    async def receive():
        await asyncio.Event().wait()
    async def send(_):
        pass
    original_create = asyncio.create_task
    monkeypatch.setattr(asyncio, "create_task", create)
    response = ImageStreamResponse(worker, on_close=lambda: queue.finish(ticket))
    with pytest.raises(asyncio.CancelledError):
        await response({"type": "http"}, receive, send)
    assert queue.snapshot()["total"] == 0 and ticket.state == "finished"
