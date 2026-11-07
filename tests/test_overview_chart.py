"""Daily chart counts actual model images independently of weighted quotas."""
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.database import Database


@pytest.mark.parametrize("timezone,day", [
    ("Asia/Shanghai", "2026-10-31"),
    ("America/Los_Angeles", "2026-03-08"),
    ("America/Los_Angeles", "2026-11-01"),
])
def test_chart_model_counts_use_successful_images_and_local_days(tmp_path, timezone, day):
    async def run():
        db = Database(str(tmp_path / "chart.db"), timezone)
        await db.connect()
        try:
            start = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(timezone))
            end = start + timedelta(days=1)
            yesterday = (start - timedelta(days=1)).date().isoformat()
            tomorrow = end.date().isoformat()
            await db._db.execute(
                "INSERT INTO api_keys(id,name,token,created_at,exclude_global_v5) "
                "VALUES(1,'independent','fixture',0,1)")

            async def log(model, images, *, ts=None, kind="image", status="ok"):
                await db._db.execute(
                    "INSERT INTO usage_log(ts,key_id,kind,model,status,images) VALUES(?,1,?,?,?,?)",
                    (start.timestamp() if ts is None else ts, kind, model, status, images))

            await log("nai-diffusion-4-5", 2)
            await log(" NAI-DIFFUSION-4-5-CURATED-INPAINTING ", 3, kind="image_stream")
            await log("nai-diffusion-5-full-medium", 4)
            await log("nai-diffusion-5", 1)
            await log("nai-v5", 1)
            await log("nai-v5-full", 2, kind="image_stream", ts=end.timestamp() - .001)
            for model in ("nai-diffusion-4-50", "nai-diffusion-50", "nai-v50", "other"):
                await log(model, 100)
            await log("nai-diffusion-5-full", 100, status="error", kind="image_stream")
            await log("nai-diffusion-4-5-full", 100, status="rejected")
            for kind in ("upscale", "augment", "vibe_encode", "text"):
                await log("nai-diffusion-5-full", 100, kind=kind)
            await log("nai-diffusion-5-full", 9, ts=start.timestamp() - .001)
            await log("nai-diffusion-4-5-full", 7, ts=end.timestamp())
            await db._db.commit()
            await db.bump_counters(1, day, images=14, anlas=12.5, v5=4.8)

            before = await db.overview(day, [yesterday, day, tomorrow])
            row = before["week"][1]
            assert row["v45_images"] == 5
            assert row["v5_images"] == 8  # Medium contributes four images, not 2.4 units.
            assert row["images"] == 14 and row["anlas"] == 12.5
            assert before["week"][0]["v5_images"] == 9
            assert before["week"][2]["v45_images"] == 7

            await db.reset_daily_image_quota(1, day)
            await db.delete_key(1)
            after = await db.overview(day, [yesterday, day, tomorrow])
            assert before["week"] == after["week"]
            empty = (await db.overview(day, ["2020-01-01"]))["week"][0]
            assert (empty["images"], empty["v45_images"], empty["v5_images"], empty["anlas"]) == (0, 0, 0, 0)
            assert (await db.overview(day, []))["week"] == []
        finally:
            await db.close()

    asyncio.run(run())
