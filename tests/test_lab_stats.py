"""实验室统计工具：与已知值 / 解析结果对照。"""
import math

import pytest

np = pytest.importorskip("numpy")

from app.lab import stats  # noqa: E402


def test_wilson_known_values():
    p, lo, hi = stats.wilson(5, 10)
    assert p == 0.5 and lo == pytest.approx(0.2366, abs=1e-4) and hi == pytest.approx(0.7634, abs=1e-4)
    p, lo, hi = stats.wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-4)
    assert stats.wilson(0, 0) == (None, None, None)
    # 对称性
    _, lo1, hi1 = stats.wilson(3, 20)
    _, lo2, hi2 = stats.wilson(17, 20)
    assert lo1 == pytest.approx(1 - hi2) and hi1 == pytest.approx(1 - lo2)


def test_poisson_ci_matches_exact():
    lo, hi = stats.poisson_ci(10)
    assert lo == pytest.approx(4.795, abs=0.02) and hi == pytest.approx(18.39, abs=0.03)
    lo, hi = stats.poisson_ci(0)
    assert lo == 0 and hi == pytest.approx(3.689, abs=0.05)


def test_rankdata_ties():
    assert stats.rankdata(np.array([1, 2, 2, 3])).tolist() == [1, 2.5, 2.5, 4]
    assert stats.rankdata(np.array([3, 1, 3, 3])).tolist() == [3, 1, 3, 3]


def test_mann_whitney_known_value():
    r = stats.mann_whitney([1, 2, 3, 4, 5], [6, 7, 8, 9, 10])
    assert r["U"] == 0
    assert r["p"] == pytest.approx(0.01219, abs=2e-4)      # scipy method='asymptotic'（连续性校正）
    assert r["delta"] == -1


def test_mann_whitney_agrees_with_permutation_on_ties():
    rng = np.random.default_rng(1)
    x = rng.integers(0, 6, 40).astype(float)        # 大量结
    y = rng.integers(1, 7, 45).astype(float)
    mw = stats.mann_whitney(x, y)
    # 以 U 为统计量的置换检验
    pool = np.concatenate([x, y])
    obs = abs(mw["U"] - len(x) * len(y) / 2)
    hits = 0
    for _ in range(4000):
        rng.shuffle(pool)
        u = stats.rankdata(pool)[:len(x)].sum() - len(x) * (len(x) + 1) / 2
        hits += abs(u - len(x) * len(y) / 2) >= obs - 1e-9
    assert mw["p"] == pytest.approx((1 + hits) / 4001, abs=0.02)


def test_cliffs_delta():
    assert stats.cliffs_delta([1, 2, 3], [1, 2, 3]) == 0
    assert stats.cliffs_delta([4, 5], [1, 2]) == 1
    x, y = [1, 3, 5, 7], [2, 3, 4]
    mw = stats.mann_whitney(x, y)
    assert stats.cliffs_delta(x, y) == pytest.approx(mw["delta"])
    assert stats.delta_magnitude(0.1) == "可忽略" and stats.delta_magnitude(0.5) == "大"


def test_holm():
    assert stats.holm([0.01, 0.04, 0.03, 0.005]) == pytest.approx([0.03, 0.06, 0.06, 0.02])
    assert stats.holm([None, 0.02]) == [None, 0.02]
    assert stats.holm([0.9, 0.8]) == pytest.approx([1.0, 1.0])


def test_bootstrap_percentile_matches_t_interval_for_normal_mean():
    rng = np.random.default_rng(3)
    x = rng.normal(10, 2, 400)
    half = 1.966 * x.std(ddof=1) / math.sqrt(len(x))
    for method in ("percentile", "bca"):
        r = stats.bootstrap_ci(x, np.mean, B=4000, method=method, seed=1)
        assert r["method"] == method
        assert r["lo"] == pytest.approx(x.mean() - half, abs=0.25 * half)
        assert r["hi"] == pytest.approx(x.mean() + half, abs=0.25 * half)


