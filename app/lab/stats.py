"""统计工具（只用 numpy + 标准库）。

━━ 统计单位（统计专家意见 4）━━
  请求不是独立样本：同一成员的请求高度相关（成员间 ICC ≈ 0.24，设计效应 ≈ 5.7），按请求数算的 n 会把区间算窄 5 倍多。
  所以：比例 / 拒绝率等按「成员-天」聚合；区间用 cluster bootstrap（按成员整体重抽样）；
  样本量门槛按成员-天数（≥ MIN_MEMBER_DAYS）和覆盖天数（≥ MIN_DAYS）判断，不按请求数。
  等待时间中位数≈0 秒（大多数请求不用排队），不可检验；描述用 P(等待 > 5 秒)、p90、几何均值 GM(等待+1)−1。
  每日报告只做描述与区间，不做「今天 vs 昨天」的显著性检验（多重比较 + 依赖结构 + 混杂事件，p 值没有意义）。

方法：
  · cluster_bootstrap：按成员整体有放回重抽样，percentile 区间（成员数少时 BCa 的加速度估计不稳，不用）。
  · bootstrap_ci：独立样本的 BCa bootstrap（Efron 1987），n 大时 jackknife 分组删除；退化时退回 percentile 并标注。
  · 蒙特卡洛的 95% 区间 = R 次重复结果的经验 2.5% / 97.5% 分位数（不是 bootstrap）。
  · 比例用 Wilson score 区间（单位必须独立：成员-天比例可以，请求比例不行）。
  · icc_oneway：单因素 ANOVA 估计 ICC(1)；设计效应 = 1 + (m̄ − 1)·ICC；有效样本量 = n / 设计效应。
  · Mann-Whitney U、Cliff's δ、Holm、置换检验保留给回测比较参数（单位是成员-天汇总值），日报不用。
  · 每小时计数的区间用 Poisson 精确区间的 Byar 近似。
样本不足时一律返回 status="insufficient" 和「样本不足，不下结论」，不计算 p 值。
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Callable, Optional, Sequence

import numpy as np

MIN_N = 30
MIN_MEMBER_DAYS = 30
MIN_DAYS = 3
INSUFFICIENT = "样本不足，不下结论"
_N = NormalDist()


def phi(z: float) -> float:
    return _N.cdf(z)


def phi_inv(p: float) -> float:
    return _N.inv_cdf(min(max(p, 1e-12), 1 - 1e-12))


# ---------------------------------------------------------------- bootstrap
def _apply(stat: Callable, samples: np.ndarray) -> np.ndarray:
    try:
        out = np.asarray(stat(samples, axis=1), dtype=float)
        if out.shape == (samples.shape[0],):
            return out
    except TypeError:
        pass
    return np.array([stat(s) for s in samples], dtype=float)


def bootstrap_ci(x: Sequence[float], stat: Callable = np.mean, *, B: int = 2000, alpha: float = 0.05,
                 method: str = "bca", seed: int = 0, chunk: int = 250) -> dict:
    """返回 {"est", "lo", "hi", "method", "B", "n"}。method: "bca"（默认）或 "percentile"。"""
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n == 0:
        return {"est": None, "lo": None, "hi": None, "method": method, "B": B, "n": 0}
    theta = float(stat(x))
    rng = np.random.default_rng(seed)
    boots = np.empty(B)
    for s in range(0, B, chunk):
        m = min(chunk, B - s)
        boots[s:s + m] = _apply(stat, x[rng.integers(0, n, size=(m, n))])
    lo_q, hi_q = alpha / 2, 1 - alpha / 2
    if method == "bca" and n >= 3 and np.ptp(boots) > 0:
        prop = (np.sum(boots < theta) + 0.5 * np.sum(boots == theta)) / B
        z0 = phi_inv(prop)
        # 加速度：jackknife（n 大时分组删除）
        groups = np.array_split(np.arange(n), min(n, 200))
        jack = np.array([float(stat(np.delete(x, g))) for g in groups])
        d = jack.mean() - jack
        den = 6.0 * (np.sum(d ** 2) ** 1.5)
        a = float(np.sum(d ** 3) / den) if den > 0 else 0.0

        def adj(q: float) -> float:
            zq = phi_inv(q)
            return phi(z0 + (z0 + zq) / (1 - a * (z0 + zq)))
        lo_q, hi_q = adj(lo_q), adj(hi_q)
    elif method == "bca":
        method = "percentile"          # 退化（常数样本 / n<3）：BCa 无定义，退回 percentile 并如实标注
    lo, hi = np.quantile(boots, [lo_q, hi_q])
    return {"est": theta, "lo": float(lo), "hi": float(hi), "method": method, "B": B, "n": n}


def quantile_band(values: Sequence[float], alpha: float = 0.05) -> dict:
    """蒙特卡洛重复结果的中位数与经验 (α/2, 1−α/2) 分位数。inf（例如「没有用尽」）按 inf 参与排序。"""
    v = np.asarray(values, dtype=float)
    v = v[~np.isnan(v)]
    if len(v) == 0:
        return {"median": None, "lo": None, "hi": None, "n": 0}
    lo, med, hi = finite_quantile(v, [alpha / 2, 0.5, 1 - alpha / 2])
    f = lambda z: None if math.isinf(z) else float(z)
    return {"median": f(med), "lo": f(lo), "hi": f(hi), "n": int(len(v)),
            "lo_inf": bool(math.isinf(lo)), "hi_inf": bool(math.isinf(hi)), "median_inf": bool(math.isinf(med))}


BIG = 1e300


def finite_quantile(values, qs) -> np.ndarray:
    """含 ±inf 的分位数：先换成有限哨兵值再插值（避免 inf−inf=nan），结果里的哨兵值还原成 inf。"""
    v = np.clip(np.asarray(values, dtype=float), -BIG, BIG)
    out = np.quantile(v, qs)
    return np.where(out >= BIG / 2, math.inf, np.where(out <= -BIG / 2, -math.inf, out))


# ---------------------------------------------------------------- 比例 / 计数区间
def wilson(k: int, n: int, alpha: float = 0.05) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Wilson score 区间：返回 (p̂, lo, hi)；n=0 返回 (None, None, None)。"""
    if n <= 0:
        return None, None, None
    z = phi_inv(1 - alpha / 2)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return p, max(0.0, centre - half), min(1.0, centre + half)


