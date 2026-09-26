"""A completed request's log and quota must commit or roll back together."""
import asyncio
import sqlite3
from types import SimpleNamespace

import aiosqlite
import pytest
import pytest_asyncio

from app import main
from app.database import Database


DAY = "2026-09-25"


@pytest_asyncio.fixture
async def ledger(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "ledger.db"))
    await db.connect()
    key = await db.create_key(dict(name="fixture", token="fixture", daily_images=100,
                                   monthly_anlas=100, daily_text_tokens=1000, rpm=10))
    monkeypatch.setattr(main, "STATE", SimpleNamespace(db=db, day=lambda: DAY))
    try:
        yield db, key
    finally:
        await db.close()


async def snapshot(db):
    return {table: [tuple(row) for row in await (await db._db.execute(
        f"SELECT * FROM {table}")).fetchall()]
        for table in ("usage_log", "counters", "api_keys")}


@pytest.mark.asyncio
async def test_success_accumulates_usage_and_failures_only_add_logs(ledger):
    db, key = ledger
    await main.record(key, "image_stream", "fixture", "ok", images=2, anlas=7,
                      v5=1, legacy_free_images=1, detail="completed")
    await main.record(key, "text", "fixture", "ok", tokens=11)
    await main.record(key, "image", "fixture", "error", unconfirmed_anlas=3)
    counter = await db.get_counter(key["id"], DAY)
    assert counter == dict(key_id=key["id"], day=DAY, images=2, anlas=7,
                           v5=1, legacy_free_images=1, text_tokens=11, requests=2)
    assert (await db.get_key(key["id"]))["last_used_at"] is not None
    assert await db.count_logs() == 3
    totals = await db.reconciliation_totals()
    assert totals["anlas"] == 7 and totals["unconfirmed_anlas"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("event,table", [
    ("INSERT", "usage_log"), ("INSERT", "counters"),
    ("UPDATE", "counters"), ("UPDATE", "api_keys"),
])
async def test_failed_write_leaves_no_partial_usage(ledger, event, table):
    db, key = ledger
    await main.record(key, "image", "fixture", "ok", images=1, anlas=2)
    before = await snapshot(db)
    await db._db.execute(f"""CREATE TRIGGER fixture_fail BEFORE {event} ON {table}
                            BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END""")
    await db._db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="fixture write failure"):
        await main.record(key, "image", "fixture", "ok", images=1, anlas=5, v5=1)
    # An unrelated later commit must not resurrect any part of the failed record.
    await db._db.commit()
    assert await snapshot(db) == before
    await db._db.execute("DROP TRIGGER fixture_fail")
    await db._db.commit()
    await main.record(key, "image", "fixture", "ok", images=1, anlas=3)
    assert (await db.get_counter(key["id"], DAY))["anlas"] == 5
    assert await db.count_logs() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_other_request_cannot_commit_an_unfinished_record(ledger, monkeypatch, cancel):
    db, key = ledger
    entered, release = asyncio.Event(), asyncio.Event()
    original = aiosqlite.Connection.execute

    async def execute(connection, sql, parameters=None):
        cursor = await original(connection, sql, parameters)
        if "INSERT INTO usage_log" in sql:
            entered.set()
            await release.wait()
        return cursor

    monkeypatch.setattr(aiosqlite.Connection, "execute", execute)
    pending = main.record(key, "image", "fixture", "ok", images=1, anlas=4)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await db._db.commit()
        with sqlite3.connect(db.path) as observer:
            assert observer.execute("SELECT COUNT(*) FROM usage_log").fetchone()[0] == 0
            assert observer.execute("SELECT COUNT(*) FROM counters").fetchone()[0] == 0
    finally:
        if cancel:
            pending.cancel()
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 2)
        else:
            await asyncio.wait_for(pending, 2)
    assert await db.count_logs() == (0 if cancel else 1)
    assert (await db.get_counter(key["id"], DAY))["anlas"] == (0 if cancel else 4)


@pytest.mark.asyncio
async def test_concurrent_records_all_persist_once(ledger):
    db, key = ledger
    await asyncio.gather(*(main.record(key, "image", "fixture", "ok", images=1,
                                       anlas=2, v5=1) for _ in range(8)))
    counter = await db.get_counter(key["id"], DAY)
    assert counter["images"] == counter["requests"] == counter["v5"] == 8
    assert counter["anlas"] == 16 and await db.count_logs() == 8
