"""Generation route contracts; all accounts, ledgers and upstreams are fakes."""
import asyncio
import base64
import copy
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx
import pytest
from starlette.requests import Request

from app import main
from app.nai import UpstreamError
from app.state import GateState
from test_nai_integration import PNG as PNG_BYTES


PNG = base64.b64encode(PNG_BYTES).decode()


def image_body(*, precise=0, **parameters):
    params = dict(width=1024, height=1024, steps=28, n_samples=1,
                  sm=False, sm_dyn=False)
    if precise:
        params.update(
            director_reference_images_cached=[{"cache_secret_key": "a" * 64, "data": PNG}] * precise,
            director_reference_descriptions=[{"caption": {"base_caption": "character", "char_captions": []}, "legacy_uc": False}] * precise,
            director_reference_information_extracted=[1] * precise,
            director_reference_strength_values=[0.8] * precise,
            director_reference_secondary_strength_values=[0] * precise,
        )
    params.update(parameters)
    return dict(input="fixture", model="nai-diffusion-4-5-full",
                action="generate", parameters=params)


def encoding_body():
    return dict(image=PNG, model="nai-diffusion-4-5-full", informationExtracted=0.7)


class FakeDB:
    def __init__(self):
        base = dict(name="fixture", enabled=True, expires_at=0, is_admin=False,
                    exclude_global_v5=False, allow_anlas=True, daily_images=100,
                    daily_anlas=100,
                    monthly_anlas=1000, daily_v5=50, image_model_scope="all",
                    allow_img2img=True, rpm=3)
        self.keys = {f"fixture-{i}": dict(base, id=i) for i in (1, 2)}
        self.charges = []
        self.logs = []
        self.accounting_entered = asyncio.Event()
        self.accounting_release = None

    async def get_key_by_token(self, token):
        return self.keys.get(token)

    async def touch_key(self, *_):
        pass

    async def get_counter(self, key_id, *_):
        values = [charge for owner, charge in self.charges if owner == key_id]
        return {name: sum(row.get(name, 0) for row in values)
                for name in ("anlas", "v5", "images", "legacy_free_images")}

    async def get_setting(self, _name, default):
        return default

    async def month_anlas(self, key_id, *_):
        return sum(row["anlas"] for owner, row in self.charges if owner == key_id)

    async def month_anlas_all(self, *_):
        return sum(row["anlas"] for _, row in self.charges)

    async def day_v5_total(self, *_):
        return sum(row["v5"] for _, row in self.charges)

    async def add_log(self, *args, **kwargs):
        self.logs.append((args, kwargs))

    async def bump_counters(self, key_id, _day, **kwargs):
        self.accounting_entered.set()
        if self.accounting_release:
            await self.accounting_release.wait()
        self.charges.append((key_id, kwargs))


class FakeNai:
    image_host = "https://fixture.invalid"

    def __init__(self):
        self.calls = []
        self.status = 200
        self.content = PNG_BYTES
        self.content_type = "application/octet-stream"
        self.entered = asyncio.Event()
        self.release = None
        self.error = None

    async def request(self, method, url, body=None, **kwargs):
        self.calls.append((method, url, copy.deepcopy(body), kwargs))
        self.entered.set()
        if self.release:
            await self.release.wait()
        if self.error:
            if self.error.status == 429:
                await kwargs["on_rate_limited"](30)
            raise self.error
        return httpx.Response(self.status, content=self.content,
                              headers={"content-type": self.content_type})


class FakeState:
    try_tag_request = GateState.try_tag_request
    finish_tag_request = GateState.finish_tag_request

    def __init__(self):
        self.db = FakeDB()
        self.nai = FakeNai()
        self.settings = SimpleNamespace(
            queue_timeout=1, key_concurrency=2, global_concurrency=3, key_image_min_interval=0,
            global_daily_v5=150, global_monthly_anlas=10000,
            safe_clamp=True, allow_img2img=True, max_pixels=1048576,
            max_steps=28, image_429_cooldown_seconds=60,
        )
        self.image_budget_lock = asyncio.Lock()
        self.global_sem = asyncio.Semaphore(3)
        self.semaphores = {}
        self.global_active = self.global_waiting = self.rpm_hits = 0
        self.key_slots = []
        self.cooldowns = []
        self._tag_active = set()
        self._tag_next_at = {}

    def key_sem(self, key_id, capacity):
        return self.semaphores.setdefault(key_id, asyncio.Semaphore(capacity))

    async def hit_rpm(self, *_):
        self.rpm_hits += 1
        return True

    async def wait_for_key_image_slot(self, key_id):
        self.key_slots.append(key_id)

    def image_cooldown_remaining(self):
        return 0

    async def block_image_generation(self, seconds):
        self.cooldowns.append(seconds)
        return seconds

    def day(self):
        return "2026-09-22"

    def month(self):
        return "2026-09"


