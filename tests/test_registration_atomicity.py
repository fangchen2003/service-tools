"""Independent connections, fault injection and fake Discord; no live network."""
import asyncio
import sqlite3
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import aiosqlite
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.admin import COOKIE, _sign
from app.database import Database
from app.registration import RegistrationError, RegistrationService, registration_status
from app.registration_admin import router
from test_registration_configuration import DEPLOYMENT


@pytest_asyncio.fixture
async def harness(tmp_path):
    databases = [Database(str(tmp_path / "gate.sqlite")) for _ in range(2)]
    for db in databases:
        await db.connect()
    h = SimpleNamespace(dbs=databases, channel_hook=None, message_hook=None, sent=[])

    async def discord(request):
        path = request.url.path
        if path.endswith("/token"):
            return httpx.Response(200, json={"access_token": parse_qs(request.content.decode())["code"][0]})
        if path == "/api/users/@me":
            return httpx.Response(200, json={"id": request.headers["authorization"].split()[1]})
        if path.endswith("/member"):
            return httpx.Response(200, json={"roles": [DEPLOYMENT.membership_role]})
        if path.endswith("/channels"):
            if h.channel_hook:
                await h.channel_hook()
            return httpx.Response(200, json={"id": "dm"})
        if path.endswith("/messages"):
            if h.message_hook:
                await h.message_hook()
            h.sent.append(request.content)
            return httpx.Response(200, json={})
        raise AssertionError(request.url)

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(discord)) as http:
            h.services = [RegistrationService(db, http, deployment=DEPLOYMENT,
                client_id="fixture", client_secret="fixture", bot_token="fixture", bridge_secret="fixture")
                for db in databases]
            yield h
    finally:
        for db in databases:
            await db.close()


async def begin(service, user):
    return parse_qs(urlsplit(await service.begin(user, DEPLOYMENT.command_guild)).query)["state"][0]


async def counts(h):
    db = h.dbs[0]
    return tuple([(await db._db.execute_fetchall(f"SELECT COUNT(*) FROM {table}"))[0][0]
                  for table in ("api_keys", "discord_registrations")])


async def two_finishes(h, same_user=False):
    arrived = 0
    release = asyncio.Event()

    async def barrier():
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            release.set()
        await asyncio.wait_for(release.wait(), 2)

    h.channel_hook = barrier
    users = ["777", "777" if same_user else "778"]
    states = [await begin(service, user) for service, user in zip(h.services, users)]
    return await asyncio.gather(*(service.finish(user, state)
        for service, user, state in zip(h.services, users, states)), return_exceptions=True)


@pytest.mark.asyncio
async def test_daily_cap_is_database_wide_across_independent_instances(harness):
    h = harness
    await h.dbs[0].set_setting("registration_daily_limit", 1)
    results = await two_finishes(h)
    assert results.count("sent") == 1
    assert sum(isinstance(result, RegistrationError) for result in results) == 1
    assert await counts(h) == (1, 1)
    assert len(h.sent) == 1
    assert (await registration_status(h.dbs[1]))["today_count"] == 1


@pytest.mark.asyncio
async def test_other_instance_admin_pause_during_channel_creation_prevents_admission(harness):
    h = harness
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()

    h.channel_hook = hold
    app = FastAPI()
    app.include_router(router)
    app.state.registrar = h.services[1]
    app.state.gate = SimpleNamespace(db=h.dbs[1], settings=SimpleNamespace(secret_key="fixture-secret"))
    task = asyncio.create_task(h.services[0].finish("777", await begin(h.services[0], "777")))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://local") as client:
            import time
            payload = str(int(time.time()) + 3600)
            client.cookies.set(COOKIE, payload + "." + _sign("fixture-secret", payload))
            token = (await client.get("/admin/api/registration")).json()["csrf_token"]
            response = await asyncio.wait_for(client.put("/admin/api/registration",
                json={"enabled": False, "daily_limit": 0}, headers={"x-nai-admin-csrf": token}), 2)
            assert response.status_code == 200
    finally:
        release.set()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
    assert isinstance(result, RegistrationError)
    assert await counts(h) == (0, 0)
    assert not h.sent


