"""V5 全站额度豁免的行为测试。"""

import asyncio
import os
import tempfile

from app.database import Database
from app.main import quota_image_check


class FullGlobalV5Database:
    async def get_counter(self, key_id, day):
        return {"v5": 0, "anlas": 0.0}

    async def get_setting(self, key, default):
        assert key == "global_daily_v5"
        return 1

    async def day_v5_total(self, day):
        return 1


class State:
    db = FullGlobalV5Database()

    class settings:
        global_daily_v5 = 1

    @staticmethod
    def day():
        return "2026-09-03"

    @staticmethod
    def month():
        return "2026-09"


def test_v5_exempt_key_does_not_consume_or_block_on_global_v5():
    """独立 V5 Key 仅受其自己的 daily_v5 限制，不受全站日额度影响。"""
    import app.main as main

    original_state = main.STATE
    try:
        main.STATE = State()
        key = {
            "id": 3,
            "is_admin": False,
            "daily_v5": 10,
            "exclude_global_v5": True,
            "allow_anlas": False,
        }
        asyncio.run(quota_image_check(key, {"v5": 1, "anlas": 0}))
    finally:
        main.STATE = original_state


def test_v5_exempt_key_is_excluded_from_global_v5_total():
    """独立 Key 的 V5 用量保留在其个人计数器，但不累加进全站额度。"""
    async def run():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        db = Database(path)
        try:
            await db.connect()
            for key_id, exempt in ((1, False), (2, True)):
                await db._db.execute(
                    "INSERT INTO api_keys (id,name,token,created_at,exclude_global_v5) VALUES (?,?,?,?,?)",
                    (key_id, f"key-{key_id}", f"token-{key_id}", 0, int(exempt)),
                )
            await db._db.commit()
            await db.bump_counters(1, "2026-09-03", v5=1)
            await db.bump_counters(2, "2026-09-03", v5=1)
            assert await db.day_v5_total("2026-09-03") == 1
            assert (await db.get_counter(2, "2026-09-03"))["v5"] == 1
            overview = await db.overview("2026-09-03", ["2026-09-03"])
            assert overview["today"]["v5"] == 1
        finally:
            await db.close()
            os.unlink(path)
    asyncio.run(run())


def test_reset_daily_v5_and_anlas_keeps_other_usage():
    """管理员重置指定 Key 时，仅清空当天 V5 与 Anlas 配额计数。"""
    async def run():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        db = Database(path)
        try:
            await db.connect()
            await db._db.execute(
                "INSERT INTO api_keys (id,name,token,created_at) VALUES (?,?,?,?)",
                (9, "quota-user", "token-9", 0),
            )
            await db._db.commit()
            await db.bump_counters(9, "2026-09-03", images=7, anlas=42.5,
                                   text_tokens=1234, requests=9, v5=3)
            await db.reset_daily_image_quota(9, "2026-09-03")
            counter = await db.get_counter(9, "2026-09-03")
            assert counter["v5"] == 0
            assert counter["anlas"] == 0
            assert counter["images"] == 7
            assert counter["text_tokens"] == 1234
            assert counter["requests"] == 9
        finally:
            await db.close()
            os.unlink(path)
    asyncio.run(run())


if __name__ == "__main__":
    test_v5_exempt_key_does_not_consume_or_block_on_global_v5()
    test_v5_exempt_key_is_excluded_from_global_v5_total()
    test_reset_daily_v5_and_anlas_keeps_other_usage()
    print("PASS: independent V5 and per-key daily quota reset")
