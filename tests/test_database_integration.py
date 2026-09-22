"""Quota reset offsets preserve immutable usage and migrate old SQLite files."""
import asyncio
import sqlite3

from app.database import Database


def test_repeated_resets_preserve_month_global_and_history(tmp_path):
    async def run():
        db = Database(str(tmp_path / 'usage.db'))
        await db.connect()
        try:
            await db._db.execute("INSERT INTO api_keys(id,name,token,created_at) VALUES(1,'fake','fake',0)")
            await db._db.commit()
            await db.bump_counters(1, '2026-09-21', anlas=8, v5=1)
            await db.bump_counters(1, '2026-09-22', images=7, anlas=42.5, v5=3, text_tokens=123, requests=9)
            for _ in range(2):
                await db.reset_daily_image_quota(1, '2026-09-22')
                c = await db.get_counter(1, '2026-09-22')
                assert dict(c) == dict(key_id=1, day='2026-09-22', images=7, anlas=0, v5=0, text_tokens=123, requests=9)
            assert await db.month_anlas(1, '2026-09') == 50.5
            assert await db.month_anlas_all('2026-09') == 50.5
            assert await db.day_v5_total('2026-09-22') == 3
            overview = await db.overview('2026-09-22', ['2026-09-21', '2026-09-22'])
            assert overview['today']['anlas'] == 42.5 and overview['today']['v5'] == 3
            assert overview['month']['anlas'] == 50.5
            await db.bump_counters(1, '2026-09-22', images=1, anlas=5, v5=1)
            c = await db.get_counter(1, '2026-09-22')
            assert c['anlas'] == 5 and c['v5'] == 1 and c['images'] == 8
            await db.reset_daily_image_quota(1, '2026-09-22')
            assert (await db.get_counter(1, '2026-09-22'))['anlas'] == 0
            assert await db.month_anlas(1, '2026-09') == 55.5
            assert await db.day_v5_total('2026-09-22') == 4
            assert (await db.get_counter(1, '2026-09-21'))['anlas'] == 8
        finally:
            await db.close()
    asyncio.run(run())


def test_existing_database_migrates_without_rewriting_usage(tmp_path):
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as raw:
        raw.execute('CREATE TABLE counters(key_id INTEGER, day TEXT, images INTEGER DEFAULT 0, anlas REAL DEFAULT 0, text_tokens INTEGER DEFAULT 0, requests INTEGER DEFAULT 0, v5 INTEGER DEFAULT 0, PRIMARY KEY(key_id, day))')
        raw.execute("INSERT INTO counters VALUES(7,'2026-09-22',4,12.5,99,5,2)")
        raw.execute("ALTER TABLE counters ADD COLUMN custom_note TEXT DEFAULT 'preserve extra field'")
        raw.execute('CREATE TABLE custom_history(value TEXT)')
        raw.execute("INSERT INTO custom_history VALUES('preserve unknown table')")
    async def run():
        for iteration in range(2):
            db = Database(str(path))
            await db.connect()
            try:
                if iteration == 0:
                    assert (await db.get_counter(7, '2026-09-22'))['anlas'] == 12.5
                    await db.reset_daily_image_quota(7, '2026-09-22')
                assert (await db.get_counter(7, '2026-09-22'))['anlas'] == 0
                assert (await db.get_counter(7, '2026-09-22'))['custom_note'] == 'preserve extra field'
                assert await db.month_anlas(7, '2026-09') == 12.5
                raw = await (await db._db.execute('SELECT * FROM counters')).fetchone()
                assert raw['anlas'] == 12.5 and raw['v5'] == 2
                custom = await (await db._db.execute('SELECT value FROM custom_history')).fetchone()
                assert custom[0] == 'preserve unknown table'
            finally:
                await db.close()
    asyncio.run(run())


def test_reset_without_usage_does_not_credit_future_generation(tmp_path):
    async def run():
        db = Database(str(tmp_path / 'empty.db'))
        await db.connect()
        try:
            await db.reset_daily_image_quota(2, '2026-09-22')
            await db.bump_counters(2, '2026-09-22', anlas=5, v5=1)
            c = await db.get_counter(2, '2026-09-22')
            assert c['anlas'] == 5 and c['v5'] == 1
        finally:
            await db.close()
    asyncio.run(run())
