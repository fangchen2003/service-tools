"""确定性回放：给定一组参数，按真实到达时间重放「新需求」，重试由显式模型生成（离散事件仿真，同一 seed 逐位相同）。

━━ 模型（对照线上代码路径，main.py / state.py / nai.py / guard.py）━━
  到达 a
   → ① 每 Key 出图节奏（state.wait_for_key_image_slot）：r = max(a, 这把 Key 上次占位 + key_image_min_interval)，超过 queue_timeout → timeout
   → ② 账号保护预检（guard.token_block_reason）：账号日上限（按成功张数）、每小时上限（滚动 60 分钟发往上游的次数，
       安静时段用 quiet_hourly_cap）、3 小时上限 → daily_account / hourly / cap3h
   → ③ 排队准入（guard.admit_image）：这把 Key 进行中 ≥ 1 + key_image_queue → key_queue；全站进行中 ≥ queue_per_account × 账号数 → site_queue
   → ④ 每 Key 并发 1（key_sem）：等这把 Key 上一张完成，超过 queue_timeout → timeout
   → ⑤ 额度（main.quota_image_check，只对 V4.5 免费图）：当天已用（含进行中预留）+ n > A → member_daily；
       > 保底 B 且全站不空闲（guard.site_idle）→ base
   → ⑥ 选账号（nai.pick_token 再查一次 ②）→ 单服务台 FIFO：
       service_mode="max"（默认，nai.py:349 的实际实现）：开始 s = max(上一张完成, 上一张开始 + 间隔 + U(0, 抖动))，
         即服务周期 = max(生成耗时, 间隔 + 抖动)，间隔从开工算起；
       service_mode="sum"：s = 上一张完成 + 间隔 + 抖动（对照用，不是线上行为）。
       进入队列到开始超过 queue_timeout → timeout；完成 f = s + 实测耗时。
  被拒（建模原因）后按重试模型：第 k 次被拒后以概率 p_k 在「实测重试间隔」后再请求一次，否则放弃。
  站长 Key 跳过 ①③④⑤。日志里因未建模原因被拒的请求（V5 额度、Anlas……）计为 other，不重试。

━━ 指标（统计单位 = 成员-天）━━
  成员体验：每个成员-天的新请求失败率（需求链因容量拒绝最终没出图 ÷ 新请求）的平均（按人计）；首次请求到出图 p90；
            当天被容量拒绝 ≥ 3 次的活跃成员比例。容量拒绝 = 账号日 / 小时 / 3 小时上限、全站排队、保底不空闲、排队超时；
            每人日上限、每 Key 排队属于个人限制，不算容量失败。
  账号安全：当天成功总张数（对照基线）；连续满载小时数（成功张数 ≥ full_load_share × 每小时上限）；
            V5 剩余斜率代理 = 恢复 %/天 − V5 张数 / 每 1% 张数（<0 表示在消耗存量）。上游 429 / 5xx 尖峰回放无法模拟，只看真实日志。
  请求级（参考）：拒绝率、等待 p50/p90/p99、每小时峰值、触顶小时数、日上限用尽时刻。
"""
from __future__ import annotations

import heapq
import math
import random
from bisect import bisect_right
from collections import deque
from dataclasses import fields as dc_fields
from typing import Any, Optional

import numpy as np

from . import stats
from .data import CAPACITY, MODELED, Dataset, clean_mask, retry_model, segments

REASONS = ("daily_account", "hourly", "cap3h", "key_queue", "site_queue", "member_daily", "base", "timeout", "other",
           "paced")       # paced 放最后：已有原因码的编号不变