@pytest.fixture
def state(monkeypatch):
    value = FakeState()
    monkeypatch.setattr(main, "STATE", value)
    return value


def request(body, token="fixture-1", *, method="POST", query=b""):
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    return Request({"type": "http", "method": method, "path": "/fixture",
                    "query_string": query, "headers": [
                        (b"authorization", f"Bearer {token}".encode()),
                        (b"content-type", b"application/json")]}, receive)


async def post(path, body, token="fixture-1"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://fixture.invalid") as client:
        return await client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_free_legacy_daily_quota_is_per_key_and_counts_success_only(state):
    key = state.db.keys["fixture-1"]
    key["daily_images"] = 1
    first = await post("/ai/generate-image", image_body())
    assert first.status_code == 200
    assert state.db.charges[-1][1]["legacy_free_images"] == 1
    rejected = await post("/ai/generate-image", image_body())
    assert rejected.status_code == 429
    assert "免费图额度" in rejected.json()["error"]["message"]
    assert len(state.nai.calls) == 1
    other = await post("/ai/generate-image", image_body(), token="fixture-2")
    assert other.status_code == 200
    assert len(state.nai.calls) == 2


@pytest.mark.asyncio
async def test_paid_legacy_does_not_use_free_daily_quota(state):
    key = state.db.keys["fixture-1"]
    key["daily_images"] = 1
    first = await post("/ai/generate-image", image_body())
    assert first.status_code == 200
    paid = await post("/ai/generate-image", image_body(precise=1))
    assert paid.status_code == 200
    assert state.db.charges[-1][1]["anlas"] > 0
    assert state.db.charges[-1][1]["legacy_free_images"] == 0


@pytest.mark.asyncio
async def test_failed_legacy_generation_does_not_use_free_daily_quota(state):
    state.db.keys["fixture-1"]["daily_images"] = 1
    state.nai.status = 500
    failed = await post("/ai/generate-image", image_body())
    assert failed.status_code == 500
    assert not state.db.charges
    state.nai.status = 200
    succeeded = await post("/ai/generate-image", image_body())
    assert succeeded.status_code == 200
    assert state.db.charges[-1][1]["legacy_free_images"] == 1


@pytest.mark.asyncio
async def test_disconnect_during_unknown_result_records_pending_once(state):
    state.nai.release = asyncio.Event()
    state.nai.error = UpstreamError(502, '上游响应中断', billing_uncertain=True)
    task = asyncio.create_task(post('/ai/generate-image', image_body(width=256, height=256, steps=29)))
    await state.nai.entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert state.image_budget_lock.locked()
    state.nai.release.set()
    # The shielded upstream failure completes its error response and ledger,
    # even though the downstream caller has already cancelled.
    assert (await task).status_code == 502
    assert len(state.nai.calls) == 1 and len(state.db.logs) == 1
    assert state.db.logs[0][1]['unconfirmed_anlas'] == 2
    assert not state.db.charges and not state.image_budget_lock.locked()


@pytest.mark.asyncio
async def test_img2img_batch_counts_only_one_free_image_against_daily_limit(state):
    state.db.keys['fixture-1']['daily_images'] = 1
    batch = image_body(width=512, height=512, steps=20, image='fixture', strength=1, n_samples=3)
    assert (await post('/ai/generate-image', batch)).status_code == 200
    charge, = state.db.charges
    assert (charge[1]['anlas'], charge[1]['images'], charge[1]['legacy_free_images']) == (8, 3, 1)
    assert (await post('/ai/generate-image', batch)).status_code == 429
    assert len(state.nai.calls) == 1


@pytest.mark.asyncio
async def test_reference_batch_budget_matches_discounted_charge(state):
    key = state.db.keys['fixture-1']
    body = image_body(precise=1, width=512, height=512, steps=20,
                      image='fixture', strength=1, n_samples=2)
    key['daily_anlas'] = 8
    assert (await post('/ai/generate-image', body)).status_code == 402
    assert not state.nai.calls
    key['daily_anlas'] = 9
    assert (await post('/ai/generate-image', body)).status_code == 200
    charge, = state.db.charges
    assert (charge[1]['anlas'], charge[1]['legacy_free_images']) == (9, 0)


@pytest.mark.asyncio
async def test_free_img2img_requires_both_key_permissions(state):
    key = state.db.keys['fixture-1']
    key['allow_anlas'] = False
    key['allow_img2img'] = False
    body = image_body(image='fixture', strength=.6)
    assert (await post('/ai/generate-image', body)).status_code == 400
    assert not state.nai.calls
    key['allow_img2img'] = True
    denied = await post('/ai/generate-image', body)
    assert denied.status_code == 402
    assert 'Anlas 权限' in denied.json()['error']['message']
    assert not state.nai.calls and not state.db.charges
    key['allow_anlas'] = True
    assert (await post('/ai/generate-image', body)).status_code == 200
    assert state.db.charges[-1][1]['anlas'] == 0
    assert not state.nai.calls[-1][3]['requires_anlas']


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("field", ["image", "mask"])
async def test_img2img_anlas_permission_checked_before_dispatch(state, streaming, field):
    state.db.keys["fixture-1"]["allow_anlas"] = False
    response = await post("/ai/generate-image" + ("-stream" if streaming else ""),
                          image_body(**{field: PNG}))
    assert response.status_code == 402
    assert not state.nai.calls and not state.db.charges
    assert state.global_active == 0 and not state.image_budget_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_img2img_admin_bypass_preserved(state, admin):
    state.db.keys["fixture-1"].update(
        is_admin=admin, allow_img2img=False, allow_anlas=False)
    state.settings.allow_img2img = False
    response = await post("/ai/generate-image", image_body(image=PNG))
    assert response.status_code == (200 if admin else 400)
    assert bool(state.nai.calls) is admin


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_anlas,admin,clamp,expected", [
    (False, False, True, 512),
    (True, False, True, 1024),
    (False, True, True, 1024),
    (False, False, False, 1024),
])
async def test_custom_pixel_limit_only_applies_to_free_key_clamp(
        state, allow_anlas, admin, clamp, expected):
    state.db.keys["fixture-1"].update(allow_anlas=allow_anlas, is_admin=admin)
    state.settings.max_pixels = 512 * 512
    state.settings.safe_clamp = clamp
    assert (await post("/ai/generate-image", image_body())).status_code == 200
    sent = state.nai.calls[0][2]["parameters"]
    assert (sent["width"], sent["height"]) == (expected, expected)
    assert not state.nai.calls[0][3]["requires_anlas"]
    assert state.db.charges[0][1]["anlas"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_anlas", [False, True])
async def test_custom_free_size_preserved_and_zero_billed(state, allow_anlas):
    state.db.keys["fixture-1"]["allow_anlas"] = allow_anlas
    body = image_body(width=896, height=1152)
    response = await post("/ai/generate-image", body)
    assert response.status_code == 200
    assert state.nai.calls[0][2] == body
    assert state.nai.calls[0][3]["requires_anlas"] is False
    assert state.db.charges[0][1]["anlas"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("field,value", [
    ("width", {"invalid": 1}), ("steps", "invalid"),
    ("n_samples", [2]), ("width", 10 ** 500),
], ids=["width-object", "steps-string", "samples-list", "width-overflow"])
async def test_invalid_free_clamp_parameters_rejected_before_dispatch(state, streaming, field, value):
    state.db.keys["fixture-1"]["allow_anlas"] = False
    response = await post("/ai/generate-image" + ("-stream" if streaming else ""), image_body(**{field: value}))
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "图片参数无效"
    assert not state.nai.calls and not state.db.charges
    assert state.global_active == 0 and not state.image_budget_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 201])
