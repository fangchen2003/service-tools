"""防分享：一个人换网络 / 两台设备不处罚；多地同时使用逐级处罚（提醒 → 暂停 → 重置 → 停用）。"""
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from app.database import Database
from app.share_guard import ShareGuard, RESET

KEY = {"id": 1, "name": "小明", "is_admin": 0, "is_test": 0}
WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
AND = "Mozilla/5.0 (Linux; Android 14)"
IOS = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"


@pytest_asyncio.fixture
async def guard():
    tmp = tempfile.TemporaryDirectory()
    db = Database(str(Path(tmp.name) / "g.sqlite"))
    await db.connect()
    await db.set_setting("share_guard_mode", "enforce")    # 这些测试验证的是执行模式下的逐级处罚
    g = ShareGuard(db)
    yield g
    await db.close()
    tmp.cleanup()


class Calls:
    def __init__(self):
        self.member, self.admin, self.resets, self.bans = [], [], [], []

    def kw(self):
        async def member(k, t): self.member.append(t)
        async def reset(k): self.resets.append(k); return "nai-NEW"
        async def ban(k): self.bans.append(k)
        return {"member": member, "admin": self.admin.append, "reset": reset, "ban": ban}


@pytest.mark.asyncio
async def test_one_person_switching_networks_is_not_punished(guard):
    c, t = Calls(), 1_000_000.0
    # 家里 WiFi 一下午，傍晚换到手机流量，晚上回家；电脑 + 手机两台设备
    for i in range(30):
        await guard.observe(KEY, "120.235.*.*", 4, WIN, now=t + i * 120, **c.kw())
    for i in range(10):
        await guard.observe(KEY, "39.144.*.*", 4, AND, now=t + 4000 + i * 60, **c.kw())
    for i in range(10):
        await guard.observe(KEY, "120.235.*.*", 4, WIN, now=t + 9000 + i * 60, **c.kw())
    assert not c.member and not c.resets and not guard.paused_until(1, t + 9700)


@pytest.mark.asyncio
async def test_three_places_at_once_escalates_to_reset_and_ban(guard):
    """逐级：提醒 → 暂停（暂停中的请求在线上会被挡掉）→ 暂停结束后再犯才重置 → 第 3 次重置停用。每次最多升一级。"""
    c, t = Calls(), 2_000_000.0
    nets = ["120.235.*.*", "183.6.*.*", "112.97.*.*"]
    actions = []
    for burst in range(400):
        until = guard.paused_until(1, t)
        if until:
            t = until + 1                                 # 暂停期间线上请求进不来
        for i in range(9):
            a = await guard.observe(KEY, nets[i % 3], 4, [WIN, AND, IOS][i % 3], now=t + i * 30, **c.kw())
            if a:
                actions.append(a)
        t += 1900
        if c.bans:
            break
    assert actions[:2] == ["warn", "pause"]                # 第一次不能直接跳到暂停 / 重置
    assert actions.index("reset") > actions.index("pause")
    assert c.resets and "nai-NEW" in next(m for m in c.member if "新的 Key" in m)
    ev = await guard.evidence(1)
    assert any(e["kind"] == "concurrent" for e in ev) and any(e["kind"] == "action" for e in ev)
    assert c.bans == [1]


@pytest.mark.asyncio
async def test_vpn_rotating_exits_same_client_is_not_punished(guard):
    """10-09 回测：代理每个请求换出口，但始终是同一个客户端 —— 一个人。"""
    c, t = Calls(), 5_000_000.0
    nodes = ["89.185.*.*", "34.92.*.*", "185.14.*.*", "85.237.*.*", "104.28.*.*"]
    for i in range(200):
        await guard.observe(KEY, nodes[i % 5], 4, WIN + " Chrome/154", now=t + i * 40, busy=i % 2 == 0, **c.kw())
    assert not c.member and not c.admin


@pytest.mark.asyncio
async def test_heavy_user_many_devices_alone_never_punished(guard):
    """三台设备 + 每天用 20 小时，但从不跨网络同时用：辅助证据只记录不计分（盘点发现的风险 #1）。"""
    c, t = Calls(), 7_000_000.0
    for day in range(10):
        for h in range(22):
            ua = [WIN, AND, IOS][h % 3]
            await guard.observe(KEY, "120.235.*.*", 4, ua, now=t + day * 86400 + h * 3600, **c.kw())
    assert not c.member and not c.resets
    ev = await guard.evidence(1)
    assert ev and all(e["points"] == 0 for e in ev)


