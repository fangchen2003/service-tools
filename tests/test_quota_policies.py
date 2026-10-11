"""影子模式：历代额度算法并排计算，只记录不生效；线上执行的就是影子里的 LIVE。"""
from app import quota_algo, quota_policies as qp

CFG = dict(quota_algo.DEFAULTS)


def test_every_policy_runs_for_every_account_count():
    stats = {"used": 1500, "ceiling_hits": 2, "hourly_blocks": 0, "base_blocks": 0}
    usage = [300, 200, 150] + [30] * 40          # 总需求 1850 ≥ 1 个账号可分的 1530
    out = qp.run_all({}, day="2026-10-11", usage=usage, stats=stats, cap_per_account=1700, v5_global=170, cfg=CFG)
    assert set(out["results"]) == set(qp.POLICIES)
    for name, by_n in out["results"].items():
        assert set(by_n) == {str(n) for n in qp.ACCOUNTS}
        assert by_n["1"]["budget"] == 1530 and by_n["2"]["budget"] == 3060          # V5 也按账号数放大
        for r in by_n.values():
            assert r["b"] <= r["a"] and r["why"]
    assert qp.LIVE in qp.POLICIES
    simple = out["results"]["simple_v2_18"]
    assert simple["1"]["a"] == simple["1"]["b"] == qp.water_fill(usage, 1530)     # 需求顶满：按需求平分
    assert simple["2"]["a"] > simple["1"]["a"]                                       # 账号多了每人分得多


def test_policies_keep_their_own_state():
    stats = {"used": 1500, "ceiling_hits": 0, "hourly_blocks": 5, "base_blocks": 0}
    state = {}
    for _ in range(3):
        out = qp.run_all(state, day="d", usage=[50] * 30, stats=stats, cap_per_account=1700, v5_global=170, cfg=CFG)
        state = out["results"]
    assert state["aimd_v2_16"]["1"]["a"] < CFG["quota_target_avg"]                 # 旧算法每天下调，走出自己的轨迹
    assert state["simple_v2_18"]["1"]["a"] == state["simple_v2_18"]["1"]["b"]


def test_live_uses_the_same_function():
    assert quota_algo.water_fill is qp.water_fill
