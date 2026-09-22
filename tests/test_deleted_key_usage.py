"""Deleting credentials must not erase settled usage or revive reset offsets."""
import asyncio
import sqlite3
import time

import pytest
import pytest_asyncio

from app.config import Settings
from app.database import Database
from app.state import GateState


DAY = "2026-09-22"


@pytest_asyncio.fixture
async def db():
    value = Database(":memory:")
    await value.connect()
    try:
        yield value
    finally:
        await value.close()


async def key(db, exempt=False):
    return await db.create_key(dict(name="fixture", token="fixture-only", daily_images=60,
        monthly_anlas=500, daily_text_tokens=1000, rpm=10, exclude_global_v5=exempt))


async def scalar(db, sql, args=()):
    return (await (await db._db.execute(sql, args)).fetchone())[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("exempt", [False, True])
@pytest.mark.parametrize("reset", [False, True])
async def test_delete_preserves_totals_and_removes_only_credentials_and_offsets(db, exempt, reset):
    row = await key(db, exempt)
    await db.bump_counters(row["id"], DAY, images=2, anlas=30, v5=2, text_tokens=50)
    await db.bump_counters(row["id"], "2026-09-21", images=1, anlas=5, v5=1)
    await db.add_log(row["id"], "fixture", "image", "fixture-model", "ok", images=2, anlas=30)
    if reset:
        await db.reset_daily_image_quota(row["id"], DAY)
    before = await db.overview(DAY, ["2026-09-21", DAY])
    await db.delete_key(row["id"])
    after = await db.overview(DAY, ["2026-09-21", DAY])
    assert await db.get_key_by_token("fixture-only") is None
    assert await db.get_key(row["id"]) is None and await db.list_keys() == []
    assert await db.month_anlas_all("2026-09") == 35
    assert await db.day_v5_total(DAY) == (0 if exempt else 2)
    assert all(before[k] == after[k] for k in ("today", "week", "month"))
    assert after["keys_total"] == after["keys_active"] == 0
    assert await scalar(db, "SELECT COUNT(*) FROM daily_quota_offsets") == 0
    assert await db.count_logs() == 1
    assert await scalar(db, "SELECT COUNT(*) FROM counters") == 2
    assert await scalar(db, "SELECT exclude_global_v5 FROM deleted_key_usage_flags WHERE key_id=?", (row["id"],)) == int(exempt)


@pytest.mark.asyncio
@pytest.mark.parametrize("exempt", [False, True])
async def test_late_settlement_repeated_delete_and_new_key_do_not_lose_usage(db, exempt):
    row = await key(db, exempt)
    await db.delete_key(row["id"])
    await db.bump_counters(row["id"], DAY, anlas=7, v5=1)
    await db.delete_key(row["id"])
    await db.reset_daily_image_quota(row["id"], DAY)
    assert await scalar(db, "SELECT COUNT(*) FROM daily_quota_offsets") == 0
    assert await db.month_anlas_all("2026-09") == 7
    assert await db.day_v5_total(DAY) == (0 if exempt else 1)
    fresh = await key(db)
    assert fresh["id"] > row["id"]
    assert (await db.get_counter(fresh["id"], DAY))["anlas"] == 0


@pytest.mark.asyncio
async def test_automatic_inactivity_cleanup_preserves_ledger(db):
    row = await key(db)
    await db._db.execute("UPDATE api_keys SET created_at=? WHERE id=?", (time.time() - 10 * 86400, row["id"]))
    await db._db.commit()
    await db.bump_counters(row["id"], DAY, anlas=12, v5=3)
    await db.reset_daily_image_quota(row["id"], DAY)
    state = GateState(Settings(key_inactivity_delete_days=3))
    state.db = db
    assert await state.delete_inactive_keys() == 1
    assert await db.month_anlas_all("2026-09") == 12
    assert await db.day_v5_total(DAY) == 3
    assert await scalar(db, "SELECT COUNT(*) FROM daily_quota_offsets") == 0


@pytest.mark.asyncio
async def test_delete_is_atomic_if_offset_cleanup_fails(db):
    row = await key(db)
    await db.bump_counters(row["id"], DAY, anlas=9, v5=1)
    await db.reset_daily_image_quota(row["id"], DAY)
    await db._db.execute("""CREATE TRIGGER fixture_abort_offset BEFORE DELETE ON daily_quota_offsets
                          BEGIN SELECT RAISE(ABORT, 'fixture abort'); END""")
    await db._db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        await db.delete_key(row["id"])
    assert await db.get_key(row["id"]) is not None
    assert await scalar(db, "SELECT COUNT(*) FROM deleted_key_usage_flags") == 0
    assert await scalar(db, "SELECT COUNT(*) FROM daily_quota_offsets") == 1
    assert await db.month_anlas_all("2026-09") == 9
    await db._db.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("reset_first", [False, True])
async def test_reset_and_delete_race_does_not_leave_orphan_offsets(db, reset_first):
    row = await key(db)
    await db.bump_counters(row["id"], DAY, anlas=5, v5=1)
    operations = [db.delete_key(row["id"]), db.reset_daily_image_quota(row["id"], DAY)]
    if reset_first:
        operations.reverse()
    await asyncio.gather(*operations)
    assert await scalar(db, "SELECT COUNT(*) FROM daily_quota_offsets") == 0
    assert await db.month_anlas_all("2026-09") == 5
    assert await db.day_v5_total(DAY) == 1


@pytest.mark.asyncio
async def test_restart_retains_archive_and_unknown_schema(tmp_path):
    path = tmp_path / "fixture.db"
    db = Database(str(path))
    await db.connect()
    try:
        row = await key(db)
        await db._db.execute("ALTER TABLE counters ADD COLUMN custom_note TEXT DEFAULT 'preserve'")
        await db._db.execute("CREATE TABLE custom_history(value TEXT)")
        await db._db.execute("INSERT INTO custom_history VALUES('preserve')")
        await db._db.commit()
        await db.bump_counters(row["id"], DAY, anlas=3, v5=1)
        await db.delete_key(row["id"])
    finally:
        await db.close()
    for _ in range(2):
        db = Database(str(path))
        await db.connect()
        try:
            assert await db.month_anlas_all("2026-09") == 3
            assert await db.day_v5_total(DAY) == 1
            assert await scalar(db, "SELECT custom_note FROM counters") == "preserve"
            assert await scalar(db, "SELECT value FROM custom_history") == "preserve"
            assert await db.get_key(row["id"]) is None
        finally:
            await db.close()