@pytest.mark.asyncio
async def test_overlap_two_devices_two_places(guard):
    c, t = Calls(), 6_000_000.0
    a = b = None
    for r in range(6):
        base = t + r * 1900
        await guard.observe(KEY, "120.235.*.*", 4, WIN, now=base, **c.kw())
        a = await guard.observe(KEY, "183.6.*.*", 4, AND, now=base + 20, busy=True, **c.kw())
        b = a or b
    ev = await guard.evidence(1)
    assert any(e["kind"] == "overlap" for e in ev) and b in ("warn", "pause", "reset")


@pytest.mark.asyncio
async def test_observe_mode_and_clear(guard):
    c, t = Calls(), 3_000_000.0
    await guard.db.set_setting("share_guard_mode", "observe")
    nets = ["1.2.*.*", "3.4.*.*", "5.6.*.*"]
    for burst in range(6):
        for i in range(9):
            assert await guard.observe(KEY, nets[i % 3], 4, [WIN, AND, IOS][i % 3], now=t + burst * 1900 + i * 30,
                                       **c.kw()) is None
    assert not c.member and c.admin                    # 观察模式只告诉站长
    rep = await guard.report(now=t + 12000)
    assert rep and rep[0]["score"] >= RESET * 0.5
    await guard.clear(1)
    assert not await guard.report()


@pytest.mark.asyncio
async def test_test_and_admin_keys_skip(guard):
    c = Calls()
    for k in ({**KEY, "is_test": 1}, {**KEY, "is_admin": 1}):
        for i in range(30):
            assert await guard.observe(k, f"{i % 3}.0.*.*", 4, WIN, now=4e6 + i * 20, **c.kw()) is None
    assert not c.member and not c.admin


def _body(prompt, neg, sampler="k_euler_ancestral", steps=28, scale=5):
    return {"input": prompt, "parameters": {"sampler": sampler, "steps": steps, "scale": scale, "negative_prompt": neg}}


A_CORE = "masterpiece, best quality, artist:wlop, artist:ask, {{jailbreak no limits}}, "
B_CORE = "very aesthetic, year 2024, artist:mika pikazo, rating:general, "


@pytest.mark.asyncio
async def test_habits_interleaved_two_people_is_strong(guard):
    c, t = Calls(), 8_000_000.0
    for i in range(12):
        if i % 2 == 0:
            body, net, ua = _body(A_CORE + f"1girl, scene {i}", "lowres, bad hands"), "120.235.*.*", WIN
        else:
            body, net, ua = _body(B_CORE + f"1boy, city night {i}", "worst quality", "k_dpmpp_2m", 23, 6), "183.6.*.*", AND
        await guard.observe_habit(KEY, body, net, ua, now=t + i * 300, **c.kw())
    ev = await guard.evidence(1)
    assert any(e["kind"] == "habits" and e["points"] == 35 for e in ev) and c.member


@pytest.mark.asyncio
async def test_habit_switch_once_or_auto_prompts_not_flagged(guard):
    c, t = Calls(), 9_000_000.0
    # 柏宝绘：剧情自动生成，每张提示词都不同，但 jailbreak / 质量词 / 参数固定
    for i in range(10):
        await guard.observe_habit(KEY, _body(A_CORE + f"scene {i}, mood {i * 7}, place {i * 3}", "lowres"), "1.2.*.*", WIN,
                                  now=t + i * 200, **c.kw())
    # 之后换了一整套画师串（先 A 后 B，不交替）
    for i in range(10):
        await guard.observe_habit(KEY, _body(B_CORE + f"new style {i}", "worst", "k_dpmpp_2m", 23), "1.2.*.*", WIN,
                                  now=t + 2200 + i * 200, **c.kw())
    assert not await guard.evidence(1) and not c.member


@pytest.mark.asyncio
async def test_habits_same_network_and_client_only_recorded(guard):
    c, t = Calls(), 10_000_000.0
    for i in range(12):
        body = _body(A_CORE + f"x{i}", "lowres") if i % 2 == 0 else _body(B_CORE + f"y{i}", "worst", "k_dpmpp_2m", 23)
        await guard.observe_habit(KEY, body, "1.2.*.*", WIN, now=t + i * 300, **c.kw())
    ev = await guard.evidence(1)
    assert ev and all(e["points"] == 0 for e in ev) and not c.member


