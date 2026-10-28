"""Account edits preserve usage and never dispatch retired credentials."""

import asyncio
import json
import os
import stat

import httpx
import pytest
import pytest_asyncio

from app.database import Database
from app.nai import NaiClient
from app.upstream_store import UpstreamManagementError


def client_for(db, tokens=None):
    return NaiClient(tokens if tokens is not None else ["fixture-first-credential"],
                     "https://image.invalid", "https://text.invalid", "https://legacy.invalid",
                     db=db, day_fn=lambda: "2026-10-27", v5_daily_limits=[150],
                     allow_anlas=[True], image_min_interval=0)


@pytest_asyncio.fixture
async def managed(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    client = client_for(db)
    path = tmp_path / "upstream_tokens.json"
    await client.load_managed_tokens(path)
    try:
        yield client, db, path
    finally:
        if isinstance(client._client, httpx.AsyncClient):
            await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_managed_accounts_override_environment_and_keep_existing_policy(managed):
    client, db, path = managed
    original = client.pool[0]
    assert client.upstream_source == "environment" and not path.exists()
    await client.set_v5_daily_limit(original.token_id, 12)
    await client.set_image_concurrency(original.token_id, 2)
    added = await client.add_token("fixture-second-credential", v5_daily_limit=5,
                                   image_concurrency=3, allow_anlas=False, enabled=False)
    assert client.upstream_source == "panel"
    await client.set_v5_daily_limit(added, 7)
    await client.set_image_concurrency(added, 4)
    await client.set_admin_enabled(added, True)
    restored = client_for(db, ["ignored-environment-credential"])
    await restored.load_managed_tokens(path)
    await restored.load_saved_limits()
    assert [t.token for t in restored.pool] == ["fixture-first-credential", "fixture-second-credential"]
    assert [(t.v5_daily_limit, t.image_slots.limit, t.admin_enabled, t.allow_anlas)
            for t in restored.pool] == [(12, 2, True, True), (7, 4, True, False)]
    assert restored.pool[1].token_id == added
    status = json.dumps(await restored.status())
    for token in restored.pool:
        assert token.token not in status
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".upstream-*.tmp"))


@pytest.mark.asyncio
async def test_credential_rotation_keeps_usage_but_resets_credential_health(managed):
    client, db, path = managed
    old = client.pool[0]
    identity, credential_id = old.token_id, old.credential_id
    await client.set_v5_daily_limit(identity, 10)
    await client.set_image_concurrency(identity, 3)
    await client.set_allow_anlas(identity, False)
    await db.bump_upstream_v5_counter(identity, "2026-10-27", 0.6)
    await db.bump_upstream_image_counter(identity, "2026-10-27", 2)
    await client.set_admin_enabled(identity, False)
    old.disabled, old.blocked_until, old.fails = True, 1e12, 9
    client.allowance._rows[identity] = {"percent": 0, "error": "fixture"}
    assert await client.replace_token(identity, "fixture-replacement-credential") == identity
    new = client.pool[0]
    assert new is not old and old.admin_enabled is False
    assert new.token_id == identity and new.credential_id != credential_id
    assert (new.v5_daily_limit, new.image_slots.limit, new.admin_enabled, new.allow_anlas) == (10, 3, False, False)
    assert not new.disabled and not new.blocked_until and new.fails == 0
    assert identity not in client.allowance._rows
    assert await db.get_upstream_counter(identity, "2026-10-27") == {"images": 2, "v5": 0.6}
    restored = client_for(db)
    await restored.load_managed_tokens(path)
    await restored.load_saved_limits()
    assert restored.pool[0].token == new.token and restored.pool[0].token_id == identity
    assert restored.pool[0].v5_daily_limit == 10
    assert (await restored.status())[0]["v5_used"] == 0.6


@pytest.mark.asyncio
async def test_removing_last_account_does_not_restore_environment_or_delete_history(managed):
    client, db, path = managed
    identity = client.pool[0].token_id
    await db.bump_upstream_v5_counter(identity, "2026-10-27")
    await client.set_admin_enabled(identity, False)
    assert await client.remove_token(identity)
    restored = client_for(db)
    await restored.load_managed_tokens(path)
    assert restored.pool == [] and not restored.configured
    assert restored.upstream_source == "panel"
    assert (await db.get_upstream_counter(identity, "2026-10-27"))["v5"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["enabled", "image", "reservation", "dispatch", "text"])
