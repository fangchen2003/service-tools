"""蒙特卡洛压力测试：两阶段 block bootstrap（先抽整天、再在天内抽成员）+ 规模放大，直到失控。

━━ 重抽样（统计专家意见 5）━━
  1× = 当前登记成员数 N_reg（启用、非测试、非站长）。每次重复：
   ① 从「干净」的完整天（剔除部署前后 30 分钟；没有完整天时用全部天并标注 partial）里抽一天 d；
   ② 活跃比例 p ~ Beta(a_d + 1, N_reg − a_d + 1)（a_d = 那天活跃成员数），活跃人数 ~ Binomial(round(规模 × N_reg), p)；
   ③ 只在第 d 天的成员-天 block 里抽成员（保留「大家都在晚上用」的同步性）：先不放回地用完当天的成员，
      超出当天人数的部分才有放回地复制；每个 block = 一个成员一天的新需求（已剔除重试），整体平移 U(−J, +J) 分钟（默认 J=10）；
   ④ 重试 / 放弃：第 k 次被拒后的重试概率每次重复从 Beta 后验重新抽（参数不确定性），重试间隔用实测分布。
  嵌套设计（把「数据不确定性」和「天与天的波动」分开）：R 次重复分成 G = R/10 组；每组先重抽数据池
  （天有放回）并从后验抽一次活跃比例与重试概率，组内再模拟 10 天。
  组内中位数 =「这份数据下的典型一天」；G 个组中位数的 2.5–97.5% 分位 = 典型日指标的 95% 区间（失控判定用这个）。
  全部 R 天的 97.5% 分位另报为「坏日子」风险（只描述，不参与失控判定）。
  每个（规模, 重复）独立 seed；参数扫描用同一组 seed（共同随机数）。
  synth 合成数据上的结果只是自洽检验，不能当验证。

━━ 失控（统计专家意见 6）：两个维度，各指标取 95% 区间中不利的一侧 ━━
  成员体验（按成员-天）：新请求失败率均值 > 10%；首次请求到出图 p90 > 60 秒；当天被拒 ≥ 3 次的活跃成员 > 20%
  账号安全（任何一项越线即失控）：当天成功总量 > 基线（safety_daily_baseline，默认 1000）；连续满载 > 4 小时；
            V5 剩余斜率代理 < 0。上游 429 / 5xx 尖峰无法模拟，只在真实日志里检查。
  「越线」= 典型日指标 95% 区间的不利一侧（越大越坏的取上界，越小越坏的取下界）越过阈值。
  同时报告中位数越线（点估计，线性插值）和有利一侧越线（几乎必然失控），失控区间 = [不利侧, 有利侧]。

━━ 交叉印证（统计专家意见 9）━━
  理论容量 = min(每小时吞吐 ÷ 人均高峰小时需求, 日上限 ÷ 人均日需求)；专家估计 55–70 人。
  若 1× 就失控，或 1.5× 时体验指标的不利侧都不到阈值一半（「远未失控」），或失控人数与理论容量相差 2 倍以上 → 标「模型可疑」。
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Optional

import numpy as np

from . import replay as rp
from . import stats
from .data import Dataset, clean_mask, retry_model

INNER = 10                 # 每组（同一份重抽数据）模拟的天数
DEFAULT_SCALES = (0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3, 3.5, 4, 5, 6, 7, 8, 10, 12, 15, 20)
EXPERT_RANGE = (55, 70)
EXPERIENCE = {"member_reject_rate": (0.10, ">"), "first_to_image_p90": (60.0, ">"), "share_members_rej3": (0.20, ">")}
# V5 剩余斜率 < 0 不再算失控（10/10 修正）：网关有全站每日 V5 上限兜底，账号余量高时按设计消耗存量（公益版借用），
# 斜率为负是计划内行为；原判定让「1×（80 人）就失控」，与线上不符。斜率仍作为描述指标输出。
SAFETY_FIXED = {"full_load_run_hours": (4, ">")}
LABELS = {"member_reject_rate": "按人计新请求失败率", "first_to_image_p90": "首次请求到出图 p90",
          "share_members_rej3": "被拒 ≥3 次的成员比例", "daily_total_max": "日成功总量",
          "full_load_run_hours": "连续满载小时", "v5_slope_min": "V5 剩余斜率"}
NOT_MODELED = ["上游 429 / 5xx 尖峰（回放无法模拟，只看真实日志）",
               "V5 账号余量耗尽（网关有全站每日 V5 上限兜底；斜率只描述，不判失控）"]
SUMMARY_KEYS = ("member_reject_rate", "first_to_image_p90", "share_members_rej3", "daily_total_max",
                "full_load_run_hours", "v5_slope_min", "wait_p90", "reject_rate", "images_ok", "new_requests", "retries")


def thresholds(params: dict) -> dict[str, tuple[float, str, str]]:
    base = int(params.get("safety_daily_baseline") or 0) or 1000
    t = {k: (v[0], v[1], "experience") for k, v in EXPERIENCE.items()}
    t["daily_total_max"] = (base, ">", "safety")
    t.update({k: (v[0], v[1], "safety") for k, v in SAFETY_FIXED.items()})
    return t


class Blocks:
    """按天组织的成员-天 block（新需求，已剔除重试与部署窗口）。"""

    def __init__(self, ds: Dataset, *, seed: int = 0, full_day_hours: float = 20.0, mask: Optional[np.ndarray] = None,
                 days: Optional[list[str]] = None):
        e = ds.ev
        keep = clean_mask(ds) if mask is None else mask
        self.retry = retry_model(ds, keep)
        inp = rp.inputs_from_dataset(ds, mask=keep, seed=seed, use_key_caps=False)
        src = inp["src_index"]
        member = e["member"][src]
        full = [d for d in ds.full_days(full_day_hours) if days is None or d in days]
        self.partial = not full
        use_days = full if full else [d for d in ds.days() if days is None or d in days]
        self.days = []
        self.registered = max(1, ds.registered)
        for d in use_days:
            sel = np.nonzero(member & (e["day"][src] == d))[0]
            groups: dict[int, list[int]] = {}
            for j in sel.tolist():
                groups.setdefault(int(e["key"][src[j]]), []).append(j)
            if not groups:
                continue
            blocks = []
            for idx in groups.values():
                ix = np.array(idx, dtype=np.int64)
                blocks.append({"tod": e["tod"][src[ix]].astype(float), "dur": inp["dur"][ix], "images": inp["images"][ix],
                               "legacy": inp["legacy"][ix], "ok": inp["ok"][ix], "exo": inp["exo"][ix]})
            self.days.append({"day": d, "blocks": blocks, "active": len(blocks), "coverage": ds.coverage_hours(d)})
        self.n_blocks = sum(len(d["blocks"]) for d in self.days)
        self.requests_per_block = (sum(len(b["tod"]) for d in self.days for b in d["blocks"]) / self.n_blocks
                                   if self.n_blocks else 0.0)

    def resample(self, rng: np.random.Generator) -> list[dict]:
        """数据不确定性：天有放回，并为每天从后验抽一个活跃比例。
        不在天内重抽成员：同一个成员-天被复制两份会把同一段会话叠在一起，人为制造拥堵。"""
        pool = []
        for di in rng.integers(0, len(self.days), len(self.days)).tolist():
            d = self.days[di]
            blocks = d["blocks"]
            a = d["active"]
            pool.append({"day": d["day"], "blocks": blocks, "active": a,
                         "p": float(rng.beta(a + 1, max(0, self.registered - a) + 1))})
        return pool

    def sample(self, scale: float, rng: np.random.Generator, jitter_min: float = 10.0,
               pool: Optional[list[dict]] = None, n_active: Optional[int] = None) -> tuple[dict, dict]:
        """n_active 给定时不抽活跃人数（回测：条件于目标日的实际活跃人数）。"""
        if pool is None:
            d = self.days[int(rng.integers(0, len(self.days)))]
            a = d["active"]
            p = float(rng.beta(a + 1, max(0, self.registered - a) + 1))
        else:
            d = pool[int(rng.integers(0, len(pool)))]
            p = d["p"]
        if n_active is None:
            n_active = max(1, int(rng.binomial(max(1, int(round(scale * self.registered))), min(1.0, p))))
        nb = len(d["blocks"])
        # 先不放回地用完当天的成员（1× 以内就是当天真实成员的子集），超出部分才有放回地复制
        pick = rng.permutation(nb)[:n_active]
        if n_active > nb:
            pick = np.concatenate([pick, rng.integers(0, nb, n_active - nb)])
        shifts = rng.uniform(-jitter_min * 60, jitter_min * 60, n_active) if jitter_min else np.zeros(n_active)
        parts: dict[str, list] = {k: [] for k in ("arrival", "key", "dur", "images", "legacy", "ok", "exo")}
        for j, (b, sh) in enumerate(zip(pick.tolist(), shifts.tolist())):
            blk = d["blocks"][b]
            parts["arrival"].append(np.clip(blk["tod"] + sh, 0, 86399.0))
            parts["key"].append(np.full(len(blk["tod"]), j + 1, dtype=np.int64))
            for k in ("dur", "images", "legacy", "ok", "exo"):
                parts[k].append(blk[k])
        inp = {k: np.concatenate(v) for k, v in parts.items()}
        order = np.argsort(inp["arrival"], kind="mergesort")
        inp = {k: v[order] for k, v in inp.items()}
        n = len(order)
        inp["priv"] = np.zeros(n, dtype=bool)
        inp["cap"] = np.full(n, -1, dtype=np.int64)
        return inp, {"day": d["day"], "p_active": p, "active": n_active}


def _metrics(res: dict) -> dict[str, float]:
    out = {k: res.get(k) for k in SUMMARY_KEYS}
    for k in ("first_to_image_p90", "wait_p90"):
        out[k] = out[k] if out[k] is not None else 0.0
    return {k: float(v) for k, v in out.items()}


def evaluate(groups: list[list[dict[str, float]]], thr: dict) -> dict[str, Any]:
    """groups：每组是同一份重抽数据下的若干天。区间 = 组中位数的 2.5–97.5% 分位（典型日）；另报全部天的分位（坏日子）。"""
    reps = [r for g in groups for r in g]
    med = {k: [float(np.median([r[k] for r in g])) for g in groups if g] for k in SUMMARY_KEYS}
    out: dict[str, Any] = {k: stats.quantile_band(med[k]) for k in SUMMARY_KEYS}
    out["days_band"] = {k: stats.quantile_band([r[k] for r in reps]) for k in thr}
    fail: dict[str, dict] = {"unfavorable": {}, "median": {}, "favorable": {}}
    for k, (t, d, _) in thr.items():
        lo, med_, hi = stats.finite_quantile(med[k], [.025, .5, .975])
        bad = (lambda v: v > t) if d == ">" else (lambda v: v < t)
        unf, fav = (hi, lo) if d == ">" else (lo, hi)
        fail["unfavorable"][k] = bool(bad(unf))
        fail["median"][k] = bool(bad(med_))
        fail["favorable"][k] = bool(bad(fav))
    out["fail"] = fail
    out["R"] = len(reps)
    out["G"] = len(groups)
    return out


def _dims(flags: dict, thr: dict) -> dict[str, bool]:
    return {dim: any(v for k, v in flags.items() if thr[k][2] == dim) for dim in ("experience", "safety")}


def _crossing(scales: list[float], series: list[Optional[float]], thr: float, direction: str) -> Optional[float]:
    prev = None
    for s, v in zip(scales, series):
        if v is None:
            prev = None
            continue
        bad = v > thr if direction == ">" else v < thr
        if bad:
            if prev is None or prev[1] == v:
                return s
            ps, pv = prev
            return ps + (thr - pv) * (s - ps) / (v - pv)
        prev = (s, v)
    return None


def theoretical_capacity(ds: Dataset, params: Optional[dict], mask: Optional[np.ndarray] = None) -> dict[str, Any]:
    """理论容量（排队论的粗估，用来交叉印证蒙特卡洛）。"""
    p = rp.resolve(params)
    e = ds.ev
    keep = clean_mask(ds) if mask is None else mask
    demand = keep & e["member"] & ~e["retry"] & ~((e["status"] == "rejected") & (e["reason"] == "other"))
    reg = max(1, ds.registered)
    days = ds.full_days() or ds.days()
    off = ds.tz_offset() if ds.n else 28800.0
    per_day, peak = [], []
    for d in days:
        m = demand & (e["day"] == d)
        if not m.any():
            continue
        imgs = np.maximum(1, e["images"][m])
        per_day.append(imgs.sum() / reg)
        h = (((e["arrival"][m] + off) % 86400) // 3600).astype(int)
        peak.append(np.bincount(h, weights=imgs, minlength=24).max() / reg)
    if not per_day:
        return {"members": None, "note": "没有数据"}
    dur = e["dur_s"][(e["status"] == "ok") & (e["dur_s"] > 0)]
    cycle = max(float(p["image_min_interval"]) + float(p["interval_jitter"]) / 2, float(np.mean(dur)) if len(dur) else 0.0)
    acc = max(1, int(p["accounts"]))
    thr_hour = min(int(p["account_hourly_cap"]) * acc or 1e9, 3600 / cycle * acc)
    n_hour = thr_hour / float(np.mean(peak))
    n_day = int(p["account_daily_cap"]) * acc / float(np.mean(per_day)) if p["account_daily_cap"] else math.inf
    return {"members": float(min(n_hour, n_day)), "by_hour": float(n_hour), "by_day": float(n_day),
            "peak_hour_per_member": float(np.mean(peak)), "daily_per_member": float(np.mean(per_day)),
            "throughput_per_hour": float(thr_hour), "service_cycle_s": cycle, "registered": reg,
            "partial": not ds.full_days(), "expert_range": list(EXPERT_RANGE),
            "note": "按新需求（剔除重试）计；数据不足一个完整天时人均日需求被低估、按日的容量被高估"}


def run(blocks: Blocks, params: Optional[dict] = None, *, replicates: int = 200, max_scale: float = 10,
        scales: Optional[list[float]] = None, seed: int = 20261010, jitter_min: float = 10.0,
        time_budget: Optional[float] = None, progress: Optional[Callable[[str], None]] = None,
        stop_after: int = 1, theory: Optional[dict] = None) -> dict[str, Any]:
    p = rp.resolve(params)
    thr = thresholds(p)
    scales = [s for s in (scales or DEFAULT_SCALES) if s <= max_scale + 1e-9]
    t_begin = time.time()
    per_scale: list[dict] = []
    certain_at = None
    after = 0
    sec_per_req = None
    for si, scale in enumerate(scales):
        if not blocks.days:
            break
        R = replicates
        if time_budget and sec_per_req is not None:
            left = time_budget - (time.time() - t_begin)
            est = sec_per_req * scale * blocks.registered * 0.6 * blocks.requests_per_block * 1.3
            R = int(max(20, min(replicates, left / max(1, min(len(scales) - si, 4)) / max(est, 1e-6))))
        G = max(2, R // INNER)
        groups: list[list[dict]] = []
        extra = []
        t0 = time.time()
        req = 0
        for g in range(G):
            grng = np.random.default_rng([seed, 99, g])          # 数据重抽与参数后验：不随规模变（共同随机数）
            pool = blocks.resample(grng)
            spec = rp.retry_spec(blocks.retry, grng)
            reps = []
            for r in range(INNER):
                rng = np.random.default_rng([seed, si, g, r])
                inp, info = blocks.sample(scale, rng, jitter_min, pool)
                res = rp.replay(inp, p, seed=seed + 1000 * si + INNER * g + r, tz_offset=0.0, retry=spec, detail=False)
                req += res["n"]
                reps.append(_metrics(res))
                extra.append(info["p_active"])
            groups.append(reps)
        took = time.time() - t0
        sec_per_req = took / max(1, req)
        ev = evaluate(groups, thr)
        ev.update(scale=scale, members=int(round(scale * blocks.registered)), seconds=round(took, 2),
                  p_active=stats.quantile_band(extra),
                  dims={lvl: _dims(ev["fail"][lvl], thr) for lvl in ("unfavorable", "median", "favorable")})
        per_scale.append(ev)
        if progress:
            progress(f"规模 {scale}×（登记 {ev['members']} 人）R={ev['R']}（{ev['G']} 组）：失败率 {ev['member_reject_rate']['median']:.1%}"
                     f"（97.5% {ev['member_reject_rate']['hi']:.1%}），首图 p90 {ev['first_to_image_p90']['median']:.0f}s，"
                     f"日总量 {ev['daily_total_max']['median']:.0f}，{took:.1f}s")
        if certain_at is None and any(ev["fail"]["favorable"].values()):
            certain_at = scale
        elif certain_at is not None:
            after += 1
        if certain_at is not None and after >= stop_after:
            break
    return summarize_runs(per_scale, blocks, p, thr, replicates, seed, jitter_min, time.time() - t_begin, theory)


def summarize_runs(per_scale: list[dict], blocks: Blocks, p: dict, thr: dict, replicates: int, seed: int,
                   jitter_min: float, seconds: float, theory: Optional[dict] = None) -> dict[str, Any]:
    scales = [s["scale"] for s in per_scale]

    def first(level: str, dim: Optional[str] = None) -> Optional[float]:
        for s in per_scale:
            if any(v for k, v in s["fail"][level].items() if dim is None or thr[k][2] == dim):
                return s["scale"]
        return None
    failure: dict[str, Any] = {}
    for dim in ("experience", "safety", None):
        unf = first("unfavorable", dim)
        crosses = [c for k, (t, d, dm) in thr.items() if dim is None or dm == dim
                   for c in [_crossing(scales, [s[k]["median"] for s in per_scale], t, d)] if c is not None]
        binding = []
        if unf is not None:
            s = next(s for s in per_scale if s["scale"] == unf)
            binding = [k for k, v in s["fail"]["unfavorable"].items() if v and (dim is None or thr[k][2] == dim)]
        failure[dim or "overall"] = {
            "scale": unf, "members": None if unf is None else int(round(unf * blocks.registered)),
            "median_first": first("median", dim), "certain_first": first("favorable", dim),
            "point": min(crosses) if crosses else None, "interval": [unf, first("favorable", dim)],
            "binding": binding, "binding_labels": [LABELS[b] for b in binding]}
    suspicious = []
    ov = failure["overall"]
    if ov["scale"] is not None and ov["scale"] <= 1.0:
        suspicious.append(f"1×（{blocks.registered} 人）就失控：与线上实际运行不符，模型可疑")
    s15 = next((s for s in per_scale if abs(s["scale"] - 1.5) < 1e-9), None)
    if s15 is not None:
        far = True
        for k, (t, d, dm) in thr.items():
            if dm != "experience":
                continue
            v = s15[k]["hi"] if d == ">" else s15[k]["lo"]
            far &= v is not None and (v < 0.5 * t if d == ">" else v > 2 * t)
        if far:
            suspicious.append(f"1.5×（{int(round(1.5 * blocks.registered))} 人）时体验指标的不利侧都不到阈值一半（远未失控），"
                              f"与专家估计 {EXPERT_RANGE[0]}–{EXPERT_RANGE[1]} 人不符，模型可疑")
    if theory and theory.get("members") and ov["members"]:
        ratio = ov["members"] / theory["members"]
        if not 0.5 <= ratio <= 2:
            suspicious.append(f"失控人数 {ov['members']} 与理论容量 {theory['members']:.0f} 相差 {ratio:.1f} 倍，模型可疑")
    return {
        "scales": per_scale, "failure": failure, "thresholds": {k: list(v) for k, v in thr.items()},
        "not_modeled": NOT_MODELED, "theory": theory, "suspicious": suspicious,
        "registered": blocks.registered, "days": [{"day": d["day"], "active": d["active"], "coverage": d["coverage"]}
                                                    for d in blocks.days],
        "blocks": blocks.n_blocks, "partial_data": blocks.partial, "requests_per_block": blocks.requests_per_block,
        "retry_model": {"by_attempt": blocks.retry["by_attempt"], "share_retried": blocks.retry["share_retried"]},
        "replicates_requested": replicates, "R_by_scale": {str(s["scale"]): s["R"] for s in per_scale},
        "seed": seed, "jitter_min": jitter_min, "seconds": round(seconds, 1), "params": p,
        "interval_method": "嵌套 bootstrap：G 组（数据重抽 + 参数后验）× 每组 10 天；区间 = 组中位数的 2.5–97.5% 分位（典型日），"
                           "失控按区间不利一侧判定；days_band = 全部天的分位（坏日子，只描述）",
        "G_by_scale": {str(s["scale"]): s["G"] for s in per_scale},
    }


def sweep(blocks: Blocks, base_params: Optional[dict], name: str, values: list, **kw) -> dict[str, Any]:
    """单参数扫描（参数网格须事先登记，见 backtest.register_grid）：每个值跑一遍 run()，共同随机数。"""
    out = []
    for v in values:
        prm = dict(base_params or {})
        prm[name] = v
        res = run(blocks, prm, **kw)
        out.append({"value": v, "failure": res["failure"], "R_by_scale": res["R_by_scale"], "seconds": res["seconds"],
                    "curve": [{"scale": s["scale"], "member_reject_rate": s["member_reject_rate"],
                               "first_to_image_p90": s["first_to_image_p90"]} for s in res["scales"]]})
    return {"param": name, "values": out}
