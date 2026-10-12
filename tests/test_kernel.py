"""模块内核：开关、单个模块出错不影响其他模块、交叉校验报告、多指标互证；真实模块集冒烟测试。"""
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from app.config import Settings
from app.database import Database
from app.kernel import Check, Kernel, Module, Param
from app.state import GateState


@pytest_asyncio.fixture
async def state():
    tmp = tempfile.TemporaryDirectory()
    st = GateState(Settings())
    st.db = Database(str(Path(tmp.name) / "k.sqlite"))
    await st.db.connect()
    st.guard.db = st.db
    st.share.db = st.db
    yield st
    await st.db.close()
    tmp.cleanup()


@pytest.mark.asyncio
async def test_toggle_isolation_and_checks(state):
    bugs, ran = [], []
    k = Kernel(state, bug=lambda src, exc=None, **kw: bugs.append(src))

    async def ok_tick(k):
        ran.append("a")
        return "fine"

    async def bad_tick(k):
        raise RuntimeError("boom")

    async def failing_check(k):
        return [Check("两边对不上", False, "1 ≠ 2"), Check("对得上", True)]

    k.register(Module("a", "A", "?", tick=ok_tick, params=[Param("p", lambda: 3, "实测")]))
    k.register(Module("b", "B", "?", tick=bad_tick, checks=failing_check))
    k.register(Module("c", "C", "?", tick=ok_tick, default_enabled=False))
    last = await k.tick_all()
    assert ran == ["a"]                                        # c 默认关闭；b 出错不影响 a
    assert last["a"]["ok"] and not last["b"]["ok"] and "boom" in last["b"]["error"]
    assert "module:b" in bugs and "check:b:两边对不上" in bugs
    await k.set_enabled("a", False)
    await k.set_enabled("c", True)
    ran.clear()
    await k.tick_all()
    assert ran == ["a"] and not (await k.enabled("a"))         # 这次跑的是 c（同一个函数）
    snap = await k.snapshot()
    assert snap[0]["params"][0] == {"name": "p", "value": 3, "note": "实测"}


@pytest.mark.asyncio
async def test_real_modules_build_tick_and_check(state):
    from app import modules
    bugs = []
    k = modules.build(state, bug=lambda src, exc=None, **kw: bugs.append((src, kw.get("title"))))
    assert list(k.modules) == ["observation", "capacity", "allocation", "scheduling", "anlas", "autopilot", "integrity", "risk", "registration"]
    await k.tick_all()
    snap = {m["name"]: m for m in await k.snapshot()}
    assert snap["capacity"]["params"] and all("读取失败" not in str(p["value"]) for m in snap.values() for p in m["params"])
    assert snap["risk"]["last"]["runs"][-1]["level"] in ("ok", "warn", "bad")       # 风险检查并进模块，有运行记录
    assert not snap["registration"]["enabled"]                 # 领 Key 默认关闭
    await k.set_enabled("registration", True)
    assert await state.db.get_setting("register_open") == "1"
    await k.set_enabled("integrity", False)
    assert await state.share.mode() == "off"
    # 观测：计数和日志对得上；内存每小时计数 ↔ 日志
    checks = {c["name"]: c for m in (await k.snapshot()) for c in m["last"].get("checks", [])}
    assert checks["生成记录 ↔ 用量日志"]["ok"]



@pytest.mark.asyncio
async def test_capacity_checks_run_with_a_token(state):
    """有上游账号时容量校验（含上游快照核对）必须能跑通，不能「校验本身出错」（10-10 线上抓到过参数写错）。"""
    from app import modules

    class Tok:
        token_id, usable = "t1", True
    state.nai.pool = [Tok()]
    await state.db._db.execute("INSERT INTO upstream_snapshots(ts, v5_percent, v5_rate, anlas) VALUES (0, 90, 11, 9000)")
    await state.db._db.commit()
    k = modules.build(state, bug=lambda *a, **kw: None)
    bad = await k.check_all()
    names = [c.name for _, c in bad]
    assert "校验本身出错" not in names, [(n, c.detail) for n, c in bad]
