"""Quota reset offsets preserve immutable usage and migrate old SQLite files."""
import asyncio
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

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
                assert dict(c) == dict(key_id=1, day='2026-09-22', images=7,
                                       legacy_free_images=0, anlas=0, v5=0,
                                       text_tokens=123, requests=9)
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


def test_legacy_free_quota_migration_backfills_successful_free_images_once(tmp_path):
    path = tmp_path / 'legacy-quota.db'
    with sqlite3.connect(path) as raw:
        raw.execute('CREATE TABLE api_keys(id INTEGER PRIMARY KEY, daily_images INTEGER NOT NULL, is_admin INTEGER NOT NULL)')
        raw.executemany('INSERT INTO api_keys VALUES(?,?,?)', [(1, 0, 0), (2, 25, 0), (3, 0, 1)])
        raw.execute('CREATE TABLE counters(key_id INTEGER, day TEXT, images INTEGER DEFAULT 0, anlas REAL DEFAULT 0, text_tokens INTEGER DEFAULT 0, requests INTEGER DEFAULT 0, v5 INTEGER DEFAULT 0, PRIMARY KEY(key_id, day))')
        raw.executemany('INSERT INTO counters(key_id,day,images) VALUES(?,?,?)',
                        [(1, '2026-09-22', 4), (1, '2026-09-23', 4),
                         (2, '2026-09-23', 1), (3, '2026-09-23', 1)])
        raw.execute('CREATE TABLE usage_log(key_id INTEGER, ts REAL, kind TEXT, status TEXT, anlas REAL, images INTEGER, model TEXT)')
        ts = datetime(2026, 9, 23, 0, 30, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        raw.executemany('INSERT INTO usage_log VALUES(?,?,?,?,?,?,?)', [
            (1, ts, 'image', 'ok', 0, 2, 'nai-diffusion-4-5-full'),
            (1, ts, 'image_stream', 'ok', 0, 1, 'nai-diffusion-4-full'),
            (1, ts, 'image', 'ok', 8, 1, 'nai-diffusion-4-5-full'),
            (1, ts, 'image', 'error', 0, 1, 'nai-diffusion-4-5-full'),
            (1, ts, 'image', 'ok', 0, 1, 'nai-diffusion-5-full'),
            (2, ts, 'image', 'ok', 0, 1, 'nai-diffusion-4-5-full'),
            (3, ts, 'image', 'ok', 0, 1, 'nai-diffusion-4-5-full'),
            (1, ts - 86400, 'image', 'ok', 0, 4, 'nai-diffusion-4-5-full'),
        ])

    async def run():
        for _ in range(2):
            db = Database(str(path), 'Asia/Shanghai')
            await db.connect()
            try:
                keys = await (await db._db.execute('SELECT id,daily_images FROM api_keys ORDER BY id')).fetchall()
                assert [(row['id'], row['daily_images']) for row in keys] == [(1, 100), (2, 25), (3, 0)]
                assert (await db.get_counter(1, '2026-09-23'))['legacy_free_images'] == 3
                assert (await db.get_counter(1, '2026-09-22'))['legacy_free_images'] == 4
                assert (await db.get_counter(2, '2026-09-23'))['legacy_free_images'] == 1
            finally:
                await db.close()
    asyncio.run(run())
