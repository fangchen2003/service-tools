"""动态额度算法：V5 按恢复速度分配、V4.5 上限 / 保底每日微调、只管理 quota_auto=1 的成员 Key。"""
import json
import time
from types import SimpleNamespace

import pytest

from app import quota_algo
from app.database import Database
from app.guard import Guard

CFG = dict(quota_algo.DEFAULTS)


def test_v5_plan_spends_surplus_and_tightens_when_low():
    full = quota_algo.v5_plan(98, 11.0, active=14)
    assert full["global"] == int(11 * 14.2 * 1.3) and full["each"] == full["global"] // 14
    low = quota_algo.v5_plan(15, 11.0, active=14)
    assert low["global"] < full["global"] and low["each"] >= 3            # 剩得少就收紧，但不低于下限
    few = quota_algo.v5_plan(98, 11.0, active=2)
    assert few["people"] == 10 and few["each"] <= 30                       # 至少按 10 人算，单人不超过 30
    assert quota_algo.v5_plan(None, None, 14)["rate"] == 5.0               # 拿不到读数按保守的 5%/天（原来的 11 是错误读数）


def test_daily_adjust_rules():
    # 拥挤：上限和保底都下调，保底不高于上限
    a, b, why = quota_algo.daily_adjust(150, 100, used=900, cap=1000, hourly_blocks=0, ceiling_hits=0, base_blocks=0, cfg=CFG)
    assert (a, b) == (125, 90) and "拥挤" in why[0]
    # 3 个不同小时被每小时上限拦过才算拥挤；只在 1～2 个小时扎堆（几分钟内的连拦）不下调
    a, b, _ = quota_algo.daily_adjust(150, 100, used=100, cap=1000, hourly_blocks=3, ceiling_hits=3, base_blocks=0, cfg=CFG)
    assert (a, b) == (125, 90)
    a, b, why = quota_algo.daily_adjust(150, 100, used=200, cap=1000, hourly_blocks=1, ceiling_hits=0, base_blocks=0, cfg=CFG)
    assert (a, b) == (150, 100) and "无需调整" in why[0]
    # 有余量且有人顶格：放宽上限；有人被保底拦：抬保底
    a, b, _ = quota_algo.daily_adjust(150, 100, used=300, cap=1000, hourly_blocks=0, ceiling_hits=2, base_blocks=5, cfg=CFG)
    assert (a, b) == (175, 110)
    # 上下限
    a, b, _ = quota_algo.daily_adjust(300, 100, used=300, cap=1000, hourly_blocks=0, ceiling_hits=2, base_blocks=0, cfg=CFG)
    assert a == 300
    a, b, why = quota_algo.daily_adjust(150, 100, used=700, cap=1000, hourly_blocks=0, ceiling_hits=0, base_blocks=0, cfg=CFG)
    assert (a, b) == (150, 100) and "无需调整" in why[0]


