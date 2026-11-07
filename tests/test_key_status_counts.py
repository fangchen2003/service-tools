"""Expired credentials remain manageable but do not count as current Keys."""
import asyncio

from app.database import Database


def test_overview_counts_only_unexpired_keys_without_deleting_expired(tmp_path, monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr('app.database.time.time', lambda: now)

    async def run():
        db = Database(str(tmp_path / 'status.db'))
        await db.connect()
        try:
            # Each policy applies whether the administrative switch is on or off.
            for index, expiry in enumerate((None, now + 1, now - 1, now)):
                for enabled in (True, False):
                    row = await db.create_key(dict(name=f'fixture-{index}-{enabled}',
                        token=f'fixture-{index}-{enabled}', expires_at=expiry,
                        daily_images=100, monthly_anlas=0, daily_text_tokens=1000, rpm=10))
                    await db.update_key(row['id'], {'enabled': enabled})
            result = await db.overview('2026-09-22', ['2026-09-22'])
            assert result['keys_total'] == 4  # permanent + future, including disabled
            assert result['keys_active'] == 2
            assert len(await db.list_keys()) == 8  # expired records are not deleted
        finally:
            await db.close()
    asyncio.run(run())