def test_bca_coverage_on_skewed_data():
    """指数分布均值（右偏）：BCa 95% 区间的实际覆盖率应接近名义值。"""
    rng = np.random.default_rng(11)
    hits = 0
    sims = 200
    for s in range(sims):
        x = rng.exponential(1.0, 40)
        r = stats.bootstrap_ci(x, np.mean, B=600, method="bca", seed=s)
        hits += r["lo"] <= 1.0 <= r["hi"]
    assert 0.87 <= hits / sims <= 0.99


def test_bootstrap_degenerate_falls_back_to_percentile():
    r = stats.bootstrap_ci([5, 5, 5, 5], np.mean, B=100)
    assert r["method"] == "percentile" and r["lo"] == r["hi"] == 5


def test_insufficient_samples_do_not_get_p_values():
    r = stats.compare_groups([1.0] * 10, list(range(100)), days_past=7)
    assert r["status"] == "insufficient" and r["p"] is None and stats.INSUFFICIENT in r["note"]
    r = stats.compare_groups(list(range(50)), list(range(100)), days_past=2)
    assert r["status"] == "insufficient" and "可比天数" in r["note"]
    r = stats.compare_groups(np.arange(50) + 60.0, np.arange(100.0), days_past=5)
    assert r["status"] == "ok" and r["p"] < 0.05 and r["delta"] > 0 and r["delta_ci"][0] <= r["delta"] <= r["delta_ci"][1]
    t = stats.two_proportion(1, 40, 2, 300)
    assert t["p"] is None and t["status"] == "insufficient"
    t = stats.two_proportion(30, 100, 10, 100)
    assert t["p"] < 0.01


def test_quantile_band_handles_inf():
    b = stats.quantile_band([1, 2, 3, math.inf, math.inf])
    assert b["median"] == 3 and b["hi"] is None and b["hi_inf"]
    q = stats.finite_quantile([math.inf] * 4, [0.5])
    assert math.isinf(q[0])


def test_permutation_test_detects_shift():
    rng = np.random.default_rng(0)
    r = stats.permutation_test(rng.normal(0, 1, 40), rng.normal(1.5, 1, 40), n_perm=999)
    assert r["p"] < 0.01


def test_icc_and_design_effect():
    g = np.repeat(np.arange(10), 20)
    perfect = np.repeat(np.arange(10, dtype=float), 20)        # 组内完全相同、组间不同 → ICC = 1
    r = stats.icc_oneway(perfect, g)
    assert r["icc"] == pytest.approx(1.0) and r["deff"] == pytest.approx(20.0) and r["n_eff"] == pytest.approx(10.0)
    rng = np.random.default_rng(0)
    r0 = stats.icc_oneway(rng.normal(size=200), g)
    assert r0["icc"] < 0.1 and r0["deff"] < 3
    # 已知 ICC 的模拟：组效应方差 1、个体方差 3 → ICC = 0.25
    gg = np.repeat(np.arange(300), 10)
    v = rng.normal(0, 1, 300)[gg] + rng.normal(0, np.sqrt(3), 3000)
    assert stats.icc_oneway(v, gg)["icc"] == pytest.approx(0.25, abs=0.06)


def test_cluster_bootstrap_wider_than_iid_for_clustered_data():
    rng = np.random.default_rng(2)
    g = np.repeat(np.arange(20), 30)
    v = (rng.random(20) < 0.3)[g].astype(float) * (rng.random(600) < 0.8)     # 强聚类的 0/1
    cl = stats.cluster_bootstrap(v, g, np.mean, B=1000, seed=1)
    iid = stats.bootstrap_ci(v, np.mean, B=1000, method="percentile", seed=1)
    assert (cl["hi"] - cl["lo"]) > 2 * (iid["hi"] - iid["lo"])
    assert cl["lo"] <= cl["est"] <= cl["hi"] and cl["clusters"] == 20


def test_wait_descriptors_and_unit_sufficiency():
    a = np.array([0, 0, 0, 10, 20.0])
    assert stats.p_over(5)(a) == pytest.approx(0.4)
    assert stats.geo_mean_plus1(np.zeros(4)) == 0
    assert stats.p90(a) == pytest.approx(16.0)
    ok, why = stats.unit_sufficiency(12, 1)
    assert not ok and "成员-天" in why and "覆盖天数" in why
    assert stats.unit_sufficiency(40, 3)[0]