def poisson_ci(k: int, alpha: float = 0.05) -> tuple[float, float]:
    """Poisson 计数的 95% 区间（Byar 近似精确区间）。"""
    z = phi_inv(1 - alpha / 2)
    lo = 0.0 if k == 0 else k * (1 - 1 / (9 * k) - z / (3 * math.sqrt(k))) ** 3
    k1 = k + 1
    hi = k1 * (1 - 1 / (9 * k1) + z / (3 * math.sqrt(k1))) ** 3
    return lo, hi


# ---------------------------------------------------------------- 检验
def rankdata(a: np.ndarray) -> np.ndarray:
    """平均秩（处理结），与 scipy.stats.rankdata(method='average') 一致。"""
    a = np.asarray(a, dtype=float)
    order = np.argsort(a, kind="mergesort")
    s = a[order]
    ranks = np.empty(len(a))
    i = 0
    n = len(a)
    while i < n:
        j = i
        while j + 1 < n and s[j + 1] == s[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def sufficiency(n_x: int, n_y: int, days_y: Optional[int] = None, *, min_n: int = MIN_N,
                min_days: int = MIN_DAYS) -> tuple[bool, str]:
    why = []
    if n_x < min_n:
        why.append(f"今天 n={n_x} < {min_n}")
    if n_y < min_n:
        why.append(f"对照 n={n_y} < {min_n}")
    if days_y is not None and days_y < min_days:
        why.append(f"可比天数 {days_y} < {min_days}")
    return (not why), "；".join(why)


def mann_whitney(x: Sequence[float], y: Sequence[float]) -> dict:
    """双侧 Mann-Whitney U（正态近似，结校正 + 连续性校正）。返回 U（x 的）、z、p、Cliff's δ。"""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n1, n2 = len(x), len(y)
    r = rankdata(np.concatenate([x, y]))
    u1 = float(r[:n1].sum() - n1 * (n1 + 1) / 2)
    n = n1 + n2
    _, counts = np.unique(np.concatenate([x, y]), return_counts=True)
    tie = float(np.sum(counts ** 3 - counts))
    var = n1 * n2 / 12.0 * ((n + 1) - tie / (n * (n - 1)))
    mu = n1 * n2 / 2.0
    if var <= 0:
        z, p = 0.0, 1.0
    else:
        diff = u1 - mu
        z = (diff - 0.5 * np.sign(diff)) / math.sqrt(var)
        p = min(1.0, 2 * (1 - phi(abs(z))))
    return {"U": u1, "z": float(z), "p": float(p), "n1": n1, "n2": n2,
            "delta": cliffs_delta_from_u(u1, n1, n2), "method": "Mann-Whitney U（双侧，正态近似，结与连续性校正）"}


def cliffs_delta_from_u(u1: float, n1: int, n2: int) -> float:
    return 2 * u1 / (n1 * n2) - 1 if n1 and n2 else 0.0


def cliffs_delta(x: Sequence[float], y: Sequence[float]) -> float:
    """δ = P(X>Y) − P(X<Y)。用排序 + 二分，O((n1+n2) log n)。"""
    x = np.asarray(x, dtype=float)
    ys = np.sort(np.asarray(y, dtype=float))
    if len(x) == 0 or len(ys) == 0:
        return 0.0
    greater = np.searchsorted(ys, x, side="left").sum()
    less = (len(ys) - np.searchsorted(ys, x, side="right")).sum()
    return float((greater - less) / (len(x) * len(ys)))


def delta_magnitude(d: float) -> str:
    a = abs(d)
    return "可忽略" if a < 0.147 else "小" if a < 0.33 else "中" if a < 0.474 else "大"


def permutation_test(x: Sequence[float], y: Sequence[float], stat: Callable = None, *, n_perm: int = 5000,
                     seed: int = 0) -> dict:
    """双侧置换检验（默认统计量：中位数差）。p = (1 + #{|T*| ≥ |T|}) / (1 + n_perm)。"""
    stat = stat or (lambda a, b: float(np.median(a) - np.median(b)))
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    obs = stat(x, y)
    pool = np.concatenate([x, y])
    rng = np.random.default_rng(seed)
    hits = 0
    for _ in range(n_perm):
        rng.shuffle(pool)
        if abs(stat(pool[:len(x)], pool[len(x):])) >= abs(obs) - 1e-12:
            hits += 1
    return {"stat": obs, "p": (1 + hits) / (1 + n_perm), "n_perm": n_perm, "method": "置换检验（中位数差，双侧）"}


def two_proportion(k1: int, n1: int, k2: int, n2: int) -> dict:
    """两比例 z 检验（合并方差，双侧）。期望频数 < 5 时不给 p 值。"""
    if n1 == 0 or n2 == 0:
        return {"p": None, "status": "insufficient", "note": INSUFFICIENT}
    pooled = (k1 + k2) / (n1 + n2)
    exp_min = min(n1 * pooled, n1 * (1 - pooled), n2 * pooled, n2 * (1 - pooled))
    if exp_min < 5:
        return {"p": None, "status": "insufficient", "note": f"{INSUFFICIENT}（期望频数 {exp_min:.1f} < 5）",
                "diff": k1 / n1 - k2 / n2}
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    z = (k1 / n1 - k2 / n2) / se if se > 0 else 0.0
    return {"p": min(1.0, 2 * (1 - phi(abs(z)))), "z": z, "diff": k1 / n1 - k2 / n2, "status": "ok",
            "method": "两比例 z 检验（双侧）"}


def holm(pvals: Sequence[Optional[float]]) -> list[Optional[float]]:
    """Holm 逐步校正后的 p 值（None 保持 None，不计入族）。"""
    idx = [i for i, p in enumerate(pvals) if p is not None]
    m = len(idx)
    out: list[Optional[float]] = [None] * len(pvals)
    running = 0.0
    for rank, i in enumerate(sorted(idx, key=lambda i: pvals[i])):
        running = max(running, min(1.0, (m - rank) * pvals[i]))
        out[i] = running
    return out


def compare_groups(today: Sequence[float], past: Sequence[float], days_past: int, *, seed: int = 0,
                   min_n: int = MIN_N, min_days: int = MIN_DAYS) -> dict:
    """今天 vs 过去 N 天（请求级）：MWU + Cliff's δ（带 percentile bootstrap 区间）。样本不足不给 p。"""
    t = np.asarray(today, dtype=float)
    p = np.asarray(past, dtype=float)
    ok, why = sufficiency(len(t), len(p), days_past, min_n=min_n, min_days=min_days)
    base = {"n_today": int(len(t)), "n_past": int(len(p)), "days_past": int(days_past)}
    if not ok:
        return {**base, "status": "insufficient", "note": f"{INSUFFICIENT}（{why}）", "p": None}
    mw = mann_whitney(t, p)
    rng = np.random.default_rng(seed)
    ds = []
    for _ in range(500):
        ds.append(cliffs_delta(t[rng.integers(0, len(t), len(t))], p[rng.integers(0, len(p), len(p))]))
    lo, hi = np.quantile(ds, [0.025, 0.975])
    return {**base, "status": "ok", **mw, "delta_ci": [float(lo), float(hi)], "delta_ci_method": "percentile bootstrap B=500",
            "magnitude": delta_magnitude(mw["delta"]),
            "caveat": "请求级检验假设请求相互独立；同一成员的连续请求有相关性，p 值偏乐观"}


def ecdf(x: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
    s = np.sort(np.asarray(x, dtype=float))
    return s, np.arange(1, len(s) + 1) / len(s) if len(s) else np.array([])


# ---------------------------------------------------------------- 聚类（成员）层面
def icc_oneway(values: Sequence[float], groups: Sequence) -> dict:
    """ICC(1)（单因素随机效应 ANOVA 估计）、平均簇大小、设计效应、有效样本量。"""
    v = np.asarray(values, dtype=float)
    g = np.asarray(groups)
    labels, inv = np.unique(g, return_inverse=True)
    k, n = len(labels), len(v)
    if k < 2 or n <= k:
        return {"icc": None, "deff": None, "n_eff": None, "clusters": int(k), "n": int(n), "m_bar": None}
    sizes = np.bincount(inv)
    means = np.bincount(inv, weights=v) / sizes
    grand = v.mean()
    msb = float(np.sum(sizes * (means - grand) ** 2) / (k - 1))
    msw = float(np.sum((v - means[inv]) ** 2) / (n - k))
    m0 = (n - np.sum(sizes ** 2) / n) / (k - 1)
    icc = (msb - msw) / (msb + (m0 - 1) * msw) if (msb + (m0 - 1) * msw) > 0 else 0.0
    icc = max(0.0, float(icc))
    m_bar = n / k
    deff = 1 + (m_bar - 1) * icc
    return {"icc": icc, "deff": float(deff), "n_eff": float(n / deff), "clusters": int(k), "n": int(n), "m_bar": float(m_bar)}


def cluster_bootstrap(values: Sequence[float], groups: Sequence, stat: Callable[[np.ndarray], float], *,
                      B: int = 2000, alpha: float = 0.05, seed: int = 0) -> dict:
    """按簇（成员）整体有放回重抽样的 percentile 区间。stat 作用于拼接后的观测值。"""
    v = np.asarray(values, dtype=float)
    g = np.asarray(groups)
    if len(v) == 0:
        return {"est": None, "lo": None, "hi": None, "B": B, "clusters": 0, "method": "cluster bootstrap（按成员）"}
    labels, inv = np.unique(g, return_inverse=True)
    order = np.argsort(inv, kind="mergesort")
    parts = np.split(v[order], np.cumsum(np.bincount(inv))[:-1])
    rng = np.random.default_rng(seed)
    k = len(parts)
    boots = np.empty(B)
    for b in range(B):
        pick = rng.integers(0, k, k)
        boots[b] = stat(np.concatenate([parts[i] for i in pick]))
    boots = boots[~np.isnan(boots)]
    lo, hi = (np.quantile(boots, [alpha / 2, 1 - alpha / 2]) if len(boots) else (np.nan, np.nan))
    return {"est": float(stat(v)), "lo": float(lo), "hi": float(hi), "B": B, "clusters": int(k),
            "method": "cluster bootstrap（按成员整体重抽样，percentile）"}


def p_over(threshold: float) -> Callable[[np.ndarray], float]:
    return lambda a: float(np.mean(a > threshold)) if len(a) else float("nan")


def p90(a: np.ndarray) -> float:
    return float(np.quantile(a, 0.9)) if len(a) else float("nan")


def geo_mean_plus1(a: np.ndarray) -> float:
    """几何均值 GM(x+1)−1：等待里大量 0 秒，直接取几何均值无定义。"""
    return float(np.exp(np.mean(np.log1p(np.maximum(a, 0)))) - 1) if len(a) else float("nan")


def unit_sufficiency(member_days: int, days: int, *, min_units: int = MIN_MEMBER_DAYS, min_days: int = MIN_DAYS) -> tuple[bool, str]:
    why = []
    if member_days < min_units:
        why.append(f"成员-天 {member_days} < {min_units}")
    if days < min_days:
        why.append(f"覆盖天数 {days} < {min_days}")
    return (not why), "；".join(why)