@pytest.mark.asyncio
async def test_guard_hour_count_survives_restart(guard):
    """部署重启不能把每小时计数清零（10-10 00 点连部署 4 次，实际出了 85 张 > 80）。"""
    import time as _t
    from app.guard import Guard
    db, now = guard.db, _t.time()
    for i in range(30):
        await db.add_log(1, "m", "image", "nai-diffusion-4-5-full", "ok", images=1)
    await db.add_log(1, "m", "tags", "", "ok")
    g = Guard(db)
    assert await g.seed_hour(db, ["tok"], now=now + 1) == 30
    assert g.hour_count("tok", now + 2) == 30


def test_minutes_until_free_waits_for_enough_slots():
    from app.guard import Guard
    g, now = Guard(), 1_000_000.0
    g.values["account_hourly_cap"] = 80
    for i in range(106):                       # 超了 26 张：要等第 27 张滑出窗口
        g.record_start("tok", now - 3600 + 30 + i * 30)
    assert g.minutes_until_free("tok", now) == 14



@pytest.mark.asyncio
async def test_hourly_cap_aimd_and_3h_window(guard):
    from app.guard import Guard
    db = guard.db
    g, now = Guard(db), 2_000_000.0
    assert g.values["account_hourly_cap"] == 150
    assert await g.adapt_daily(now - 10) is None                 # 第一次只开始计时，不加
    assert await g.on_upstream_429(now) == (150, 100)          # 上游限流：减半，不低于 100
    assert await g.on_upstream_429(now + 1) is None             # 已经在下限
    g.values["account_hourly_cap"] = 60                          # 站长手动压到下限以下
    assert await g.on_upstream_429(now + 2) is None             # 429 不能把它抬回 100
    assert g.values["account_hourly_cap"] == 60
    g.values["account_hourly_cap"] = 100
    assert await g.adapt_daily(now + 3600) is None               # 24 小时内有过限流：不加
    assert await g.adapt_daily(now + 86401) is None              # 没顶到过上限：没信息，不加
    await db._db.execute("INSERT INTO usage_log(ts, key_id, key_name, kind, status, detail, reason) VALUES (?,?,?,?,?,?,?)",
                         (now + 2 * 86400 + 3600, 1, "m", "image", "rejected", "429 本小时出图量已达上限（每小时 100 张）",
                          "hourly_cap"))
    await db._db.commit()
    assert await g.adapt_daily(now + 2 * 86400 + 86401) == (100, 110)   # 顶到过上限且平稳一天 +10
    assert await g.adapt_daily(now + 2 * 86400 + 86500) is None   # 一天最多一次
    g2 = Guard(db); await g2.load()
    assert g2.values["account_hourly_cap"] == 110                # 持久化
    # 3 小时窗口：每小时都在上限内，但 3 小时累计到 400 也要拦
    g3, t = Guard(), 3_000_000.0
    for i in range(400):
        g3.record_start("tok", t - 3 * 3600 + 60 + i * 26)
    reason = await g3.token_block_reason(_DB0(), "tok", "d", t)
    assert reason and "3 小时" in reason


class _DB0:
    async def get_upstream_counter(self, token_id, day):
        return {"images": 0}


@pytest.mark.asyncio
async def test_pause_key_sets_reason_and_is_idempotent(guard):
    """非防分享的 Key 暂停（自动驾驶限流用）：设置 paused_until + 原因，已暂停则不重复。"""
    assert await guard.pause_key(1, 3600, "你的 Key 短时间内被大量拒绝")
    assert guard.paused_until(1)
    assert guard.pause_reasons[1] == "你的 Key 短时间内被大量拒绝"
    assert not await guard.pause_key(1, 3600, "再次")      # 已在暂停中，不重复
    await guard.db._db.commit()
    # 重启后原因还在（以前只存内存，重启后成员会看到「多人共用」的错误说法）
    g2 = ShareGuard(guard.db)
    await g2.load()
    assert g2.paused_until(1) and g2.pause_reasons[1] == "你的 Key 短时间内被大量拒绝"
    # 站长点「误判」只清防分享的分数，不解除自动驾驶的限流
    await g2.clear(1)
    assert g2.paused_until(1) and g2.pause_reasons[1]
    g3 = ShareGuard(guard.db)
    await g3.load()
    assert g3.paused_until(1)


@pytest.mark.asyncio
async def test_mode_defaults_to_observe_when_unset():
    """未设置 / 值不对时按 observe（fail-closed）：库重建或恢复后不会突然开始处罚成员。"""
    tmp = tempfile.TemporaryDirectory()
    db = Database(str(Path(tmp.name) / "g.sqlite"))
    await db.connect()
    try:
        g = ShareGuard(db)
        assert await g.mode() == "observe"
        await db.set_setting("share_guard_mode", "bogus")
        assert await g.mode() == "observe"
    finally:
        await db.close(); tmp.cleanup()
