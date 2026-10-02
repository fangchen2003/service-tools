"""Per-Key lifetime generation count excludes failed requests and image tools."""

import asyncio

from app.database import Database


def test_generated_image_totals_are_historical_and_generation_only(tmp_path):
    async def run():
        db = Database(str(tmp_path / "usage.db"))
        await db.connect()
        try:
            for key_id in (1, 2):
                await db._db.execute(
                    "INSERT INTO api_keys(id,name,token,created_at) VALUES(?,?,?,0)",
                    (key_id, f"user-{key_id}", f"key-{key_id}"),
                )
            await db._db.commit()
            await db.add_log(1, "user-1", "image", "model", "ok", images=2)
            await db.add_log(1, "user-1", "image_stream", "model", "ok", images=1)
            await db.add_log(1, "user-1", "image_stream", "model", "error", images=2)
            await db.add_log(1, "user-1", "upscale", "model", "ok", images=1)
            await db.add_log(2, "user-2", "image", "model", "ok", images=4)
            await db.reset_daily_image_quota(1, "2026-10-01")

            assert await db.generated_image_totals() == {1: 3, 2: 4}
            assert await db.generated_image_totals(1) == {1: 3}
            assert await db.generated_image_totals(99) == {}

            await db.delete_key(1)
            assert await db.generated_image_totals(1) == {1: 3}
        finally:
            await db.close()

    asyncio.run(run())
