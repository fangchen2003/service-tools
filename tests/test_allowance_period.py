import pytest

from app.allowance import AllowanceCache


class FakeDB:
    def __init__(self):
        self.kv = {}

    async def get_setting(self, k, default=None):
        return self.kv.get(k, default)

    async def set_setting(self, k, v):
        self.kv[k] = v


@pytest.mark.asyncio
async def test_recharge_uses_longest_reading_not_latest():
    # 「再恢复 1% 还剩多少秒」读在快恢复完时很小：以前用最新一次读数，算出每天 11%（实际约 5%）
    db = FakeDB()
    cache = AllowanceCache(db)
    for s in (16000, 7800, 1200):
        await cache._note_period("t1", s)
    assert cache.period("t1") == 16000
    again = AllowanceCache(db)                    # 重启后从库里读回来
    await again._note_period("t1", 900)
    assert again.period("t1") == 16000