async def test_precise_generation_settles_and_uses_paid_token_policy(state, status):
    state.nai.status = status
    body = image_body(precise=2)
    response = await post("/ai/generate-image", body)
    assert response.status_code == 200
    assert state.db.charges[0][1]["anlas"] == 10
    assert state.db.logs[-1][1]['unconfirmed_anlas'] == 0
    assert state.nai.calls[0][2] == body
    assert state.nai.calls[0][3] | {"on_rate_limited": None} == {
        "accept": "*/*", "on_rate_limited": None, "requires_anlas": True,
        "v5_free": False, "image_count": 1, "image_lane": True,
        "max_response_bytes": 64 * 1024 * 1024}
    assert not state.image_budget_lock.locked() and state.global_active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/ai/encode-vibe", "/nai/ai/encode-vibe"])
@pytest.mark.parametrize("status", [200, 201])
async def test_encoding_binary_response_and_own_two_anlas_fee(state, path, status):
    state.nai.status = status
    response = await post(path, encoding_body())
    assert response.status_code == 200 and response.content == state.nai.content
    assert response.headers["cache-control"] == "no-store"
    assert state.db.charges[0][1]["anlas"] == 2
    assert state.db.logs[-1][1]['unconfirmed_anlas'] == 0
    assert state.db.charges[0][1]["images"] == 0
    options = state.nai.calls[0][3]
    assert options["requires_anlas"] and options["image_lane"]
    assert options["image_count"] == 0 and not options["v5_free"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "content_type", "content"), [
    (500, "text/plain", b"private-upstream-error"),
    (200, "application/json", b'{"error":"private"}'),
    (200, "application/octet-stream", b""),
])
async def test_encoding_failures_do_not_charge_or_echo_binary_input(state, status, content_type, content):
    state.nai.status, state.nai.content_type, state.nai.content = status, content_type, content
    response = await post("/ai/encode-vibe", encoding_body())
    assert response.status_code == 502
    assert b"private" not in response.content
    assert not state.db.charges and not state.image_budget_lock.locked()


