"""Daily Vibe grants use real SQLite; upstream encoding is always mocked."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.requests import Request

from app import main, registration_routes
from app.database import Database
from app.image_queue import ImageQueue
from app.registration import COMMAND_GUILD


DAY = "2026-10-14"
NEXT_DAY = "2026-10-15"


def encoding_body():
    # Valid 1x1 PNG; no source image or paid API is used in these tests.
    return dict(image="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==",
                model="nai-diffusion-4-5-full", informationExtracted=0.7)


def request(body):
    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
    return Request({"type": "http", "method": "POST", "path": "/ai/encode-vibe",
                    "headers": [(b"authorization", b"Bearer fixture-1"),
                                (b"content-type", b"application/json")]}, receive)


async def post(path, body, token="fixture-1"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        return await client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})


class MockUpstream:
    image_host = "https://mock.invalid"

    def __init__(self):
        self.calls = []
        self.status, self.content_type, self.content = 200, "application/octet-stream", b"mock-encoded-vibe"
        self.error = self.release = None
        self.entered = asyncio.Event()

    async def request(self, method, url, body, **options):
        self.calls.append((method, url, body, options))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            if self.error.status == 429:
                await options["on_rate_limited"](30)
            raise self.error
        return httpx.Response(self.status, content=self.content,
                              headers={"content-type": self.content_type})


class EncodingState:
    def __init__(self, db):
        self.db, self.nai = db, MockUpstream()
        self.settings = SimpleNamespace(queue_timeout=2, key_concurrency=2,
                                        global_monthly_anlas=1, image_429_cooldown_seconds=60)
        self.image_budget_lock = asyncio.Lock()
        self.semaphores, self.key_slots = {}, []
        self.image_queue = ImageQueue(
            lambda: self.settings.key_concurrency, lambda: self.settings.key_concurrency,
            lambda key_id: key_id in self.semaphores and self.semaphores[key_id].locked())
        self.global_active = self.global_waiting = self.rpm_hits = 0

    def key_sem(self, key_id, capacity):
        return self.semaphores.setdefault(key_id, asyncio.Semaphore(capacity))

    async def hit_rpm(self, *args):
        self.rpm_hits += 1
        return True

    async def wait_for_key_image_slot(self, key_id):
        self.key_slots.append(key_id)

    def image_cooldown_remaining(self):
        return 0

    async def block_image_generation(self, seconds):
        return seconds

    def day(self):
        return self.db.vibe_day()


@pytest_asyncio.fixture
async def db(tmp_path):
    value = Database(str(tmp_path / "vibe.sqlite"), tz="UTC")
    await value.connect()
    yield value
    await value.close()


async def register(db, discord_id="777", token="fixture-1", **overrides):
    fields = dict(name="registered", token=token, enabled=True, daily_images=100,
                  daily_anlas=1, monthly_anlas=1, daily_text_tokens=0, rpm=100,
                  allow_anlas=False, image_model_scope="all")
    fields.update(overrides)
    key = await db.create_key(fields)
    await db._db.execute(
        "INSERT INTO discord_registrations(discord_id,key_id,token,created_at) VALUES(?,?,?,0)",
        (discord_id, key["id"], token))
    await db._db.commit()
    return key


@pytest.mark.asyncio
async def test_claim_is_three_once_per_discord_day_and_starts_at_zero(db):
    key = await register(db)
    assert await db.get_vibe_remaining(key["id"], DAY) == 0
    assert await db.claim_registered_vibe("777", DAY) == {"status": "claimed", "remaining": 3}
    assert await db.claim_registered_vibe("777", DAY) == {"status": "already", "remaining": 3}
    assert await db.get_vibe_remaining(key["id"], NEXT_DAY) == 0
    assert await db.claim_registered_vibe("777", NEXT_DAY) == {"status": "claimed", "remaining": 3}


@pytest.mark.asyncio
async def test_concurrent_reservations_use_only_three_and_release_is_idempotent(db):
    key = await register(db)
    assert await db.reserve_vibe_encoding(key["id"], DAY) is None
    await db.claim_registered_vibe("777", DAY)
    results = await asyncio.gather(*(db.reserve_vibe_encoding(key["id"], DAY) for _ in range(12)))
    tokens = [token for token in results if token is not None]
    assert len(tokens) == len(set(tokens)) == 3
    assert await db.get_vibe_remaining(key["id"], DAY) == 0
    assert await db.claim_registered_vibe("777", DAY) == {"status": "already", "remaining": 0}
    await asyncio.gather(*(db.release_vibe_encoding(tokens[0]) for _ in range(8)))
    assert await db.get_vibe_remaining(key["id"], DAY) == 1
    assert await db.get_vibe_remaining(key["id"], NEXT_DAY) == 0


@pytest.mark.asyncio
async def test_settlement_audits_actual_two_anlas_exactly_once_and_never_refunds(db):
    key = await register(db)
    await db.claim_registered_vibe("777", DAY)
    token = await db.reserve_vibe_encoding(key["id"], DAY)
    settled = await asyncio.gather(*(db.settle_vibe_encoding(token, "nai-diffusion-4-5-full") for _ in range(6)))
    assert sum(settled) == 1
    assert not await db.release_vibe_encoding(token)
    assert await db.get_vibe_remaining(key["id"], DAY) == 2
    counter = await db.get_counter(key["id"], DAY)
    assert (counter["anlas"], counter["requests"], counter["images"]) == (2, 1, 0)
    logs = await db.list_logs()
    assert len(logs) == 1
    assert (logs[0]["kind"], logs[0]["status"], logs[0]["anlas"]) == ("vibe_encode", "ok", 2)
    assert await db.month_anlas_all("2026-10") == 2
    assert (await db.get_key(key["id"]))["last_used_at"] is not None
    released = await db.reserve_vibe_encoding(key["id"], DAY)
    assert await db.release_vibe_encoding(released)
    assert not await db.settle_vibe_encoding(released, "nai-diffusion-4-5-full")
    assert not await db.settle_vibe_encoding("unknown", "nai-diffusion-4-5-full")


@pytest.mark.asyncio
async def test_vibe_bridge_requires_secret_guild_active_registration_and_returns_remaining(db):
    app = FastAPI()
    app.include_router(registration_routes.router)
    app.state.registrar = SimpleNamespace(db=db, bridge_secret="test-secret")
    headers = {"Authorization": "Bearer test-secret"}
    body = {"discord_id": "777", "guild_id": COMMAND_GUILD}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/self-register/vibe", json=body)).status_code == 401
        assert (await client.post("/self-register/vibe", json=body, headers=headers)).status_code == 403
        key = await register(db)
        for invalid in ({**body, "guild_id": "wrong"}, {**body, "discord_id": "not-a-user"}):
            assert (await client.post("/self-register/vibe", json=invalid, headers=headers)).status_code == 403
        ok = await client.post("/self-register/vibe", json=body, headers=headers)
        assert ok.status_code == 200
        assert ok.json() == {"status": "claimed", "remaining": 3}
        assert ok.headers["cache-control"] == "no-store"
        token = await db.reserve_vibe_encoding(key["id"])
        again = await client.post("/self-register/vibe", json=body, headers=headers)
        assert again.json() == {"status": "already", "remaining": 2}
        await db.update_key(key["id"], {"enabled": False})
        assert (await client.post("/self-register/vibe", json=body, headers=headers)).status_code == 403
        await db.update_key(key["id"], {"enabled": True, "expires_at": 1})
        assert (await client.post("/self-register/vibe", json=body, headers=headers)).status_code == 403
        assert token


@pytest_asyncio.fixture
async def encoding_state(db, monkeypatch):
    value = EncodingState(db)
    await register(db)
    value.settings.global_monthly_anlas = 1
    monkeypatch.setattr(main, "STATE", value)
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/ai/encode-vibe", "/nai/ai/encode-vibe"])
async def test_encoding_accepts_snake_case_extraction_and_forwards_canonical_field(encoding_state, path):
    state = encoding_state
    await state.db.claim_registered_vibe("777")
    body = encoding_body()
    body["information_extracted"] = body.pop("informationExtracted")
    response = await post(path, body)
    assert response.status_code == 200
    assert len(state.nai.calls) == 1
    upstream_body = state.nai.calls[0][2]
    assert upstream_body == encoding_body()
    assert "information_extracted" not in upstream_body
    assert await state.db.get_vibe_remaining(1) == 2
    assert len(await state.db.list_logs()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical,alias,expected_status", [
    (0.4, 0.9, 200),
    (0.4, "invalid", 200),
    (None, 0.9, 400),
    ("0.4", 0.9, 400),
])
async def test_encoding_canonical_extraction_takes_precedence(encoding_state, canonical, alias, expected_status):
    state = encoding_state
    await state.db.claim_registered_vibe("777")
    body = encoding_body() | {"informationExtracted": canonical, "information_extracted": alias}
    response = await post("/ai/encode-vibe", body)
    assert response.status_code == expected_status
    if expected_status == 200:
        assert state.nai.calls[0][2] == encoding_body() | {"informationExtracted": canonical}
        assert await state.db.get_vibe_remaining(1) == 2
    else:
        assert not state.nai.calls
        assert await state.db.get_vibe_remaining(1) == 3
        assert not await state.db.list_logs()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "0.6", True, -0.1, 1.1, [], {}])
async def test_encoding_snake_case_keeps_numeric_validation(encoding_state, value):
    state = encoding_state
    await state.db.claim_registered_vibe("777")
    body = encoding_body()
    del body["informationExtracted"]
    body["information_extracted"] = value
    response = await post("/ai/encode-vibe", body)
    assert response.status_code == 400
    assert "informationExtracted" in response.json()["error"]["message"]
    assert not state.nai.calls
    assert await state.db.get_vibe_remaining(1) == 3
    assert not await state.db.list_logs()


@pytest.mark.asyncio
async def test_encoding_snake_case_still_requires_daily_claim(encoding_state):
    state = encoding_state
    body = encoding_body()
    body["information_extracted"] = body.pop("informationExtracted")
    response = await post("/ai/encode-vibe", body)
    assert response.status_code == 429
    assert not state.nai.calls
    assert await state.db.get_vibe_remaining(1) == 0


@pytest.mark.asyncio
async def test_encoding_needs_claim_then_bypasses_all_anlas_limits_and_audits(encoding_state):
    state = encoding_state
    db = state.db
    day = db.vibe_day()
    denied = await post("/ai/encode-vibe", encoding_body())
    assert denied.status_code == 429
    assert not state.nai.calls
    await db.claim_registered_vibe("777")
    await db.bump_counters(1, day, anlas=100)
    for index in range(3):
        response = await post("/ai/encode-vibe", encoding_body())
        assert response.status_code == 200
        assert state.nai.calls[-1][3]["requires_anlas"] is True
        assert await db.get_vibe_remaining(1) == 2 - index
    assert (await post("/nai/ai/encode-vibe", encoding_body())).status_code == 429
    assert len(state.nai.calls) == 3
    assert (await db.get_counter(1, day))["anlas"] == 106
    assert len(await db.list_logs()) == 3
    assert not state.image_budget_lock.locked() and state.global_active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["status", "json", "empty", "rate_limit", "transport", "timeout"])
async def test_failed_upstream_releases_opportunity_without_success_audit(encoding_state, failure):
    state = encoding_state
    await state.db.claim_registered_vibe("777")
    if failure == "status":
        state.nai.status = 500
    elif failure == "json":
        state.nai.content_type = "application/json"
    elif failure == "empty":
        state.nai.content = b""
    elif failure == "rate_limit":
        from app.nai import UpstreamError
        state.nai.error = UpstreamError(429, "mock rate limit")
    else:
        async def fail(*args, **kwargs):
            if failure == "transport":
                raise httpx.ConnectError("mock connection failure")
            raise TimeoutError("mock timeout")
        state.nai.request = fail
    response = await post("/ai/encode-vibe", encoding_body())
    assert response.status_code == (429 if failure == "rate_limit" else 502)
    assert await state.db.get_vibe_remaining(1) == 3
    assert (await state.db.get_counter(1, state.db.vibe_day()))["anlas"] == 0
    assert not [log for log in await state.db.list_logs() if log["status"] == "ok"]
    assert state.global_active == 0


@pytest.mark.asyncio
async def test_concurrent_claims_only_one_grant_charge_cannot_refill(db):
    key = await register(db)
    results = await asyncio.gather(*(db.claim_registered_vibe("777", DAY) for _ in range(12)))
    assert sum(result["status"] == "claimed" for result in results) == 1
    assert sum(result["status"] == "already" for result in results) == 11
    assert all(result["remaining"] == 3 for result in results)
    token = await db.reserve_vibe_encoding(key["id"], DAY)
    await db.settle_vibe_encoding(token, "nai-diffusion-4-5-full")
    assert await db.charge_registered_key("777", DAY) == "charged"
    await db.reset_daily_image_quota(key["id"], DAY)
    assert await db.get_vibe_remaining(key["id"], DAY) == 2
    assert await db.claim_registered_vibe("777", DAY) == {"status": "already", "remaining": 2}


@pytest.mark.asyncio
async def test_restart_preserves_unsettled_reservations_conservatively_consumed(db):
    key = await register(db)
    await db.claim_registered_vibe("777", DAY)
    token = await db.reserve_vibe_encoding(key["id"], DAY)
    await db.close()
    await db.connect()
    assert await db.get_vibe_remaining(key["id"], DAY) == 2
    assert await db.claim_registered_vibe("777", DAY) == {"status": "already", "remaining": 2}
    row = (await db._db.execute_fetchall(
        "SELECT status FROM vibe_encoding_reservations WHERE token=?", (token,)))[0]
    assert row["status"] == "reserved"
    assert not await db.list_logs()


@pytest.mark.asyncio
async def test_key_recovery_cannot_reclaim_and_late_refund_uses_original_discord_day(db):
    key = await register(db)
    await db.claim_registered_vibe("777", DAY)
    success = await db.reserve_vibe_encoding(key["id"], DAY)
    await db.settle_vibe_encoding(success, "nai-diffusion-4-5-full")
    pending = await db.reserve_vibe_encoding(key["id"], DAY)
    await db.delete_key(key["id"])
    assert await db.claim_registered_vibe("777", DAY) == {"status": "not_registered", "remaining": 0}
    status, restored, old = await db.restore_discord_key("777", "restored-token")
    assert status == "restored" and old == key["id"] and restored != old
    assert await db.get_vibe_remaining(restored, DAY) == 1
    assert await db.claim_registered_vibe("777", DAY) == {"status": "already", "remaining": 1}
    await db.claim_registered_vibe("777", NEXT_DAY)
    await db.reserve_vibe_encoding(restored, NEXT_DAY)
    assert await db.release_vibe_encoding(pending)
    assert not await db.release_vibe_encoding(pending)
    assert await db.get_vibe_remaining(restored, DAY) == 2
    assert await db.get_vibe_remaining(restored, NEXT_DAY) == 2
    assert await db.get_vibe_remaining(old, DAY) == 0


@pytest.mark.asyncio
async def test_default_day_changes_at_shanghai_midnight_even_with_utc_site(db):
    key = await register(db)

    class Clock(datetime):
        instant = datetime(2026, 10, 14, 15, 59, 59, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz)

    with patch("datetime.datetime", Clock):
        assert db.vibe_day() == DAY
        assert (await db.claim_registered_vibe("777"))["remaining"] == 3
        pending = await db.reserve_vibe_encoding(key["id"])
        Clock.instant = datetime(2026, 10, 14, 16, tzinfo=timezone.utc)
        assert db.vibe_day() == NEXT_DAY
        assert await db.get_vibe_remaining(key["id"]) == 0
        assert await db.reserve_vibe_encoding(key["id"]) is None
        await db.release_vibe_encoding(pending)
        assert await db.get_vibe_remaining(key["id"]) == 0
        assert await db.claim_registered_vibe("777") == {"status": "claimed", "remaining": 3}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/user/subscription", "/v1/me"])
async def test_reports_follow_vibe_day_but_normal_quotas_follow_utc_site(db, monkeypatch, path):
    from pathlib import Path
    from app.config import Settings
    from app.state import GateState

    key = await register(db, daily_v5=30)
    state = GateState(Settings(data_dir=Path(db.path).parent, tz="UTC"))
    state.db = db
    monkeypatch.setattr(main, "STATE", state)
    await db.bump_counters(key["id"], DAY, v5=5, images=5)
    await db.bump_counters(key["id"], NEXT_DAY, v5=9, images=9)

    class Clock(datetime):
        instant = datetime(2026, 10, 14, 15, 59, 59, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz)

    async def check(client, remaining, site_used):
        response = await client.get(path, headers={"Authorization": "Bearer fixture-1"})
        assert response.status_code == 200
        payload = response.json()
        if path == "/user/subscription":
            assert payload["naiGate"]["vibeRemainingToday"] == remaining
            assert payload["naiGate"]["v5LeftToday"] == 30 - site_used
        else:
            assert payload["today"]["vibe_remaining"] == remaining
            assert payload["today"]["v5_today"] == site_used
            assert payload["today"]["images"] == site_used

    with patch("datetime.datetime", Clock), patch("app.state.time.time", lambda: Clock.instant.timestamp()):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            await db.claim_registered_vibe("777")
            assert await db.reserve_vibe_encoding(key["id"])
            await check(client, 2, 5)
            # Shanghai midnight resets Vibe, not UTC image quotas.
            Clock.instant = datetime(2026, 10, 14, 16, tzinfo=timezone.utc)
            await check(client, 0, 5)
            await db.claim_registered_vibe("777")
            assert await db.reserve_vibe_encoding(key["id"])
            await check(client, 2, 5)
            Clock.instant = datetime(2026, 10, 14, 23, 59, 59, tzinfo=timezone.utc)
            await check(client, 2, 5)
            # UTC midnight resets normal reporting, not the Shanghai grant.
            Clock.instant = datetime(2026, 10, 15, 0, tzinfo=timezone.utc)
            await check(client, 2, 9)


@pytest.mark.asyncio
async def test_real_sqlite_parallel_encodes_never_dispatch_more_than_three(encoding_state):
    state = encoding_state
    state.settings.key_concurrency = 20
    await state.db.claim_registered_vibe("777")
    state.nai.release = asyncio.Event()
    tasks = [asyncio.create_task(post("/ai/encode-vibe", encoding_body())) for _ in range(20)]
    async with asyncio.timeout(3):
        while len(state.nai.calls) < 3:
            await asyncio.sleep(0)
    assert await state.db.get_vibe_remaining(1) == 0
    state.nai.release.set()
    responses = await asyncio.gather(*tasks)
    assert sum(response.status_code == 200 for response in responses) == 3
    assert sum(response.status_code == 429 for response in responses) == 17
    assert len(state.nai.calls) == 3
    assert (await state.db.get_counter(1, state.db.vibe_day()))["anlas"] == 6
    assert len(await state.db.list_logs()) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["upstream", "settlement", "release"])
@pytest.mark.parametrize("cancel_kind", ["asyncio", "anyio"])
async def test_cancellation_finishes_real_sqlite_settlement_or_failure_refund(encoding_state, monkeypatch, phase, cancel_kind):
    import anyio

    state = encoding_state
    await state.db.claim_registered_vibe("777")
    entered, release = asyncio.Event(), asyncio.Event()
    if phase == "upstream":
        state.nai.entered, state.nai.release = entered, release
    else:
        name = "release_vibe_encoding" if phase == "release" else "settle_vibe_encoding"
        original = getattr(state.db, name)

        async def pause(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        monkeypatch.setattr(state.db, name, pause)
        if phase == "release":
            state.nai.status = 500
    scope = anyio.CancelScope()

    async def run():
        with scope:
            return await main.encode_vibe(request(encoding_body()))

    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), 3)
    if cancel_kind == "asyncio":
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
    else:
        scope.cancel()
    await asyncio.sleep(0)
    assert not task.done() and state.global_active == 1
    assert await state.db.get_vibe_remaining(1) == 2
    release.set()
    if phase == "release":
        with pytest.raises(main.GateError):
            await task
    elif cancel_kind == "asyncio":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert state.global_active == 0
    expected = 3 if phase == "release" else 2
    assert await state.db.get_vibe_remaining(1) == expected
    logs = [log for log in await state.db.list_logs() if log["status"] == "ok"]
    assert len(logs) == (0 if phase == "release" else 1)
    assert (await state.db.get_counter(1, state.db.vibe_day()))["anlas"] == (0 if phase == "release" else 2)


@pytest.mark.asyncio
async def test_successful_upstream_with_failed_audit_keeps_reservation_consumed(encoding_state):
    state = encoding_state
    db = state.db
    await db.claim_registered_vibe("777")
    await db._db.execute("""CREATE TRIGGER fail_vibe_counter BEFORE INSERT ON counters
                            BEGIN SELECT RAISE(ABORT, 'mock audit failure'); END""")
    await db._db.commit()
    import aiosqlite
    with pytest.raises(aiosqlite.IntegrityError, match="mock audit failure"):
        await post("/ai/encode-vibe", encoding_body())
    assert await db.get_vibe_remaining(1) == 2
    assert not await db.list_logs()  # The log and counter roll back together.
    token = (await db._db.execute_fetchall("SELECT token FROM vibe_encoding_reservations"))[0][0]
    await db._db.execute("DROP TRIGGER fail_vibe_counter")
    await db._db.commit()
    assert await db.settle_vibe_encoding(token, "nai-diffusion-4-5-full")
    assert not await db.settle_vibe_encoding(token, "nai-diffusion-4-5-full")
    assert len(await db.list_logs()) == 1 and len(state.nai.calls) == 1


@pytest.mark.asyncio
async def test_admin_unregistered_key_exemption_still_selects_paid_token_and_audits(encoding_state):
    state = encoding_state
    fields = dict(await state.db.get_key(1))
    fields.update(token="admin-test", name="admin", is_admin=True)
    admin = await state.db.create_key(fields)
    for _ in range(4):
        assert (await post("/ai/encode-vibe", encoding_body(), token="admin-test")).status_code == 200
    assert await state.db.get_vibe_remaining(admin["id"]) == 0
    assert state.rpm_hits == 0 and state.key_slots == [] and state.semaphores == {}
    assert all(call[3]["requires_anlas"] for call in state.nai.calls)
    assert (await state.db.get_counter(admin["id"], state.day()))["anlas"] == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [{"enabled": False}, {"expires_at": 1}])
async def test_disabled_or_expired_registration_cannot_claim_or_reserve(db, invalid):
    key = await register(db)
    await db.claim_registered_vibe("777", DAY)
    await db.update_key(key["id"], invalid)
    assert await db.claim_registered_vibe("777", NEXT_DAY) == {"status": "not_registered", "remaining": 0}
    assert await db.reserve_vibe_encoding(key["id"], DAY) is None
    assert await db.get_vibe_remaining(key["id"], DAY) == 3
    assert await db.get_vibe_remaining(999, DAY) == 0


@pytest.mark.asyncio
async def test_encoding_unregistered_paid_permission_key_cannot_bypass_daily_grant(encoding_state):
    state = encoding_state
    fields = dict(await state.db.get_key(1))
    fields.update(token="unregistered", allow_anlas=True, daily_anlas=100, monthly_anlas=100)
    key = await state.db.create_key(fields)
    assert await state.db.get_vibe_remaining(key["id"]) == 0
    assert (await post("/ai/encode-vibe", encoding_body(), token="unregistered")).status_code == 429
    assert not state.nai.calls


@pytest.mark.asyncio
async def test_cancellation_before_concurrency_admission_does_not_consume(encoding_state):
    state = encoding_state
    await state.db.claim_registered_vibe("777")
    state.semaphores[1] = asyncio.Semaphore(0)
    task = asyncio.create_task(main.encode_vibe(request(encoding_body())))
    async with asyncio.timeout(3):
        while state.global_waiting != 1 and not state.image_queue.snapshot()["queued"]:
            await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await state.db.get_vibe_remaining(1) == 3
    assert not state.nai.calls and state.global_active == state.global_waiting == 0
    assert state.image_queue.snapshot()["total"] == 0


@pytest.mark.asyncio
async def test_delete_recover_during_dispatch_preserves_original_audit_and_consumption(encoding_state):
    state = encoding_state
    db = state.db
    await db.claim_registered_vibe("777")
    state.nai.release = asyncio.Event()
    task = asyncio.create_task(post("/ai/encode-vibe", encoding_body()))
    await asyncio.wait_for(state.nai.entered.wait(), 3)
    await db.delete_key(1)
    status, restored, _ = await db.restore_discord_key("777", "restored-token")
    assert status == "restored"
    assert await db.get_vibe_remaining(restored) == 2
    assert await db.claim_registered_vibe("777") == {"status": "already", "remaining": 2}
    state.nai.release.set()
    assert (await task).status_code == 200
    assert (await db.get_counter(1, db.vibe_day()))["anlas"] == 2
    assert (await db.get_counter(restored, db.vibe_day()))["anlas"] == 0
    assert (await db.list_logs())[0]["key_id"] == 1
    assert await db.get_vibe_remaining(restored) == 2