R_OK, R_ERR = 0, 1
CODE = {name: i + 2 for i, name in enumerate(REASONS)}
NAME = {v: k for k, v in CODE.items()}
MODELED_CODES = frozenset(CODE[r] for r in MODELED)
CAPACITY_CODES = frozenset(CODE[r] for r in CAPACITY)
MAX_ATTEMPTS = 10
LAB_EXTRA = {
    # 名称: (默认, 说明, 来源)
    "image_min_interval": (15.0, "上游账号两次出图最小间隔（秒，从开工算起）", "app/config.py IMAGE_MIN_INTERVAL"),
    "key_image_min_interval": (15.0, "每把 Key 两次出图最小间隔（秒）", "app/config.py KEY_IMAGE_MIN_INTERVAL"),
    "queue_timeout": (90.0, "排队最长等待（秒）", "app/config.py QUEUE_TIMEOUT"),
    "accounts": (1, "可用上游账号数", "当前 1 个 NovelAI 账号"),
    "idle_share": (0.6, "空闲借用阈值", "app/guard.py IDLE_SHARE / private capacity.idle_share"),
    "service_mode": ("max", "服务周期：max = max(耗时, 间隔+抖动)（nai.py 实际）；sum = 耗时+间隔+抖动", "lab"),
    "safety_daily_baseline": (0, "账号安全：日总量基线（0 = 用 account_daily_cap）", "lab"),
    "full_load_share": (0.9, "满载：本小时成功张数 ≥ 该比例 × 每小时上限", "lab"),
    "v5_recharge_rate": (5.0, "V5 恢复 %/天（斜率代理，未核对时的保守值）", "app/quota_algo.py V5_FALLBACK_RATE"),
    "v5_images_per_percent": (14.2, "每 1% V5 ≈ 张", "app/quota_algo.py V5_IMAGES_PER_PERCENT"),
}


# ---------------------------------------------------------------- 参数
def default_params() -> dict[str, Any]:
    """从代码读默认值：guard.FIELDS、quota_algo.DEFAULTS / 常量、config.Settings、私密参数（app.params.P）。"""
    p: dict[str, Any] = {k: v[0] for k, v in LAB_EXTRA.items()}
    try:
        from app import guard
        p.update({k: v[0] for k, v in guard.FIELDS.items()})
        p["idle_share"] = guard.IDLE_SHARE
        try:
            from app.params import P
            p["idle_share"] = float(P("capacity.idle_share", guard.IDLE_SHARE))
        except Exception:      # pragma: no cover
            pass
    except Exception:          # pragma: no cover
        pass
    try:
        from app import quota_algo
        p.update(quota_algo.DEFAULTS)
        p["v5_recharge_rate"] = float(quota_algo.V5_FALLBACK_RATE)
        p["v5_images_per_percent"] = float(quota_algo.V5_IMAGES_PER_PERCENT)
    except Exception:          # pragma: no cover
        pass
    try:
        from app.config import Settings
        for f in dc_fields(Settings):
            if f.name in ("image_min_interval", "key_image_min_interval", "queue_timeout"):
                p[f.name] = float(f.default)
    except Exception:          # pragma: no cover
        pass
    return p


def current_params(ds: Dataset) -> dict[str, Any]:
    """副本里 site_settings 的当前生效值（guard_*、runtime_*、动态额度的当前 A / B）。"""
    p = default_params()
    s = ds.settings

    def num(raw):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    for k in list(p):
        if isinstance(p[k], str):
            continue
        for key in ("guard_" + k, "runtime_" + k, k):
            v = num(s.get(key))
            if v is not None:
                p[k] = type(p[k])(v)
                break
    a = num(s.get("quota_ceiling"))
    if a is not None:
        p["quota_target_avg"] = int(a)
    b = num(s.get("quota_base_now"))
    if b is not None and num(s.get("guard_base_daily_images")) is None:
        p["base_daily_images"] = int(b)
    return p


def resolve(params: Optional[dict]) -> dict[str, Any]:
    p = default_params()
    for k, v in (params or {}).items():
        if k not in p:
            raise KeyError(f"未知参数 {k}；可用：{sorted(p)}")
        p[k] = v
    return p


def retry_spec(model: Optional[dict], rng: Optional[np.random.Generator] = None) -> Optional[dict]:
    """重试模型 → 回放用的 {p: [p1, p2, p3+], delays}。给 rng 时从 Beta 后验抽一组（参数不确定性）。"""
    if not model:
        return None
    ps = [float(rng.beta(b["alpha"], b["beta"])) if rng is not None else b["p"] for b in model["by_attempt"]]
    return {"p": ps, "delays": np.asarray(model["delays"], dtype=float)}


