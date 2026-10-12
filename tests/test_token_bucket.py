"""平滑注水令牌桶机制测试：
- 零点初始底仓 30 张，彻底斩断凌晨 00:00~02:00 冲闸
- 每小时自动注水恢复 4 张（每 15 分钟恢复 1 张），全天恢复上限 96 张
- 蓄水池满额时阻断并返回准确的动态恢复等待时间
- 手动配置 Key（quota_auto == -1）不受注水限制，直通静态上限
- 超出日硬上限时严格抛出 402（保留“额度已用完”契约）
"""
import time
from unittest.mock import patch
import pytest
from app.main import GateError
from app.routers.image_routes import quota_image_check
from app.reasons import PACED


def _make_key(*, daily_images=70, quota_auto=1, is_admin=False):
    return {
        "id": 1,
        "name": "test_user",
        "daily_images": daily_images,
        "daily_v5": 0,
        "daily_anlas": 0,
        "quota_auto": quota_auto,
        "is_admin": is_admin,
        "exclude_global_v5": False,
        "allow_anlas": False,
    }


class FakeDB:
    def __init__(self, legacy_free=0):
        self.legacy_free = legacy_free

    async def get_counter(self, key_id, day):
        return {
            "legacy_free_images": self.legacy_free,
            "v5": 0,
            "anlas": 0,
        }


class FakeState:
    def __init__(self, day="2026-10-12", legacy_free=0):
        self._day = day
        self.db = FakeDB(legacy_free)
        self.image_reservations = {}
        self.guard = None

    def day(self):
        return self._day


@pytest.mark.asyncio
async def test_token_bucket_midnight_burst():
    """凌晨 00:00 初始底仓为 30 张，第 31 张出图应被拦截（429 PACED）"""
    key = _make_key(daily_images=70, quota_auto=1)
    # 模拟当天 00:00:00（0 小时）
    midnight = time.mktime(time.strptime("2026-10-12", "%Y-%m-%d"))
    with patch("time.time", return_value=midnight + 60):  # 00:01
        # 出第 30 张：允许
        state = FakeState(legacy_free=29)
        with patch("app.routers.image_routes._state", return_value=state):
            await quota_image_check(key, {"v5": 0, "anlas": 0}, legacy_free_images=1)

        # 出第 31 张：超过底仓 30 张，应拦截
        state_blocked = FakeState(legacy_free=30)
        with patch("app.routers.image_routes._state", return_value=state_blocked):
            with pytest.raises(GateError) as exc_info:
                await quota_image_check(key, {"v5": 0, "anlas": 0}, legacy_free_images=1)
            err = exc_info.value
            assert err.status == 429
            assert err.code == PACED
            assert "当前额度蓄水池已用完" in err.message
            assert "每小时自动恢复 4 张" in err.message
            assert "分钟后恢复" in err.message


@pytest.mark.asyncio
async def test_token_bucket_daytime_refill():
    """白天 10:00（10 小时），底仓 30 + 10*4 = 70 张，允许出到 70 张"""
    key = _make_key(daily_images=96, quota_auto=1)
    midnight = time.mktime(time.strptime("2026-10-12", "%Y-%m-%d"))
    # 10:00:00
    with patch("time.time", return_value=midnight + 10 * 3600):
        # 已经用了 69 张，再出 1 张（共 70 张）：放行
        state = FakeState(legacy_free=69)
        with patch("app.routers.image_routes._state", return_value=state):
            await quota_image_check(key, {"v5": 0, "anlas": 0}, legacy_free_images=1)

        # 已经用了 70 张，再出 1 张（第 71 张）：拦截
        state_blocked = FakeState(legacy_free=70)
        with patch("app.routers.image_routes._state", return_value=state_blocked):
            with pytest.raises(GateError) as exc_info:
                await quota_image_check(key, {"v5": 0, "anlas": 0}, legacy_free_images=1)
            assert exc_info.value.status == 429
            assert exc_info.value.code == PACED
            assert "约 15 分钟后恢复 1 张" in exc_info.value.message


@pytest.mark.asyncio
async def test_token_bucket_manual_key_exemption():
    """站长手动配置的 Key（quota_auto == -1）不受注水限制，直接享受全额"""
    key = _make_key(daily_images=70, quota_auto=-1)
    midnight = time.mktime(time.strptime("2026-10-12", "%Y-%m-%d"))
    # 00:01 即使只过了 1 分钟，手动 Key 也能出第 50 张（只要不超过 daily_images）
    with patch("time.time", return_value=midnight + 60):
        state = FakeState(legacy_free=49)
        with patch("app.routers.image_routes._state", return_value=state):
            await quota_image_check(key, {"v5": 0, "anlas": 0}, legacy_free_images=1)


@pytest.mark.asyncio
async def test_token_bucket_daily_hard_cap_raises_402():
    """超出每日硬上限（daily_images）时，抛出 402 且保留契约文案 '额度已用完'"""
    key = _make_key(daily_images=70, quota_auto=1)
    midnight = time.mktime(time.strptime("2026-10-12", "%Y-%m-%d"))
    # 下午 20:00，注水配额充裕，但累计使用已经达到 70 张上限
    with patch("time.time", return_value=midnight + 20 * 3600):
        state = FakeState(legacy_free=70)
        with patch("app.routers.image_routes._state", return_value=state):
            with pytest.raises(GateError) as exc_info:
                await quota_image_check(key, {"v5": 0, "anlas": 0}, legacy_free_images=1)
            assert exc_info.value.status == 402
            assert "额度已用完" in exc_info.value.message