@pytest.mark.asyncio
async def test_encoding_and_reference_permission_reject_before_upstream(state):
    state.db.keys["fixture-1"]["allow_anlas"] = False
    assert (await post("/ai/encode-vibe", encoding_body())).status_code == 402
    assert (await post("/ai/generate-image", image_body(precise=1))).status_code == 402
    assert not state.nai.calls
    assert (await post("/ai/encode-vibe", encoding_body(), "unknown")).status_code == 401


@pytest.mark.asyncio
async def test_invalid_reference_and_unsupported_encoding_reject_before_upstream(state):
    body = image_body(precise=1)
    body["parameters"]["director_reference_strength_values"] = []
    assert (await post("/ai/generate-image", body)).status_code == 400
    assert (await post("/ai/encode-vibe", encoding_body() | {"model": "nai-diffusion-5-full"})).status_code == 400
    assert not state.nai.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_tags_forward_get_escaped_query_without_generation_rpm(state, method):
    body = {"prompt": "blue hair&星", "model": "nai-diffusion-4-5-full"}
    query = b"prompt=blue+hair%26%E6%98%9F&model=nai-diffusion-4-5-full"
    await main.suggest_tags(request(body, method=method, query=query))
    forwarded = state.nai.calls[0]
    assert forwarded[0] == "GET" and forwarded[2] is None
    assert parse_qs(urlsplit(forwarded[1]).query) == {key: [value] for key, value in body.items()}
    assert state.rpm_hits == 0
    assert state.key_slots == [] and forwarded[3]["image_lane"]
    assert forwarded[3]["wait_for_image_slot"] is False