@pytest.mark.asyncio
async def test_duplicate_is_rejected_across_independent_instances(harness):
    results = await two_finishes(harness, same_user=True)
    assert results.count("sent") == 1
    assert sum(isinstance(result, RegistrationError) for result in results) == 1
    assert await counts(harness) == (1, 1)


@pytest.mark.asyncio
async def test_archive_insert_failure_rolls_back_key_even_after_unrelated_commit(harness):
    h = harness
    await h.dbs[0]._db.execute("""CREATE TRIGGER reject_archive BEFORE INSERT ON discord_registrations
        BEGIN SELECT RAISE(ABORT, 'fixture archive failure'); END""")
    await h.dbs[0]._db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="fixture archive failure"):
        await h.services[0].finish("777", await begin(h.services[0], "777"))
    await h.dbs[0].set_setting("unrelated_fixture", "1")
    assert await counts(h) == (0, 0)
    assert not h.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("after_commit", [False, True])
async def test_issuance_commit_failure_leaves_no_key_or_archive(harness, monkeypatch, after_commit):
    h = harness
    original = aiosqlite.Connection.commit
    fired = False

    async def fail_commit(conn):
        nonlocal fired
        if not fired and (await conn.execute_fetchall("SELECT COUNT(*) FROM discord_registrations"))[0][0]:
            fired = True
            if after_commit:
                await original(conn)
            raise sqlite3.OperationalError("fixture commit failure")
        return await original(conn)

    monkeypatch.setattr(aiosqlite.Connection, "commit", fail_commit)
    with pytest.raises(sqlite3.OperationalError, match="fixture commit failure"):
        await h.services[0].finish("777", await begin(h.services[0], "777"))
    assert fired
    await h.dbs[0].set_setting("unrelated_fixture", "1")
    assert await counts(h) == (0, 0)
    assert not h.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("restoring", [False, True])
async def test_cancelled_dm_compensates_issuance_or_restore(harness, restoring):
    h = harness
    service, db = h.services[0], h.dbs[0]
    old_id = None
    if restoring:
        await service.finish("777", await begin(service, "777"))
        old_id = (await db.get_discord_registration("777"))["key_id"]
        await db.delete_key(old_id)
    entered = asyncio.Event()

    async def hold_message():
        entered.set()
        await asyncio.Event().wait()

    h.message_hook = hold_message
    operation = (service.find("777", DEPLOYMENT.command_guild) if restoring else
                 service.finish("777", await begin(service, "777")))
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(), 2)
    finally:
        task.cancel()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
    assert isinstance(result, asyncio.CancelledError)
    assert await counts(h) == (0, 1 if restoring else 0)
    if restoring:
        assert (await db.get_discord_registration("777"))["key_id"] == old_id


@pytest.mark.asyncio
@pytest.mark.parametrize("restoring", [False, True])
async def test_cancellation_after_commit_before_return_is_compensated(harness, monkeypatch, restoring):
    h = harness
    service, db = h.services[0], h.dbs[0]
    old_id = None
    if restoring:
        await service.finish("777", await begin(service, "777"))
        old_id = (await db.get_discord_registration("777"))["key_id"]
        await db.delete_key(old_id)
    entered, release = asyncio.Event(), asyncio.Event()
    original = aiosqlite.Connection.commit
    fired = False

    async def hold_commit(conn):
        nonlocal fired
        await original(conn)
        if not fired and (await conn.execute_fetchall("SELECT COUNT(*) FROM api_keys"))[0][0]:
            fired = True
            entered.set()
            await release.wait()

    monkeypatch.setattr(aiosqlite.Connection, "commit", hold_commit)
    operation = (service.find("777", DEPLOYMENT.command_guild) if restoring else
                 service.finish("777", await begin(service, "777")))
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
    finally:
        release.set()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
    assert isinstance(result, asyncio.CancelledError)
    assert await counts(h) == (0, 1 if restoring else 0)
    if restoring:
        assert (await db.get_discord_registration("777"))["key_id"] == old_id


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_compensation(harness, monkeypatch):
    h = harness
    service, db = h.services[0], h.dbs[0]
    dm_entered, cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hold_message():
        dm_entered.set()
        await asyncio.Event().wait()

    original = db.rollback_discord_registration

    async def hold_cleanup(*args):
        cleanup_entered.set()
        await cleanup_release.wait()
        await original(*args)

    monkeypatch.setattr(db, "rollback_discord_registration", hold_cleanup)
    h.message_hook = hold_message
    task = asyncio.create_task(service.finish("777", await begin(service, "777")))
    try:
        await asyncio.wait_for(dm_entered.wait(), 2)
        task.cancel()
        await asyncio.wait_for(cleanup_entered.wait(), 2)
        task.cancel()
    finally:
        cleanup_release.set()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
    assert isinstance(result, asyncio.CancelledError)
    assert await counts(h) == (0, 0)


