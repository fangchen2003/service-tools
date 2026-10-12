"""公平分配（10-11 站长同意，10-13 上线）：
1. 账号每日上限按时段放行（令牌桶）：0 点放出 burst 张，之后每小时补 daily/24，没用完的留到后面。
2. 保底按前一天实际需求做最大最小公平分配（water-filling）。
3. 超出保底的人只能用「已放出、还剩得多」的份额。"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.guard import Guard
from app.quota_algo import FAIR_SHARE, water_fill
from app.reasons import PACED, SITE_LEVEL, code_of

TZ = ZoneInfo("Asia/Shanghai")


def _at(h, m=0):
    return datetime(2026, 10, 13, h, m, tzinfo=TZ).timestamp()


class _DB:
    def __init__(self, used):
        self.used = used

    async def get_upstream_counter(self, token_id, day):
        return {"images": int(self.used), "v5": 0, "units": float(self.used)}


def _guard(**kw):
    g = Guard()
    g.values.update({"account_daily_cap": 1500, "account_hourly_cap": 160, "daily_burst": 150, **kw})
    return g


def test_allowance_schedule():
    g = _guard()
    assert g.paced_allowance(_at(0)) == 150
    assert g.paced_allowance(_at(12)) == 150 + 750
    assert g.paced_allowance(_at(23, 59)) == 1500                # 当天总量不超过每日上限


@pytest.mark.asyncio
async def test_paced_block_and_wait():
    g = _guard()
    now = _at(10)                                                 # 已放出 150 + 625 = 775
    assert await g.token_block_reason(_DB(774), "t", "d", now) is None
    r = await g.token_block_reason(_DB(775), "t", "d", now)
    assert code_of(r) == PACED and "分钟后" in r
    assert g.minutes_until_paced(775, now) == 1                  # 每 57.6 秒补 1 张
    r2 = await g.token_block_reason(_DB(1500), "t", "d", now)
    assert "明天 0 点" in r2                                      # 每日上限仍优先


@pytest.mark.asyncio
async def test_unused_morning_carries_to_evening():
    g = _guard()
    # 白天几乎没人用，晚上 20 点可以一下用掉已放出的量（仍受每小时上限约束）
    assert await g.token_block_reason(_DB(300), "t", "d", _at(20)) is None
    assert g.paced_allowance(_at(20)) - 300 > 900


@pytest.mark.asyncio
async def test_burst_zero_restores_old_first_come_behaviour():
    g = _guard(daily_burst=0)
    assert await g.token_block_reason(_DB(1499), "t", "d", _at(1)) is None
    assert await g.paced_headroom(_DB(1499), ["t"], "d", _at(1)) == float("inf")


@pytest.mark.asyncio
async def test_headroom_sums_accounts():
    g = _guard()
    assert await g.paced_headroom(_DB(700), ["a", "b"], "d", _at(10)) == pytest.approx(2 * 75)


def test_paced_is_site_level():
    assert PACED in SITE_LEVEL                                    # 自动驾驶不能把它算成某个成员死循环


def test_water_fill_max_min_fair():
    assert water_fill([], 900) == 0
    assert water_fill([5, 5, 5], 900) == 5                        # 需求不到预算：人人满足
    assert water_fill([10, 100, 100], 110) == 50                  # 小用户拿足 10，剩 100 两人平分
    assert water_fill([1, 2, 300, 300], 103) == 50
    # 10-10 实际分布的形状：少数人用得多，大多数人只用几张
    usage = [145, 136, 110, 100, 60] + [8] * 40 + [2] * 20
    b = water_fill(usage, 1500 * FAIR_SHARE)
    assert sum(min(u, b) for u in usage) >= 900 > sum(min(u, b - 1) for u in usage) - 5


@pytest.mark.asyncio
async def test_daily_review_sets_fair_base(tmp_path, monkeypatch):
    import time as _t
    from datetime import timedelta
    from types import SimpleNamespace

    from app import quota_algo
    from app.database import Database

    db = Database(str(tmp_path / "f.sqlite"))
    await db.connect()
    try:
        base = {"daily_images": 125, "daily_anlas": 0, "daily_v5": 6, "monthly_anlas": 0, "daily_text_tokens": 0,
                "rpm": 10, "allow_anlas": False, "allow_img2img": False, "exclude_global_v5": False}
        now = _t.time()
        yday = (datetime.fromtimestamp(now) - timedelta(days=1)).strftime("%Y-%m-%d")
        usage = [145, 136, 110, 100, 60] + [8] * 40
        for i, u in enumerate(usage):
            k = await db.create_key({"name": f"m{i}", "token": f"nai-{i}", "image_model_scope": "all", **base})
            await db._db.execute("INSERT INTO counters (key_id, day, images, legacy_free_images) VALUES (?,?,?,?)",
                                 (k["id"], yday, u, u))
        await db._db.commit()
        monkeypatch.setattr(quota_algo, "day_coverage", lambda db, day: _async(24.0))
        guard = Guard(db)
        guard.values["account_daily_cap"] = 1500
        await db.set_settings_bulk({"quota_ceiling": 125, "quota_base_now": 90})
        st = SimpleNamespace(db=db, guard=guard, nai=SimpleNamespace(pool=[SimpleNamespace(usable=True)], allowance=None))
        r = await quota_algo.run(st, now=now)
        # 极简模式：昨天共 871 张，远没用完 1500，有人用满 125 → 放宽一步到 150；上限 = 保底 = N
        assert r["base"] == r["ceiling"] == 150 == guard.values["base_daily_images"]
        assert any("放宽" in x for x in r["review"]["reasons"])
    finally:
        await db.close()


async def _async(v):
    return v


def test_review_signals_count_paced_and_not_headroom():
    import inspect
    from app import main, quota_algo
    src = inspect.getsource(quota_algo._yesterday)
    assert "%按时段放行%" not in src and "%出图总量已达上限%" not in src  # 总量本来就这么多：不算「每人给多了」
    m = inspect.getsource(main.quota_image_check)
    headroom_msg = m.split("paced_headroom", 1)[1]
    assert "其他人在排队" not in headroom_msg.split("raise", 2)[1]  # 份额紧的拦截不算「有余量还被拦」


def test_over_base_rejections_are_site_level():
    from app.reasons import OVER_BASE, LABELS
    assert OVER_BASE in SITE_LEVEL and OVER_BASE in LABELS      # 忙时自动重试不能被 key_guard 当成死循环暂停



@pytest.mark.asyncio
async def test_daily_cap_falls_back_on_upstream_signals():
    # 2026-10-11 站长：日上限试探到 1700，上游 1 小时 ≥3 次 429 或连续 403 → 自动退回 1500，只降不升
    g = _guard(account_daily_cap=1700, daily_cap_fallback=1500)
    now = _at(12)
    assert g.note_upstream_429(now) == 1 and g.note_upstream_429(now + 10) == 1   # 同一次限流的并发 429 只算一次
    assert g.note_upstream_429(now + 70) == 2
    assert g.note_upstream_429(now + 4000) == 1                       # 1 小时外的不算
    assert await g.fall_back_daily("test") == (1700, 1500)
    assert g.values["account_daily_cap"] == 1500
    assert await g.fall_back_daily("again") is None                   # 已在安全档：不再动
    g2 = _guard(account_daily_cap=1200, daily_cap_fallback=1500)
    assert await g2.fall_back_daily("x") is None                      # 只降不升


@pytest.mark.asyncio
async def test_simple_mode_one_number_for_everyone(tmp_path, monkeypatch):
    import time as _t
    from datetime import timedelta
    from types import SimpleNamespace
    from app import quota_algo
    from app.database import Database
    db = Database(str(tmp_path / "s.sqlite"))
    await db.connect()
    try:
        base = {"daily_images": 125, "daily_anlas": 0, "daily_v5": 6, "monthly_anlas": 0, "daily_text_tokens": 0,
                "rpm": 10, "allow_anlas": False, "allow_img2img": False, "exclude_global_v5": False}
        now = _t.time()
        yday = (datetime.fromtimestamp(now) - timedelta(days=1)).strftime("%Y-%m-%d")
        usage = [300, 200, 150] + [5] * 60
        for i, u in enumerate(usage):
            k = await db.create_key({"name": f"m{i}", "token": f"nai-s{i}", "image_model_scope": "all", **base})
            await db._db.execute("INSERT INTO counters (key_id, day, images, legacy_free_images) VALUES (?,?,?,?)",
                                 (k["id"], yday, u, u))
        await db._db.commit()
        await db.set_settings_bulk({"global_daily_v5": 200})
        monkeypatch.setattr(quota_algo, "day_coverage", lambda db, day: _async(24.0))
        guard = Guard(db)
        guard.values["account_daily_cap"] = 1000
        st = SimpleNamespace(db=db, guard=guard, nai=SimpleNamespace(pool=[SimpleNamespace(usable=True)], allowance=None))
        r = await quota_algo.run(st, now=now)
        n = water_fill(usage, 1000 - 200)
        assert r["ceiling"] == r["base"] == n == guard.values["base_daily_images"]
        rows = await db._db.execute_fetchall("SELECT DISTINCT daily_images FROM api_keys WHERE quota_auto=1")
        assert [x[0] for x in rows] == [n]                              # 所有人同一个数
        assert quota_algo.BORROW["open"] is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_live_reports_paced_block():
    """回归（10-11）：按时段放行拦下全站时，首页还显示「运行正常 · 发了就画」。live 要带上拦截原因。"""
    from types import SimpleNamespace
    from app import live
    g = _guard()
    tok = SimpleNamespace(token_id="t", usable=True, disabled=False, admin_enabled=True, blocked_until=0, image_next_at=0)

    class DB(_DB):
        async def get_setting(self, k, d=None): return d
        async def day_v5_total(self, day): return 0

    def state(used):
        return SimpleNamespace(db=DB(used), guard=g, nai=SimpleNamespace(pool=[tok]), day=lambda: "d",
                               settings=SimpleNamespace(global_daily_v5=0, image_min_interval=15),
                               image_cooldown_remaining=lambda: 0)
    live._cache.update(at=0, body=None)
    blocked = (await live.build(state(900), None, now=_at(10)))["guard"]["blocked"]
    assert blocked["code"] == PACED and "分钟后" in blocked["text"]
    live._cache.update(at=0, body=None)
    assert (await live.build(state(100), None, now=_at(10)))["guard"]["blocked"] is None
    live._cache.update(at=0, body=None)