async def test_rotation_and_removal_reject_active_or_enabled_account(managed, busy):
    client, _, path = managed
    token = client.pool[0]
    token.admin_enabled = busy == "enabled"
    if busy == "image":
        await token.image_slots.acquire()
    if busy == "reservation":
        token.pending_v5_units = 60
    if busy == "dispatch":
        await token.dispatch_lock.acquire()
    if busy == "text":
        token.text_streams = 1
    for action in (lambda: client.replace_token(token.token_id, "fixture-new-credential"),
                   lambda: client.remove_token(token.token_id)):
        with pytest.raises(UpstreamManagementError) as error:
            await action()
        assert error.value.status == 409
    assert client.pool[0] is token and not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, 1, "", "  ", "line\nbreak", "tab\tvalue", "a b", "a\x7fb", "汉字", "x" * 8193])
async def test_invalid_credentials_never_reach_disk(managed, value):
    client, _, path = managed
    with pytest.raises(UpstreamManagementError) as error:
        await client.add_token(value)
    assert error.value.status == 422 and not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"v5_daily_limit": True}, {"v5_daily_limit": -1},
                                    {"v5_daily_limit": 100001}, {"image_concurrency": 0},
                                    {"image_concurrency": 5}, {"image_concurrency": 1.5},
                                    {"allow_anlas": "false"}, {"enabled": 0}])
async def test_invalid_policy_never_reaches_disk(managed, kwargs):
    client, _, path = managed
    with pytest.raises(UpstreamManagementError) as error:
        await client.add_token("fixture-new-credential", **kwargs)
    assert error.value.status == 422 and not path.exists()


@pytest.mark.asyncio
async def test_duplicate_and_unknown_accounts_leave_store_unchanged(managed):
    client, _, path = managed
    await client.set_allow_anlas(client.pool[0].token_id, False)
    before = path.read_bytes()
    with pytest.raises(UpstreamManagementError) as error:
        await client.add_token(client.pool[0].token)
    assert error.value.status == 409
    with pytest.raises(UpstreamManagementError) as error:
        await client.remove_token("token-unknown")
    assert error.value.status == 404
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_failed_atomic_write_preserves_pool_and_removes_private_temporary(managed, monkeypatch):
    client, _, path = managed
    token = client.pool[0]
    await client.set_allow_anlas(token.token_id, False)
    before = path.read_bytes()

    def fail(*args):
        raise OSError("fixture-secret-must-not-escape")

    monkeypatch.setattr("app.upstream_store.os.replace", fail)
    with pytest.raises(UpstreamManagementError) as error:
        await client.add_token("fixture-another-credential")
    assert error.value.status == 503 and "fixture-secret" not in str(error.value)
    assert path.read_bytes() == before and client.pool == [token]
    assert not list(path.parent.glob(".upstream-*.tmp"))


@pytest.mark.asyncio
async def test_corrupt_store_fails_instead_of_reintroducing_environment_accounts(managed):
    client, _, path = managed
    path.write_text('{"tokens": "fixture-private-value"}', encoding="utf-8")
    with pytest.raises(UpstreamManagementError) as error:
        await client.load_managed_tokens(path)
    assert error.value.status == 503 and "fixture-private-value" not in str(error.value)


@pytest.mark.asyncio
async def test_revoking_anlas_while_waiting_reroutes_before_dispatch(managed):
    client, _, _ = managed
    second = await client.add_token("fixture-second-credential")
    target = client.pool[1]
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []

    async def wait_slot(token):
        if token is target:
            entered.set()
            await release.wait()

    class FakeHTTP:
        async def request(self, *args, **kwargs):
            sent.append(kwargs["headers"]["Authorization"])
            return httpx.Response(200, content=b"ok")

    client.wait_for_token_image_slot = wait_slot
    client._client = FakeHTTP()
    task = asyncio.create_task(client.request("POST", "https://image.invalid", image_lane=True,
                                              requires_anlas=True))
    await entered.wait()
    await client.set_allow_anlas(second, False)
    release.set()
    assert (await task).status_code == 200
    assert sent == ["Bearer fixture-first-credential"]


