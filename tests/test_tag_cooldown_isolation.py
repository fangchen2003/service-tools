"""Tag autocomplete ignores local image cooldown, not upstream safety limits."""
import time
import uuid

import httpx
import pytest

from app import main
from app.image_queue import ImageQueue, QueueError
from test_tag_admission import state


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("prefix", ["", "/nai"])
async def test_tags_work_during_image_cooldown_without_clearing_it(state, method, prefix):
    state._image_blocked_until = time.time() + 60
    blocked_until = state._image_blocked_until
    state.settings.key_image_min_interval = 15
    state.settings.queue_timeout = .2
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                 base_url="http://fixture.invalid") as client:
        for _ in range(2):
            response = await client.request(method, prefix + "/ai/generate-image/suggest-tags",
                                            headers={"Authorization": "Bearer fixture-1"},
                                            **({"json": {}} if method == "POST" else {}))
            assert response.status_code == 200, response.text
    assert len(state.nai.calls) == 2
    assert state._image_blocked_until == blocked_until
    assert state.image_queue_capacity() == 0
    assert state.image_queue.snapshot()["total"] == 0
    assert not state._key_image_next_at
    assert not state._tag_active and state._tag_waiting == 0


@pytest.mark.asyncio
async def test_tags_can_use_idle_slots_while_images_wait_for_cooldown(state):
    state._image_blocked_until = time.time() + 60
    ticket = state.image_queue.create(2, str(uuid.uuid4()), "generate-image")
    assert ticket.state == "queued"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                 base_url="http://fixture.invalid") as client:
        response = await client.get("/ai/generate-image/suggest-tags",
                                    headers={"Authorization": "Bearer fixture-1"})
    assert response.status_code == 200, response.text
    assert len(state.nai.calls) == 1
    assert ticket.state == "queued"
    assert state.image_queue.snapshot()["queued"] == 1
    state.image_queue.cancel(ticket.ticket_id, ticket.key_id)


def test_uncooled_tags_still_hold_slots_and_yield_to_images_on_recovery():
    capacity = {"images": 0}
    queue = ImageQueue(lambda: capacity["images"], lambda: 1, lambda _key: False,
                       tag_capacity=lambda: 1)
    image = queue.create(2, str(uuid.uuid4()), "generate-image")
    tag = queue.try_tag(1)
    assert image.state == "queued" and tag.state == "active"
    with pytest.raises(QueueError) as caught:
        queue.try_tag(3)
    assert caught.value.status == 503
    capacity["images"] = 1
    assert queue.snapshot()["active"] == 1
    assert image.state == "queued"
    queue.finish(tag)
    assert image.state == "ready"
    with pytest.raises(QueueError):
        queue.try_tag(3)
    queue.cancel(image.ticket_id, image.key_id)
    assert queue.snapshot()["total"] == 0


@pytest.mark.asyncio
async def test_generation_is_still_rejected_during_image_cooldown(state):
    state._image_blocked_until = time.time() + 60
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                 base_url="http://fixture.invalid") as client:
        response = await client.post("/ai/generate-image", json={},
                                     headers={"Authorization": "Bearer fixture-1"})
    assert response.status_code == 429
    assert not state.nai.calls
    assert state.image_queue.snapshot()["total"] == 0
