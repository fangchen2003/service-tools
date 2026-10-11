"""Admission, cancellation and memory bounds for real reference conversion."""
import asyncio
import base64
import copy
import hashlib
import io
import json
import threading

import anyio
import pytest
from PIL import Image

from app import image_compat, main
from test_client_reference_compatibility import UUID, reference_image
from test_generation_integration import state, image_body, post


def precise_body(count=1):
    body = image_body(precise=count)
    body["parameters"]["director_reference_images_cached"] = [
        {"cache_secret_key": UUID, "data": reference_image("JPEG")}
        for _ in range(count)]
    return body


def noisy_jpeg():
    pixels = hashlib.shake_256(b"reference-budget-fixture").digest(128 * 128 * 3)
    with Image.frombytes("RGB", (128, 128), pixels) as img, io.BytesIO() as output:
        img.save(output, format="JPEG", quality=80)
        return base64.b64encode(output.getvalue()).decode("ascii")


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("bad", ["array", "strength", "description", "cache-only", "mixed"])
async def test_invalid_structure_never_converts(state, monkeypatch, bad, streaming):
    def unexpected(*args):
        pytest.fail("Invalid reference structure reached Pillow")

    monkeypatch.setattr(main, "normalize_image_references", unexpected)
    body = precise_body()
    p = body["parameters"]
    if bad == "array":
        p["director_reference_images_cached"] = "wrong"
    elif bad == "strength":
        p["director_reference_strength_values"] = [2]
    elif bad == "description":
        p["director_reference_descriptions"] = []
    elif bad == "cache-only":
        del p["director_reference_images_cached"][0]["data"]
    else:
        p["reference_image_multiple"] = ["fixture"]
    response = await post("/ai/generate-image" + ("-stream" if streaming else ""), body)
    assert response.status_code == 400
    assert not state.nai.calls and not state.db.charges and not state.semaphores


@pytest.mark.asyncio
@pytest.mark.parametrize("same_key", [False, True])
async def test_conversion_obeys_global_and_key_admission(state, monkeypatch, same_key):
    state.settings.key_concurrency = 1
    # With two global slots the same-Key case specifically exercises Key admission.
    state.reference_conversion_sem = asyncio.Semaphore(2 if same_key else 1)
    entered = asyncio.Queue()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = main.normalize_image_references
    calls = []

    def blocked(*args):
        calls.append(args[1])
        loop.call_soon_threadsafe(entered.put_nowait, args[1])
        assert release.wait(3), "conversion test did not release worker"
        return original(*args)

    monkeypatch.setattr(main, "normalize_image_references", blocked)
    first = asyncio.create_task(post("/ai/generate-image", precise_body()))
    second = None
    try:
        await asyncio.wait_for(entered.get(), 2)
        second = asyncio.create_task(post("/ai/generate-image", precise_body(),
                                         token="fixture-1" if same_key else "fixture-2"))
        await asyncio.sleep(.05)
        assert len(calls) == 1 and not second.done()
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 3)
        assert [r.status_code for r in results] == [200, 200]
        assert len(calls) == len(state.nai.calls) == 2
    finally:
        release.set()
        await asyncio.gather(*[t for t in (first, second) if t is not None], return_exceptions=True)
    assert state.global_active == state.global_waiting == 0
    assert not state.reference_conversion_sem.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_kind", ["asyncio", "anyio"])
async def test_cancel_keeps_conversion_slot_until_worker_finishes(state, monkeypatch, cancel_kind):
    entered = asyncio.Queue()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = main.normalize_image_references
    calls = []

    def blocked(*args):
        calls.append(args[1])
        loop.call_soon_threadsafe(entered.put_nowait, args[1])
        assert release.wait(3), "conversion test did not release worker"
        return original(*args)

    monkeypatch.setattr(main, "normalize_image_references", blocked)
    scope = anyio.CancelScope()

    async def run():
        with scope:
            return await post("/ai/generate-image", precise_body())

    first = asyncio.create_task(run())
    second = None
    try:
        await asyncio.wait_for(entered.get(), 2)
        if cancel_kind == "asyncio":
            first.cancel()
        else:
            scope.cancel()
        second = asyncio.create_task(post("/ai/generate-image", precise_body(), token="fixture-2"))
        await asyncio.sleep(.05)
        assert not first.done() and state.reference_conversion_sem.locked()
        assert calls == ["fixture-1"] and not state.nai.calls
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 3)
        if cancel_kind == "asyncio":
            assert isinstance(results[0], asyncio.CancelledError)
        assert results[1].status_code == 200
        assert len(state.nai.calls) == len(state.db.charges) == 1
        assert state.db.charges[0][0] == 2
    finally:
        release.set()
        await asyncio.gather(*[t for t in (first, second) if t is not None], return_exceptions=True)
    assert not state.reference_conversion_sem.locked() and state.global_active == 0