@pytest.mark.asyncio
async def test_run_applies_same_quota_to_auto_members_only(tmp_path):
    db = Database(str(tmp_path / "g.sqlite"))
    await db.connect()
    try:
        base = {"daily_images": 300, "daily_anlas": 0, "daily_v5": 15, "monthly_anlas": 0, "daily_text_tokens": 0,
                "rpm": 10, "allow_anlas": False, "allow_img2img": False, "exclude_global_v5": False}
        old = await db.create_key({"name": "老成员", "token": "nai-old", "image_model_scope": "all", **base})
        new = await db.create_key({"name": "新成员", "token": "nai-new", "image_model_scope": "legacy", **{**base, "daily_images": 150, "daily_v5": 0}})
        manual = await db.create_key({"name": "手动", "token": "nai-man", "image_model_scope": "legacy", **base})
        test = await db.create_key({"name": "测试", "token": "nai-test", "image_model_scope": "legacy", **base})
        await db._db.execute("UPDATE api_keys SET quota_auto=-1 WHERE id=?", (manual["id"],))
        await db._db.execute("UPDATE api_keys SET is_test=1 WHERE id=?", (test["id"],))
        await db._db.commit()
        guard = Guard(db)

        class Allow:
            async def snapshot(self, pool):
                return {"accounts": [{"percent": 98, "recharge_per_day": 11.0}]}
        st = SimpleNamespace(db=db, guard=guard, nai=SimpleNamespace(pool=[SimpleNamespace(usable=True)], allowance=Allow()))
        r = await quota_algo.run(st, now=time.time())
        assert r["ceiling"] == r["base"] == 150 and r["review"] is not None      # 极简模式：每人一个数，没有保底 / 上限两层
        for k in (old, new):
            row = await db.get_key(k["id"])
            assert row["daily_images"] == 150 and row["daily_v5"] == r["v5"]["each"] and row["image_model_scope"] == "all"
        assert (await db.get_key(manual["id"]))["image_model_scope"] == "legacy"       # 手动的不覆盖
        assert (await db.get_key(test["id"]))["daily_v5"] == 15                        # 测试 Key 不参与
        assert int(await db.get_setting("global_daily_v5", 0)) == r["v5"]["global"]
        assert await db.get_setting("register_image_scope", "") == "all"
        # 同一天第二次运行不再做每日微调
        assert (await quota_algo.run(st, now=time.time()))["review"] is None
        assert len(json.loads(await db.get_setting(quota_algo.HISTORY_KEY, "[]"))) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_manual_v5_is_exact_and_pinned_rows_are_cleared(tmp_path):
    # 2026-10-11：手动 Key 填多少就是多少；旧的 v5_pinned（「不低于普通成员」）启动时清掉，算法不再改手动 Key
    db = Database(str(tmp_path / "p.sqlite"))
    await db.connect()
    try:
        base = {"daily_images": 150, "daily_anlas": 0, "daily_v5": 5, "monthly_anlas": 0, "daily_text_tokens": 0,
                "rpm": 10, "allow_anlas": True, "allow_img2img": False, "exclude_global_v5": True, "image_model_scope": "all"}
        low = await db.create_key({"name": "手动-低", "token": "nai-low", **base})
        await db._db.execute("UPDATE api_keys SET quota_auto=-1, v5_pinned=5 WHERE id=?", (low["id"],))
        await db._db.commit()
        await db.close()
        db = Database(str(tmp_path / "p.sqlite"))
        await db.connect()                                                    # 重启：清掉旧基础值
        assert (await db.get_key(low["id"]))["v5_pinned"] is None

        class Allow:
            async def snapshot(self, pool):
                return {"accounts": [{"percent": 98, "recharge_per_day": 11.0}]}
        st = SimpleNamespace(db=db, guard=Guard(db), announcer=None,
                             nai=SimpleNamespace(pool=[SimpleNamespace(usable=True)], allowance=Allow()))
        each = (await quota_algo.run(st))["v5"]["each"]
        assert each > 5 and (await db.get_key(low["id"]))["daily_v5"] == 5   # 不再被抬到普通成员的值
    finally:
        await db.close()


def test_daily_adjust_paced_never_ratchets_ceiling_down():
    # 按时段放行后每天用量都接近 100%：上限 A 不能每天 −25 直到等于保底（Sonnet 审查 P1）
    a, b = 125, 30
    for _ in range(10):
        a, b, why = quota_algo.daily_adjust(a, b, used=1500, cap=1500, hourly_blocks=5, ceiling_hits=0,
                                            base_blocks=0, cfg=CFG, paced=True)
    assert (a, b) == (125, 30) and "上限不变" in why[0]
    a, _, why = quota_algo.daily_adjust(125, 30, used=500, cap=1500, hourly_blocks=0, ceiling_hits=3,
                                        base_blocks=0, cfg=CFG, paced=True)
    assert a == 150