@pytest.mark.asyncio
async def test_encoding_rate_limit_keeps_upstream_global_cooldown_minimum(state):
    state.nai.error = UpstreamError(429, "fixture-rate-limit")
    assert (await post("/ai/encode-vibe", encoding_body())).status_code == 429
    assert state.cooldowns == [60] and not state.db.charges
    assert len(state.db.logs) == 1 and state.db.logs[0][1]['unconfirmed_anlas'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_budget", [False, True])
async def test_parallel_requests_recheck_ledger_inside_global_budget_lock(state, shared_budget):
    if shared_budget:
        state.settings.global_monthly_anlas = 2
    else:
        state.db.keys["fixture-1"]["daily_anlas"] = 2
    state.nai.release = asyncio.Event()
    first = asyncio.create_task(post("/ai/encode-vibe", encoding_body()))
    await state.nai.entered.wait()
    second = asyncio.create_task(post("/ai/encode-vibe", encoding_body(), "fixture-2" if shared_budget else "fixture-1"))
    for _ in range(100):
        if state.global_waiting == 1:
            break
        await asyncio.sleep(0)
    assert state.global_active == 1 and state.global_waiting == 1 and len(state.nai.calls) == 1
    state.nai.release.set()
    responses = await asyncio.gather(first, second)
    assert [item.status_code for item in responses] == [200, 402]
    assert len(state.nai.calls) == 1 and len(state.db.charges) == 1
    assert state.global_active == 0 and not state.image_budget_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_kind", ["asyncio", "anyio"])
async def test_cancellation_during_success_ledger_keeps_locks_until_accounted(state, cancel_kind):
    state.db.accounting_release = asyncio.Event()
    scope = anyio.CancelScope()

    async def run():
        with scope:
            return await main.encode_vibe(request(encoding_body()))

    task = asyncio.create_task(run())
    await state.db.accounting_entered.wait()
    if cancel_kind == "asyncio":
        task.cancel()
    else:
        scope.cancel()
    await asyncio.sleep(0)
    assert state.image_budget_lock.locked() and state.global_active == 1
    assert not state.db.charges
    state.db.accounting_release.set()
    if cancel_kind == "asyncio":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert state.db.charges[0][1]["anlas"] == 2
    assert not state.image_budget_lock.locked() and state.global_active == 0


@pytest.mark.asyncio
async def test_admin_keeps_upstream_exemptions_but_still_uses_paid_token(state):
    key = state.db.keys["fixture-1"]
    key.update(is_admin=True, allow_anlas=False, daily_anlas=1, monthly_anlas=1)
    state.settings.global_monthly_anlas = 1
    assert (await post("/ai/encode-vibe", encoding_body())).status_code == 200
    assert state.rpm_hits == 0 and state.key_slots == [] and state.semaphores == {}
    assert state.nai.calls[0][3]["requires_anlas"]
    assert state.db.charges[0][1]["anlas"] == 2


@pytest.mark.asyncio
async def test_admin_paid_reference_keeps_parameters_despite_free_key_clamp(state):
    state.db.keys["fixture-1"].update(is_admin=True, allow_anlas=False)
    assert state.settings.safe_clamp is True
    body = image_body(precise=1, width=1536, height=1024, steps=40, n_samples=2)
    response = await post("/ai/generate-image", body)
    assert response.status_code == 200
    assert state.nai.calls[0][2] == body
    options = state.nai.calls[0][3]
    assert options["requires_anlas"] and options["image_lane"]
    assert options["image_count"] == 2 and not options["v5_free"]
    assert state.db.charges[0][1]["images"] == 2
    assert state.db.charges[0][1]["anlas"] > 10


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["encode", "generation"])
@pytest.mark.parametrize("cancel_kind", ["asyncio", "anyio"])
async def test_cancel_after_dispatch_finishes_response_and_user_charge(state, kind, cancel_kind):
    # Model the upstream returning success while its own token accounting has
    # not yet returned control to main. Cancelling main must not cancel that job.
    state.nai.release = asyncio.Event()
    scope = anyio.CancelScope()

    async def run():
        with scope:
            if kind == "encode":
                return await main.encode_vibe(request(encoding_body()))
            return await main.generate_image(request(image_body(precise=1)))

    task = asyncio.create_task(run())
    await state.nai.entered.wait()
    if cancel_kind == "asyncio":
        task.cancel()
    else:
        scope.cancel()
    await asyncio.sleep(0)
    assert state.image_budget_lock.locked() and state.global_active == 1
    assert not task.done() and not state.db.charges
    state.nai.release.set()
    if cancel_kind == "asyncio":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert state.db.charges[0][1]["anlas"] == (2 if kind == "encode" else 5)
    assert state.global_active == 0 and not state.image_budget_lock.locked()


@pytest.mark.asyncio
async def test_image_stream_still_requires_authentication_before_upstream(state):
    response = await post("/ai/generate-image-stream", image_body(), token="unknown")
    assert response.status_code == 401
    assert not state.nai.calls and not state.db.charges


@pytest.mark.asyncio
async def test_cancelled_semaphore_wait_restores_key_slot_and_queue_counts(state):
    state.global_sem = asyncio.Semaphore(0)

    async def wait():
        async with main.acquire_concurrency(state.db.keys["fixture-1"]):
            raise AssertionError("must not enter")

    task = asyncio.create_task(wait())
    await asyncio.sleep(0)
    assert state.global_waiting == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state.global_waiting == 0 and state.global_active == 0
    assert state.semaphores[1]._value == state.settings.key_concurrency
