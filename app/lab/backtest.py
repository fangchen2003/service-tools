"""回测流程（统计专家意见 8）：参数网格事先登记 → 训练 / 验证 / 样本外 → 滚动前推 → 预测区间覆盖率。

  · 至少 14 个干净的完整天（覆盖 ≥ 20 小时）；不够时返回 status="insufficient"，不跑、不下结论。
  · 第 1–7 天训练，第 8–10 天验证（选模型配置），第 11–14 天样本外 —— 每个登记过的网格只跑一次
    （结果写 registry/oos-<网格哈希>.json，再跑直接返回存档，除非 force=True 并在结果里注明）。
  · 滚动前推：预测第 t 天时只用 t 之前的 7 天建 block 池与重试模型；条件于第 t 天的实际活跃人数与当天的规则参数。
  · 每个指标给 95% 预测区间（R 次重复的 2.5–97.5% 分位），报告实际值落在区间内的比例（覆盖率，Wilson 区间）
    和区间分数（Gneiting & Raftery 2007 interval score，越小越好）。
  · 网格（模型超参数，例如 jitter_min；以及要扫描的策略参数）先 register_grid 登记：文件按内容哈希命名、只追加不修改，
    登记时间早于任何结果。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Optional

import numpy as np

from . import montecarlo as mc
from . import replay as rp
from . import stats
from .data import Dataset, clean_mask, segments

MIN_FULL_DAYS = 14
TRAIN, VALID, OOS = 7, 3, 4
METRICS = ("images_ok", "member_reject_rate", "first_to_image_p90")
DEFAULT_GRID = {"model": {"jitter_min": [0, 10, 30]}, "sweep": {}}


# ---------------------------------------------------------------- 登记
def _registry(out_dir: str) -> str:
    d = os.path.join(out_dir, "registry")
    os.makedirs(d, exist_ok=True)
    return d


def register_grid(out_dir: str, grid: dict, note: str = "") -> dict:
    """登记网格（只追加）：同样内容重复登记返回原记录（含原登记时间）。"""
    body = json.dumps(grid, sort_keys=True, ensure_ascii=False)
    sha = hashlib.sha256(body.encode()).hexdigest()[:12]
    path = os.path.join(_registry(out_dir), f"grid-{sha}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    rec = {"sha": sha, "grid": grid, "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "note": note}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    return rec


def registered_grids(out_dir: str) -> list[dict]:
    d = os.path.join(out_dir, "registry")
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        if name.startswith("grid-") and name.endswith(".json"):
            with open(os.path.join(d, name), encoding="utf-8") as f:
                out.append(json.load(f))
    return sorted(out, key=lambda r: r["registered_at"])


def check_sweep_registered(out_dir: str, name: str, values: list) -> tuple[bool, dict]:
    """参数扫描必须与某个已登记网格完全一致；从没登记过这个参数时，现在登记（登记先于结果）。"""
    hits = [g for g in registered_grids(out_dir) if name in (g["grid"].get("sweep") or {})]
    if not hits:
        return True, register_grid(out_dir, {"model": DEFAULT_GRID["model"], "sweep": {name: list(values)}},
                                   note="首次使用时登记（在看到任何结果之前）")
    for g in hits:
        if list(g["grid"]["sweep"][name]) == list(values):
            return True, g
    return False, hits[-1]


# ---------------------------------------------------------------- 单日预测
def _params_for_day(ds: Dataset, base: dict, day: str) -> dict:
    mid = ds.day_start(day) + 43200
    for seg in segments(ds):
        if seg["start"] <= mid <= seg["end"] + 1:
            return {**base, **seg["inferred"]}
    return dict(base)


def predict_day(ds: Dataset, train_days: list[str], day: str, base: dict, cfg: dict, *, replicates: int = 50,
                seed: int = 0) -> dict:
    keep = clean_mask(ds)
    blocks = mc.Blocks(ds, mask=keep, days=train_days)
    if not blocks.days:
        return {"day": day, "status": "no_blocks"}
    p = _params_for_day(ds, base, day)
    m = keep & (ds.ev["day"] == day)
    actual = rp.actual_metrics(ds, p, m)
    n_active = int(len(set(ds.ev["key"][m & ds.ev["member"]].tolist())))
    reps = []
    for r in range(replicates):
        rng = np.random.default_rng([seed, r])
        jm = cfg.get("jitter_min", 10.0)          # 条件于当天实际活跃人数：在训练窗口的某一天里抽 n_active 个成员-天
        inp = blocks.sample(1.0, rng, jm, n_active=n_active or None)[0]
        res = rp.replay(inp, p, seed=seed + r, tz_offset=0.0, retry=rp.retry_spec(blocks.retry, rng), detail=False)
        reps.append(res)
    out = {"day": day, "active": n_active, "params_hourly_cap": p["account_hourly_cap"], "metrics": {}}
    for k in METRICS:
        vals = np.array([r[k] if r[k] is not None else np.nan for r in reps], dtype=float)
        vals = vals[~np.isnan(vals)]
        a = actual[k]
        if len(vals) == 0 or a is None:
            out["metrics"][k] = {"actual": a, "lo": None, "hi": None, "covered": None}
            continue
        lo, hi = np.quantile(vals, [0.025, 0.975])
        score = (hi - lo) + (2 / 0.05) * (max(0.0, lo - a) + max(0.0, a - hi))
        out["metrics"][k] = {"actual": float(a), "lo": float(lo), "hi": float(hi), "median": float(np.median(vals)),
                             "covered": bool(lo <= a <= hi), "interval_score": float(score)}
    return out


def _score(preds: list[dict]) -> dict:
    cov = [m["covered"] for p in preds for m in p.get("metrics", {}).values() if m.get("covered") is not None]
    iscore = [m["interval_score"] for p in preds for m in p.get("metrics", {}).values() if m.get("interval_score") is not None]
    k = int(sum(cov))
    est, lo, hi = stats.wilson(k, len(cov))
    by_metric = {}
    for name in METRICS:
        c = [p["metrics"][name]["covered"] for p in preds if p.get("metrics", {}).get(name, {}).get("covered") is not None]
        by_metric[name] = {"covered": int(sum(c)), "n": len(c)}
    return {"coverage": est, "coverage_ci": [lo, hi], "n": len(cov), "covered": k, "by_metric": by_metric,
            "mean_interval_score": float(np.mean(iscore)) if iscore else None, "ci_method": "Wilson 95%（天 × 指标，非独立，仅作参考）"}


def _rolling(ds: Dataset, days: list[str], targets: list[str], base: dict, cfg: dict, replicates: int, seed: int) -> list[dict]:
    out = []
    for t in targets:
        i = days.index(t)
        out.append(predict_day(ds, days[max(0, i - TRAIN):i], t, base, cfg, replicates=replicates, seed=seed + i))
    return out


def run_backtest(ds: Dataset, out_dir: str, *, grid: Optional[dict] = None, params: Optional[dict] = None,
                 replicates: int = 50, seed: int = 20261010, force_oos: bool = False) -> dict[str, Any]:
    days = ds.full_days()
    if len(days) < MIN_FULL_DAYS:
        return {"status": "insufficient", "have_full_days": len(days), "need": MIN_FULL_DAYS,
                "note": f"{stats.INSUFFICIENT}：只有 {len(days)} 个完整天，回测至少需要 {MIN_FULL_DAYS} 个（7 训练 + 3 验证 + 4 样本外）"}
    rec = register_grid(out_dir, grid or DEFAULT_GRID)
    base = params or rp.current_params(ds)
    days = days[:MIN_FULL_DAYS] if len(days) == MIN_FULL_DAYS else days
    valid_days = days[TRAIN:TRAIN + VALID]
    oos_days = days[TRAIN + VALID:TRAIN + VALID + OOS]
    configs = [{"jitter_min": j} for j in rec["grid"]["model"].get("jitter_min", [10])]
    val = []
    for cfg in configs:
        preds = _rolling(ds, days, valid_days, base, cfg, replicates, seed)
        val.append({"config": cfg, "score": _score(preds), "predictions": preds})
    best = min(val, key=lambda v: (abs((v["score"]["coverage"] or 0) - 0.95), v["score"]["mean_interval_score"] or 1e18))
    oos_path = os.path.join(_registry(out_dir), f"oos-{rec['sha']}.json")
    if os.path.exists(oos_path) and not force_oos:
        with open(oos_path, encoding="utf-8") as f:
            oos = json.load(f)
        oos["already_run"] = True
    else:
        preds = _rolling(ds, days, oos_days, base, best["config"], replicates, seed + 7)
        oos = {"config": best["config"], "days": oos_days, "score": _score(preds), "predictions": preds,
               "run_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "forced_rerun": bool(force_oos and os.path.exists(oos_path))}
        with open(oos_path, "w", encoding="utf-8") as f:
            json.dump(oos, f, ensure_ascii=False, indent=1, default=float)
        oos["already_run"] = False
    return {"status": "ok", "grid": rec, "train_window": TRAIN, "valid_days": valid_days, "oos_days": oos_days,
            "validation": [{"config": v["config"], "score": v["score"]} for v in val], "chosen": best["config"],
            "oos": oos, "replicates": replicates,
            "conditioning": "预测条件于目标日的实际活跃人数与当天规则参数；block 池与重试模型只用目标日之前 7 天"}