# ---------------------------------------------------------------- 输入
def inputs_from_dataset(ds: Dataset, *, mask: Optional[np.ndarray] = None, seed: int = 0, use_key_caps: bool = True,
                        demand_only: bool = True) -> dict[str, np.ndarray]:
    """数据 → 回放输入（按到达排序）。demand_only：只取新需求（剔除重试，重试交给重试模型）。
    被拒请求没有生成耗时：从实测耗时分布按 seed 抽取。"""
    e = ds.ev
    m = np.ones(ds.n, dtype=bool) if mask is None else mask.copy()
    if demand_only:
        m &= ~e["retry"]
    served = (e["status"] != "rejected") & (e["dur_s"] > 0)
    pool = e["dur_s"][served]
    if len(pool) == 0:
        pool = np.array([10.0])
    rng = np.random.default_rng(seed)
    dur = e["dur_s"].astype(float).copy()
    fill = dur <= 0
    dur[fill] = rng.choice(pool, size=int(fill.sum()))
    exo = (e["status"] == "rejected") & (e["reason"] == "other")
    caps = np.full(ds.n, -1, dtype=np.int64)
    if use_key_caps:
        for i, k in enumerate(e["key"]):
            info = ds.keys.get(int(k))
            if info and info.get("quota_auto", 1) == -1:
                caps[i] = int(info.get("daily_images") or 0)
    idx = np.nonzero(m)[0]
    return {"arrival": e["arrival"][idx].astype(float), "key": e["key"][idx].astype(np.int64), "dur": dur[idx],
            "images": np.maximum(1, e["images"][idx]).astype(np.int64), "legacy": ~e["v5"][idx],
            "ok": (e["status"] != "error")[idx], "exo": exo[idx], "priv": e["admin"][idx], "cap": caps[idx],
            "src_index": idx}