@pytest.mark.asyncio
async def test_conversion_queue_timeout_releases_key_without_decoding(state, monkeypatch):
    state.settings.queue_timeout = .03
    await state.reference_conversion_sem.acquire()

    def unexpected(*args):
        pytest.fail("Timed-out request started conversion")

    monkeypatch.setattr(main, "normalize_image_references", unexpected)
    try:
        response = await post("/ai/generate-image", precise_body())
        assert response.status_code == 429
        assert not state.nai.calls and state.global_active == state.global_waiting == 0
        assert not state.semaphores[1].locked()
    finally:
        state.reference_conversion_sem.release()


def test_cumulative_expansion_stops_at_first_over_budget_image(monkeypatch):
    body = precise_body(6)
    data = noisy_jpeg()
    for item in body["parameters"]["director_reference_images_cached"]:
        item["data"] = data
    one_size = len(image_compat._precise_png(data, max_chars=1024 * 1024))
    metadata = copy.deepcopy(body)
    for item in metadata["parameters"]["director_reference_images_cached"]:
        item.update(data="", cache_secret_key="0" * 64)
    overhead = len(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode())
    limit = overhead + one_size * 2 + one_size // 2
    assert len(json.dumps(body, separators=(",", ":")).encode()) < limit
    started, completed = [], []
    original = image_compat._precise_png

    def traced(value, **kwargs):
        started.append(kwargs["max_chars"])
        result = original(value, **kwargs)
        completed.append(len(result))
        return result

    monkeypatch.setattr(image_compat, "_precise_png", traced)
    with pytest.raises(ValueError, match="请求体过大"):
        image_compat.normalize_image_references(body, "fixture-1", max_bytes=limit)
    assert len(started) == 3 and len(completed) == 2
    assert started[0] > started[1] > started[2]
    assert all(item["data"] == data for item in body["parameters"]["director_reference_images_cached"])


def test_png_writer_cannot_allocate_past_remaining_budget(monkeypatch):
    written = []
    original = image_compat._BoundedPNG.write

    def traced(self, data):
        result = original(self, data)
        written.append(self.tell())
        return result

    monkeypatch.setattr(image_compat._BoundedPNG, "write", traced)
    with pytest.raises(ValueError, match="请求体过大"):
        image_compat._precise_png(noisy_jpeg(), max_chars=256)
    assert written and max(written) <= 192


@pytest.mark.parametrize("raw", [False, True])
def test_size_budget_includes_unicode_metadata_and_cache_keys(raw):
    body = precise_body()
    body["input"] = "角色：\"森林\"\\夜晚" * 10
    if raw:
        p = body["parameters"]
        p["director_reference_images"] = [p.pop("director_reference_images_cached")[0]["data"]]
    output = image_compat.normalize_image_references(body, "fixture-1")
    exact = len(json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode())
    assert image_compat.normalize_image_references(body, "fixture-1", max_bytes=exact) == output
    with pytest.raises(ValueError, match="请求体过大"):
        image_compat.normalize_image_references(body, "fixture-1", max_bytes=exact - 1)


def test_oversized_metadata_rejects_before_first_conversion(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Oversized metadata reached Pillow")

    monkeypatch.setattr(image_compat, "_precise_png", unexpected)
    with pytest.raises(ValueError, match="请求体过大"):
        image_compat.normalize_image_references(precise_body(), "fixture-1", max_bytes=100)