@pytest.mark.asyncio
async def test_daily_account_cap_counts_compute_units(tmp_path):
    # 节约模式的图算半张：1000 张上限下，2 张 14 步的图只占 1 份
    db = Database(str(tmp_path / "u.sqlite"))
    await db.connect()
    try:
        guard = Guard(db)
        await guard.save({"account_daily_cap": 2})
        await db.bump_upstream_image_counter("tok", "2026-10-10", 2, 0.5)
        c = await db.get_upstream_counter("tok", "2026-10-10")
        assert c["images"] == 2 and c["units"] == 1.0
        assert await guard.token_block_reason(db, "tok", "2026-10-10") is None
        await db.bump_upstream_image_counter("tok", "2026-10-10", 1)
        assert "已达上限" in (await guard.token_block_reason(db, "tok", "2026-10-10") or "")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_yesterday_usage_uses_compute_units(tmp_path):
    # 节约模式的一天：成员出了 1000 张、折算 500 份——每日微调要按 500 判断拥挤，不能把大家的上限砍掉
    db = Database(str(tmp_path / "y.sqlite"))
    await db.connect()
    try:
        k = await db.create_key({"name": "m", "token": "nai-y", "daily_images": 150, "daily_anlas": 0, "daily_v5": 0,
                                 "monthly_anlas": 0, "daily_text_tokens": 0, "rpm": 10, "allow_anlas": False,
                                 "allow_img2img": False, "exclude_global_v5": False, "image_model_scope": "all"})
        await db.bump_counters(k["id"], "2026-10-09", images=1000)
        await db.bump_upstream_image_counter("tok", "2026-10-09", 1000, 0.5)
        stats = await quota_algo._yesterday(db, "2026-10-09", [k["id"]])
        assert stats["used"] == 500
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_displayed_v5_percent_is_live_not_morning_plan(tmp_path):
    db = Database(str(tmp_path / "pct.sqlite"))
    await db.connect()
    try:
        pct = {"v": 97}

        class Allow:
            async def snapshot(self, pool):
                return {"accounts": [{"percent": pct["v"], "recharge_per_day": 11.0}]}
        st = SimpleNamespace(db=db, guard=Guard(db), nai=SimpleNamespace(pool=[SimpleNamespace(usable=True)], allowance=Allow()))
        first = (await quota_algo.run(st))["v5"]
        pct["v"] = 92
        later = (await quota_algo.run(st))["v5"]
        assert later["percent"] == 92 and later["each"] == first["each"]      # 显示实时剩余，每人额度当天不变
        assert json.loads(await db.get_setting(quota_algo.V5_DAY_KEY, "{}"))["plan"]["percent"] == 97
    finally:
        await db.close()


def test_choose_rate_guards_unverified_and_mismatched_readings():
    # 10/10：接口单次读数 11%/天，实测约 5%——没核对过的读数不能直接拿来分额度
    r, info = quota_algo.choose_rate(11.0, None)
    assert r == 5.0 and info["status"] == "未核对"
    r, info = quota_algo.choose_rate(11.0, 5.2)            # 对不上：取较小的，并标出来
    assert r == 5.2 and info["status"] == "对不上"
    r, info = quota_algo.choose_rate(5.5, 5.0)             # 差不多：已核对
    assert r == 5.0 and info["status"] == "已核对"
    r, info = quota_algo.choose_rate(60.0, None)           # 超出合理范围：不用
    assert r == 5.0 and info["api"] is None
    r, info = quota_algo.choose_rate(None, -3.0)           # 实测是负数（比如当天账号被别处用了）：不用
    assert r == 5.0 and info["measured"] is None


@pytest.mark.asyncio
async def test_quiet_recovery_uses_only_fresh_snapshots_without_v5_usage(tmp_path):
    # 夜里没人用 V5 时，两次「主动读」之间剩余的上涨 = 纯恢复；旧缓存（fresh=0）不能用，有 V5 出图的时段也不能用
    from app.database import Database
    db = Database(str(tmp_path / "q.db"))
    await db.connect()
    t0 = 1_000_000.0
    rows = [(t0, 80, 1), (t0 + 3600 * 2, 80.5, 1), (t0 + 3600 * 4, 81, 1), (t0 + 3600 * 6, 81.5, 0), (t0 + 3600 * 8, 82, 1)]
    for ts, p, f in rows:
        await db._db.execute("INSERT INTO upstream_snapshots(ts, v5_percent, fresh) VALUES (?,?,?)", (ts, p, f))
    await db._db.commit()
    q = await quota_algo.quiet_recovery(db, t0 + 3600 * 9)
    assert q is not None and q["hours"] == 4.0 and q["rate"] == 6.0      # 只算前两段（4 小时涨 1%）
    await db.close()


def test_borrow_only_when_pool_nearly_full_and_heavy_users_get_less():
    # 公益版：账号快满（≥95%）才借；用得最多的三分之一只借一半，不奖励刷量
    assert quota_algo.borrow_plan(90, 18, {1: 50})["open"] is False
    usage = {1: 2, 2: 5, 3: 10, 4: 40, 5: 90, 6: 120}
    p = quota_algo.borrow_plan(97, 18, usage)
    assert p["open"] and p["bonus"] == 9 and p["heavy_bonus"] == 4 and sorted(p["heavy"]) == [5, 6]
    quota_algo.BORROW.clear(); quota_algo.BORROW.update(p)
    assert quota_algo.borrow_for(1) == 9 and quota_algo.borrow_for(6) == 4
    quota_algo.BORROW.clear(); quota_algo.BORROW.update({"open": False})
    assert quota_algo.borrow_for(1) == 0