# ---------------------------------------------------------------- 回放核心
def replay(inp: dict[str, np.ndarray], params: Optional[dict] = None, *, seed: int = 0, tz_offset: float = 28800.0,
           retry: Optional[dict] = None, detail: bool = True) -> dict[str, Any]:
    p = resolve(params)
    n0 = len(inp["arrival"])
    arr0 = inp["arrival"].tolist()
    A = inp["arrival"].tolist()
    keys = inp["key"].tolist()
    durs = inp["dur"].tolist()
    imgs = inp["images"].tolist()
    legacy = inp["legacy"].tolist()
    okflag = inp["ok"].tolist()
    exo = inp["exo"].tolist()
    priv = inp["priv"].tolist() if "priv" in inp else [False] * n0
    kcap = inp["cap"].tolist() if "cap" in inp else [-1] * n0
    chain = list(range(n0))
    att = [1] * n0

    acc = max(1, int(p["accounts"]))
    daily_cap = int(p["account_daily_cap"]) * acc
    hcap_normal = int(p["account_hourly_cap"]) * acc
    hcap_quiet = int(p["quiet_hourly_cap"]) * acc
    q_start, q_end = int(p["quiet_start"]), int(p["quiet_end"])
    cap3 = int(p["account_3h_cap"]) * acc
    burst = int(p.get("daily_burst", 0) or 0) * acc        # 按时段放行（guard.paced_allowance，v2.17.0 起）
    jit = float(p["interval_jitter"])
    kq = int(p["key_image_queue"])
    per = int(p["queue_per_account"]) * acc
    base = int(p["base_daily_images"])
    A_cap = int(p["quota_target_avg"])
    gap = float(p["image_min_interval"])
    kgap = float(p["key_image_min_interval"])
    qt = float(p["queue_timeout"])
    idle_share = float(p["idle_share"])
    summ = p["service_mode"] == "sum"
    rnd = random.Random(seed)
    off = float(tz_offset)
    r_p = list(retry["p"]) if retry else []
    r_d = list(map(float, retry["delays"])) if retry else []

    def day(t: float) -> int:
        return int((t + off) // 86400)

    def hour_cap(t: float) -> int:
        if q_start == q_end:
            return hcap_normal
        h = int(((t + off) % 86400) // 3600)
        quiet = q_start <= h < q_end if q_start < q_end else (h >= q_start or h < q_end)
        return hcap_quiet if quiet else hcap_normal

    outcome = [-1] * n0
    start = [math.nan] * n0
    end = [math.nan] * n0                  # 被拒时刻 / 完成时刻
    starts: list[float] = []
    acct_day: dict[int, int] = {}
    exhaust_at: dict[int, float] = {}
    used: dict[tuple, int] = {}
    reserved: dict[int, tuple] = {}
    key_next: dict[int, float] = {}
    inflight: dict[int, int] = {}
    active_keys = 0
    site_inflight = 0
    key_busy: dict[int, bool] = {}
    key_wait: dict[int, deque] = {}
    server_q: deque = deque()
    servers_free = acc
    token_next = -math.inf
    last_finish = -math.inf
    block_hours: set[int] = set()
    heap: list = []
    seq = 0
    FIN, TMO_KEY, TMO_SRV, SLOT_FREE, START, READY, ARR = 0, 1, 2, 3, 4, 5, 6
    WAIT_KEY, WAIT_SRV, DONE, SERVING = 1, 2, 3, 4
    state = [0] * n0

    def push(t, typ, i):
        nonlocal seq
        seq += 1
        heapq.heappush(heap, (t, typ, seq, i))

    def cnt(t: float, span: float) -> int:
        return len(starts) - bisect_right(starts, t - span)

    def cap_block(t: float) -> int:
        if daily_cap and acct_day.get(day(t), 0) >= daily_cap:
            return CODE["daily_account"]
        if daily_cap and burst:
            frac = ((t + off) % 86400) / 86400
            if acct_day.get(day(t), 0) >= min(daily_cap, burst + daily_cap * frac):
                return CODE["paced"]
        hc = hour_cap(t)
        if hc and cnt(t, 3600) >= hc:
            return CODE["hourly"]
        if cap3 and cnt(t, 10800) >= cap3:
            return CODE["cap3h"]
        return 0

    def reject(i: int, code: int, t: float) -> None:
        outcome[i] = code
        state[i] = DONE
        end[i] = t
        if code in (CODE["hourly"], CODE["daily_account"], CODE["paced"]):
            block_hours.add(int((t + off) // 3600))
        if r_p and code in MODELED_CODES and att[i] < MAX_ATTEMPTS:
            pk = r_p[min(att[i], len(r_p)) - 1]
            if rnd.random() < pk:                       # 客户端自动重试：同一把 Key、同一张图
                j = len(A)
                d = r_d[rnd.randrange(len(r_d))]
                for lst, v in ((A, t + d), (keys, keys[i]), (durs, durs[i]), (imgs, imgs[i]), (legacy, legacy[i]),
                               (okflag, okflag[i]), (exo, False), (priv, priv[i]), (kcap, kcap[i]), (chain, chain[i]),
                               (att, att[i] + 1), (outcome, -1), (start, math.nan), (end, math.nan), (state, 0)):
                    lst.append(v)
                push(t + d, ARR, j)

    def release(i: int, t: float, held_sem: bool) -> None:
        nonlocal site_inflight, active_keys
        r = reserved.pop(i, None)
        if r is not None and outcome[i] != R_OK:
            used[r] -= imgs[i]
        if priv[i]:
            return
        k = keys[i]
        inflight[k] -= 1
        site_inflight -= 1
        if inflight[k] == 0:
            active_keys -= 1
        if held_sem:
            w = key_wait.get(k)
            while w:
                j = w.popleft()
                if state[j] == WAIT_KEY:
                    budget(j, t)
                    return
            key_busy[k] = False

    def site_idle(k: int, t: float) -> bool:
        others = active_keys - (1 if inflight.get(k, 0) > 0 else 0)
        if others > 0:
            return False
        hc = hour_cap(t)
        return (not hc) or cnt(t, 3600) < idle_share * hc

    def budget(i: int, t: float) -> None:
        k = keys[i]
        if legacy[i] and not priv[i]:
            cap_k = kcap[i] if kcap[i] >= 0 else A_cap
            d = (k, day(t))
            u = used.get(d, 0)
            if cap_k and u + imgs[i] > cap_k:
                reject(i, CODE["member_daily"], t)
                release(i, t, True)
                return
            if base and base < cap_k and u + imgs[i] > base and not site_idle(k, t):
                reject(i, CODE["base"], t)
                release(i, t, True)
                return
            used[d] = u + imgs[i]
            reserved[i] = d
        c = cap_block(t)
        if c:
            reject(i, c, t)
            release(i, t, not priv[i])
            return
        state[i] = WAIT_SRV
        server_q.append((i, t))
        push(t + qt, TMO_SRV, i)
        dispatch(t)

    def dispatch(t: float) -> None:
        nonlocal servers_free, token_next
        while servers_free > 0 and server_q:
            i, e = server_q.popleft()
            if state[i] != WAIT_SRV:
                continue
            ready_at = last_finish + gap + (rnd.uniform(0, jit) if jit else 0.0) if summ else token_next
            s = max(t, ready_at)
            servers_free -= 1
            if s - e > qt:
                state[i] = DONE
                push(e + qt, SLOT_FREE, i)
                continue
            state[i] = SERVING
            token_next = s + gap + (rnd.uniform(0, jit) if jit else 0.0) if not summ else s
            push(s, START, i)

    def arrive(i: int, t: float) -> None:
        if exo[i]:
            outcome[i] = CODE["other"]
            state[i] = DONE
            return
        if priv[i]:
            c = cap_block(t)
            if c:
                reject(i, c, t)
                return
            budget(i, t)
            return
        k = keys[i]
        r = max(t, key_next.get(k, -math.inf))
        if r - t > qt:
            reject(i, CODE["timeout"], t + qt)
            return
        key_next[k] = r + kgap
        if r > t:
            push(r, READY, i)
        else:
            ready(i, t)

    def ready(i: int, t: float) -> None:
        nonlocal active_keys, site_inflight
        k = keys[i]
        c = cap_block(t)
        if c:
            reject(i, c, t)
            return
        mine = inflight.get(k, 0)
        if mine >= 1 + kq:
            reject(i, CODE["key_queue"], t)
            return
        if per and site_inflight >= per:
            reject(i, CODE["site_queue"], t)
            return
        inflight[k] = mine + 1
        if mine == 0:
            active_keys += 1
        site_inflight += 1
        if key_busy.get(k):
            state[i] = WAIT_KEY
            key_wait.setdefault(k, deque()).append(i)
            push(t + qt, TMO_KEY, i)
        else:
            key_busy[k] = True
            budget(i, t)

    ai = 0
    while ai < n0 or heap:
        if heap and (ai >= n0 or heap[0][0] <= arr0[ai]):
            t, typ, _, i = heapq.heappop(heap)
        else:
            t, typ, i = arr0[ai], ARR, ai
            ai += 1
        if typ == ARR:
            arrive(i, t)
        elif typ == READY:
            ready(i, t)
        elif typ == TMO_KEY:
            if state[i] == WAIT_KEY:
                reject(i, CODE["timeout"], t)
                release(i, t, False)
        elif typ == TMO_SRV:
            if state[i] == WAIT_SRV:
                reject(i, CODE["timeout"], t)
                release(i, t, not priv[i])
        elif typ == SLOT_FREE:
            reject(i, CODE["timeout"], t)
            servers_free += 1
            release(i, t, not priv[i])
            dispatch(t)
        elif typ == START:
            start[i] = t
            starts.append(t)
            push(t + durs[i], FIN, i)
        elif typ == FIN:
            outcome[i] = R_OK if okflag[i] else R_ERR
            state[i] = DONE
            end[i] = t
            if okflag[i]:
                d = day(t)
                acct_day[d] = acct_day.get(d, 0) + imgs[i]
                if daily_cap and acct_day[d] >= daily_cap and d not in exhaust_at:
                    exhaust_at[d] = t
            servers_free += 1
            last_finish = t
            release(i, t, not priv[i])
            dispatch(t)

    out = np.array(outcome, dtype=np.int64)
    res = summarize(out, np.array(start, dtype=float), np.array(A, dtype=float), np.array(imgs, dtype=np.int64),
                    np.array(legacy, dtype=bool), np.array(keys, dtype=np.int64), np.array(chain, dtype=np.int64),
                    np.array(priv, dtype=bool), p, tz_offset=off, block_hours=len(block_hours),
                    exhaust={d: ((t + off) % 86400) / 3600 for d, t in exhaust_at.items()},
                    finish=np.array(start, dtype=float) + np.array(durs, dtype=float))
    res["params"] = {k: p[k] for k in sorted(p)}
    res["seed"] = seed
    res["retries"] = len(A) - n0
    if detail:
        res["outcome"] = out[:n0]
        res["outcome_all"] = out
        res["start"] = np.array(start[:n0], dtype=float)
        res["chain"] = np.array(chain, dtype=np.int64)
        res["start_all"] = np.array(start, dtype=float)
        res["arrival_all"] = np.array(A, dtype=float)
        res["attempt_all"] = np.array(att, dtype=np.int64)
        res["end_all"] = np.array(end, dtype=float)
    return res


def summarize(outcome: np.ndarray, start: np.ndarray, arrival: np.ndarray, images: np.ndarray, legacy: np.ndarray,
              key: np.ndarray, chain: np.ndarray, priv: np.ndarray, p: dict, *, tz_offset: float, block_hours: int,
              exhaust: dict[int, float], finish: Optional[np.ndarray] = None) -> dict[str, Any]:
    """同口径汇总（回放结果与真实日志都用这一个函数）。chain[i] = 该请求所属需求链第一条的下标。"""
    n = len(outcome)
    served = outcome <= R_ERR
    ok = outcome == R_OK
    modeled_mask = np.isin(outcome, list(MODELED_CODES))
    cap_mask = np.isin(outcome, list(CAPACITY_CODES))      # 容量拒绝（不含每人上限、每 Key 排队这类个人限制）
    rej = {name: int(np.sum(outcome == code)) for name, code in CODE.items()}
    denom = n - rej["other"]
    wait = (start - arrival)[served]
    q = (lambda a, x: float(np.quantile(a, x)) if len(a) else None)
    off = tz_offset
    hours = ((start[ok] + off) // 3600).astype(np.int64)
    per_hour = np.bincount(hours - hours.min(), weights=images[ok]) if len(hours) else np.zeros(0)
    peak = int(per_hour.max()) if len(per_hour) else 0

    # ---- 成员-天（需求链）----
    fin = start if finish is None else finish
    head_arr = arrival[chain]
    unit_rows: dict[tuple, list] = {}
    first_to_img = []
    if n:
        order = np.argsort(chain, kind="mergesort")
        groups = np.split(order, np.flatnonzero(np.diff(chain[order])) + 1)
        for g in groups:
            h = int(chain[g[0]])
            if priv[h] or outcome[h] == CODE["other"]:
                continue
            u = (int(key[h]), int((head_arr[h] + off) // 86400))
            rec = unit_rows.setdefault(u, [0, 0, 0])          # 新请求, 失败（最终没出图且有建模拒绝）, 被拒次数
            rec[0] += 1
            rec[2] += int(cap_mask[g].sum())
            oks = g[ok[g]]
            if len(oks):
                first_to_img.append(float(fin[oks].min() - head_arr[h]))      # 首次请求 → 出图（含生成耗时）
            elif cap_mask[g].any() and not served[g].any():
                rec[1] += 1
    ud = np.array(list(unit_rows.values()), dtype=float).reshape(-1, 3)

    # ---- 账号安全 ----
    hc = int(p["account_hourly_cap"]) * max(1, int(p["accounts"]))
    run = best = 0
    if hc:
        for v in per_hour:
            run = run + 1 if v >= float(p["full_load_share"]) * hc else 0
            best = max(best, run)
    daily: dict[int, int] = {}
    v5d: dict[int, int] = {}
    for d, im, lg in zip(((start[ok] + off) // 86400).astype(np.int64).tolist(), images[ok].tolist(), legacy[ok].tolist()):
        daily[d] = daily.get(d, 0) + im
        if not lg:
            v5d[d] = v5d.get(d, 0) + im
    rate, ipp = float(p["v5_recharge_rate"]), float(p["v5_images_per_percent"])
    return {
        "n": int(n), "served": int(served.sum()), "ok": int(ok.sum()), "errors": int(np.sum(outcome == R_ERR)),
        "images_ok": int(images[ok].sum()), "rejects": rej, "rejected_modeled": int(modeled_mask.sum()),
        "reject_rate": int(modeled_mask.sum()) / denom if denom else 0.0, "denominator": int(denom),
        "wait_p50": q(wait, .5), "wait_p90": q(wait, .9), "wait_p99": q(wait, .99),
        "wait_mean": float(wait.mean()) if len(wait) else None,
        "hourly_peak": peak, "hours_at_cap": int(block_hours),
        "exhaust_hour": min(exhaust.values()) if exhaust else None, "exhaust_days": len(exhaust),
        # 成员体验（成员-天）
        "member_days": int(len(ud)), "new_requests": int(ud[:, 0].sum()) if len(ud) else 0,
        "member_reject_rate": float(np.mean(ud[:, 1] / ud[:, 0])) if len(ud) else 0.0,
        "first_to_image_p90": q(np.array(first_to_img), .9),
        "share_members_rej3": float(np.mean(ud[:, 2] >= 3)) if len(ud) else 0.0,
        # 账号安全
        "daily_total_max": int(max(daily.values())) if daily else 0, "full_load_run_hours": int(best),
        "v5_slope_min": float(min((rate - v5d.get(d, 0) / ipp for d in daily), default=rate)),
    }


# ---------------------------------------------------------------- 实际日志的同口径指标 + 校准
def outcome_codes(ds: Dataset) -> np.ndarray:
    e = ds.ev
    code = np.full(ds.n, R_OK, dtype=np.int64)
    code[e["status"] == "error"] = R_ERR
    rej = e["status"] == "rejected"
    for name, c in CODE.items():
        code[rej & (e["reason"] == name)] = c
    return code


def actual_metrics(ds: Dataset, params: Optional[dict] = None, mask: Optional[np.ndarray] = None) -> dict[str, Any]:
    e = ds.ev
    m = np.ones(ds.n, dtype=bool) if mask is None else mask
    idx = np.nonzero(m)[0]
    code = outcome_codes(ds)[idx]
    start = np.where(code <= R_ERR, e["ts"][idx] - e["dur_s"][idx], np.nan)
    # 链编号映射到子集内的下标（链头被剔除时，子集里该链的第一条当链头）
    first: dict[int, int] = {}
    chain = np.empty(len(idx), dtype=np.int64)
    for j, c in enumerate(e["chain"][idx].tolist()):
        chain[j] = first.setdefault(c, j)
    off = ds.tz_offset() if ds.n else 28800.0
    hours = set()
    rej = e["status"][idx] == "rejected"
    for t, r in zip(e["ts"][idx][rej], e["reason"][idx][rej]):
        if r in ("hourly", "daily_account"):
            hours.add(int((t + off) // 3600))
    p = resolve(params)
    res = summarize(code, start, e["arrival"][idx], np.maximum(1, e["images"][idx]), ~e["v5"][idx], e["key"][idx], chain,
                    e["admin"][idx], p, tz_offset=off, block_hours=len(hours), exhaust={}, finish=e["ts"][idx])
    up = e["up_status"][idx]
    res["upstream_429"] = int(np.sum(up == 429))
    res["upstream_5xx"] = int(np.sum(up >= 500))
    res["hourly_caps_seen_in_messages"] = sorted({int(c) for c in e["cap_in_msg"][idx] if c})
    res["outcome"] = code
    return res


CALIB_METRICS = ("ok", "images_ok", "rejected_modeled", "reject_rate", "member_reject_rate", "first_to_image_p90",
                 "share_members_rej3", "wait_p90", "hourly_peak", "hours_at_cap")


def calibrate(ds: Dataset, params: Optional[dict] = None, *, seed: int = 0, exclude_deploys: bool = True) -> dict[str, Any]:
    """交叉校验：按规则版本分段，每段用「当前参数 + 段内从文案推断的参数」回放新需求 + 重试模型，与实际日志逐项对照。
    重试模型从同一份数据估计（样本内）；样本外检验见 backtest.py。"""
    base = params if params is not None else current_params(ds)
    keep = clean_mask(ds) if exclude_deploys else np.ones(ds.n, dtype=bool)
    rm = retry_model(ds, keep)
    spec = retry_spec(rm)
    seg_out = []
    pa = {"ok": 0, "rejected_modeled": 0, "denominator": 0}
    pp = dict(pa)
    for seg in segments(ds):
        m = seg["mask"] & keep
        if m.sum() < 5:
            continue
        p = {**base, **seg["inferred"]}
        pred = replay(inputs_from_dataset(ds, mask=m, seed=seed), p, seed=seed, tz_offset=ds.tz_offset(), retry=spec)
        act = actual_metrics(ds, p, m)
        rows = []
        for k in CALIB_METRICS + tuple("reject:" + r for r in MODELED):
            a = act["rejects"][k[7:]] if k.startswith("reject:") else act[k]
            b = pred["rejects"][k[7:]] if k.startswith("reject:") else pred[k]
            diff = None if a is None or b is None else b - a
            rows.append({"metric": k, "actual": a, "predicted": b, "diff": diff,
                         "rel": None if diff is None or not a else diff / a})
        for k in pa:
            pa[k] += act[k]
            pp[k] += pred[k]
        seg_out.append({"index": seg["index"], "start": seg["start"], "end": seg["end"], "versions": seg["versions"],
                        "inferred": seg["inferred"], "n": int(m.sum()), "rows": rows,
                        "retries_actual": int(ds.ev["retry"][m].sum()), "retries_predicted": pred["retries"]})
    ar = pa["rejected_modeled"] / pa["denominator"] if pa["denominator"] else None
    pr = pp["rejected_modeled"] / pp["denominator"] if pp["denominator"] else None
    _, lo, hi = stats.wilson(pa["rejected_modeled"], pa["denominator"])
    notes = []
    caps = sorted({int(c) for c in ds.ev["cap_in_msg"] if c})
    if len(caps) > 1 or (caps and caps[0] != int(base["account_hourly_cap"])):
        notes.append(f"拒绝文案里出现过每小时上限 {caps}；已按规则版本分段回放，每段用文案推断的上限")
    if exclude_deploys:
        notes.append(f"剔除部署 / 规则变更前后 30 分钟：{int((~keep).sum())} 条")
    return {"params": base, "segments": seg_out,
            "pooled": {"actual_reject_rate": ar, "predicted_reject_rate": pr, "actual_ok": pa["ok"], "predicted_ok": pp["ok"]},
            "actual_reject_rate_ci": [lo, hi], "ci_method": "Wilson 95%（请求级，未做聚类校正，仅作参考）",
            "abs_err_reject_rate": None if ar is None or pr is None else abs(pr - ar),
            "rel_err_ok": (pp["ok"] - pa["ok"]) / pa["ok"] if pa["ok"] else None,
            "retry_model": {"by_attempt": rm["by_attempt"], "share_retried": rm["share_retried"],
                            "delay_median": float(np.median(rm["delays"])), "n_rejections": rm["n_rejections"]},
            "notes": notes, "n": int(keep.sum()), "in_sample": True}