@pytest.mark.asyncio
@pytest.mark.parametrize("remove", [False, True])
async def test_selected_request_cannot_send_retired_credential(managed, remove):
    client, _, _ = managed
    identity = await client.add_token("fixture-selected-credential")
    target = client.pool[1]
    entered, release = asyncio.Event(), asyncio.Event()
    acquire = target.image_slots.acquire
    sent = []

    async def wait_acquire():
        entered.set()
        await release.wait()
        await acquire()

    class FakeHTTP:
        async def request(self, *args, **kwargs):
            sent.append(kwargs["headers"]["Authorization"])
            return httpx.Response(200, content=b"ok")

    target.image_slots.acquire = wait_acquire
    client._client = FakeHTTP()
    task = asyncio.create_task(client.request("POST", "https://image.invalid", image_lane=True))
    await entered.wait()
    await client.set_admin_enabled(identity, False)
    if remove:
        await client.remove_token(identity)
    else:
        await client.replace_token(identity, "fixture-replacement-credential")
        await client.set_admin_enabled(identity, True)
    release.set()
    assert (await task).status_code == 200
    assert "Bearer fixture-selected-credential" not in sent
    assert target.admin_enabled is False


@pytest.mark.asyncio
async def test_open_text_response_blocks_rotation_until_close_finishes(managed):
    client, _, _ = managed
    entered, release = asyncio.Event(), asyncio.Event()

    class Frames(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"fixture"

        async def aclose(self):
            entered.set()
            await release.wait()

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=Frames())))
    response = await client.stream("https://text.invalid", {})
    token = client.pool[0]
    await client.set_admin_enabled(token.token_id, False)
    close = asyncio.create_task(response.aclose())
    await entered.wait()
    try:
        with pytest.raises(UpstreamManagementError) as error:
            await client.replace_token(token.token_id, "fixture-replacement-credential")
        assert error.value.status == 409 and token.text_streams == 1
    finally:
        release.set()
        await close
    assert token.text_streams == 0
    await client.replace_token(token.token_id, "fixture-replacement-credential")


@pytest.mark.asyncio
async def test_failed_text_close_releases_account_for_management(managed):
    client, _, _ = managed

    class Frames(httpx.AsyncByteStream):
        close_calls = 0

        async def __aiter__(self):
            yield b"fixture"

        async def aclose(self):
            self.close_calls += 1
            raise httpx.ReadError("fixture close failure")

    frames = Frames()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=frames)))
    response = await client.stream("https://text.invalid", {})
    token = client.pool[0]
    await client.set_admin_enabled(token.token_id, False)

    results = await asyncio.wait_for(asyncio.gather(
        response.aclose(), response.aclose(), return_exceptions=True), 1)
    assert sum(isinstance(result, httpx.ReadError) for result in results) == 1
    assert sum(result is None for result in results) == 1
    assert response.is_closed and token.text_streams == 0 and not token.busy
    await asyncio.wait_for(response.aclose(), 1)
    assert frames.close_calls == 1 and token.text_streams == 0
    await client.replace_token(token.token_id, "fixture-replacement-credential")
    assert await client.remove_token(token.token_id)


@pytest.mark.asyncio
async def test_cancelled_text_eof_close_finishes_before_account_release(managed):
    client, _, _ = managed
    entered, release = asyncio.Event(), asyncio.Event()

    class Frames(httpx.AsyncByteStream):
        close_calls = 0
        closed = False

        async def __aiter__(self):
            yield b"fixture"

        async def aclose(self):
            self.close_calls += 1
            entered.set()
            await release.wait()
            self.closed = True

    frames = Frames()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=frames)))
    response = await client.stream("https://text.invalid", {})
    token = client.pool[0]
    await client.set_admin_enabled(token.token_id, False)

    async def consume():
        async for _ in response.aiter_bytes():
            pass

    consumer = asyncio.create_task(consume())
    other_close = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        consumer.cancel()
        await asyncio.sleep(0)
        assert not consumer.done()
        assert response.is_closed and not frames.closed
        assert token.text_streams == 1 and token.busy
        for action in (
            lambda: client.replace_token(token.token_id, "fixture-replacement-credential"),
            lambda: client.remove_token(token.token_id),
        ):
            with pytest.raises(UpstreamManagementError) as error:
                await action()
            assert error.value.status == 409

        other_close = asyncio.create_task(response.stream.aclose())
        await asyncio.sleep(0)
        assert frames.close_calls == 1 and token.text_streams == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(consumer, 1)
        await asyncio.wait_for(other_close, 1)
        await asyncio.wait_for(response.aclose(), 1)
        await asyncio.wait_for(response.stream.aclose(), 1)
        assert frames.closed and frames.close_calls == 1
        assert token.text_streams == 0 and not token.busy
        await client.replace_token(token.token_id, "fixture-replacement-credential")
        assert await client.remove_token(token.token_id)
    finally:
        release.set()
        if not consumer.done():
            consumer.cancel()
        pending = [consumer] + ([other_close] if other_close is not None else [])
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 1)