@pytest.mark.asyncio
async def test_discord_http_does_not_hold_sqlite_writer_lock(harness):
    h = harness
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_message():
        entered.set()
        await release.wait()

    h.message_hook = hold_message
    task = asyncio.create_task(h.services[0].finish("777", await begin(h.services[0], "777")))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(h.dbs[1].set_setting("unrelated_accounting_fixture", "1"), 1)
        assert await counts(h) == (1, 1)
    finally:
        release.set()
        assert await task == "sent"


@pytest.mark.asyncio
async def test_other_instance_policy_write_serializes_with_isolated_issuance(harness, monkeypatch):
    h = harness
    service, db = h.services[0], h.dbs[0]
    inserted, release = asyncio.Event(), asyncio.Event()
    original = db._insert_key

    async def hold_insert(*args):
        row = await original(*args)
        inserted.set()
        await release.wait()
        return row

    monkeypatch.setattr(db, "_insert_key", hold_insert)
    app = FastAPI()
    app.include_router(router)
    app.state.registrar = h.services[1]
    app.state.gate = SimpleNamespace(db=h.dbs[1], settings=SimpleNamespace(secret_key="fixture-secret"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://local") as client:
        import time
        payload = str(int(time.time()) + 3600)
        client.cookies.set(COOKIE, payload + "." + _sign("fixture-secret", payload))
        token = (await client.get("/admin/api/registration")).json()["csrf_token"]
        finish = asyncio.create_task(service.finish("777", await begin(service, "777")))
        update = None
        response = None
        try:
            await asyncio.wait_for(inserted.wait(), 2)
            # A shared connection commit must not publish half the isolated pair.
            await db._db.commit()
            assert await counts(h) == (0, 0)
            update = asyncio.create_task(client.put("/admin/api/registration",
                json={"enabled": False, "daily_limit": 1}, headers={"x-nai-admin-csrf": token}))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(update), 0.1)
        finally:
            release.set()
            assert await asyncio.wait_for(finish, 2) == "sent"
            if update is not None:
                response = await asyncio.wait_for(update, 2)
        assert response is not None
        assert response.status_code == 200
        assert not response.json()["enabled"]
        assert response.json()["today_count"] == 1
        assert await counts(h) == (1, 1)


@pytest.mark.asyncio
async def test_cancelling_active_resend_preserves_existing_key(harness):
    h = harness
    service, db = h.services[0], h.dbs[0]
    await service.finish("777", await begin(service, "777"))
    before = dict(await db.get_discord_registration("777"))
    entered = asyncio.Event()

    async def hold_message():
        entered.set()
        await asyncio.Event().wait()

    h.message_hook = hold_message
    task = asyncio.create_task(service.find("777", DEPLOYMENT.command_guild))
    try:
        await asyncio.wait_for(entered.wait(), 2)
    finally:
        task.cancel()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
    assert isinstance(result, asyncio.CancelledError)
    assert dict(await db.get_discord_registration("777")) == before
    assert await counts(h) == (1, 1)


@pytest.mark.asyncio
async def test_cleanup_has_a_bounded_timeout(harness, monkeypatch):
    from app import registration
    h = harness
    cancelled = asyncio.Event()

    async def failed_message():
        raise RegistrationError("fixture DM failure")

    async def stuck_cleanup(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(registration, "CLEANUP_TIMEOUT", 0.02)
    monkeypatch.setattr(h.dbs[0], "rollback_discord_registration", stuck_cleanup)
    h.message_hook = failed_message
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(h.services[0].finish("777", await begin(h.services[0], "777")), 1)
    assert cancelled.is_set()
