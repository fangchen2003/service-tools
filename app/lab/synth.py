"""合成数据：生成一个与线上同表结构的 SQLite（只含 lab 用到的表），用于开发与测试。

⚠ 合成数据只能做「自洽检验」（管线是否把自己生成的东西还原回来），不能当验证：结果是 replay 在「真值参数」下产生的，
所以回放它当然会对得上。库里写 site_settings.lab_synthetic=1，报告会标出「合成数据」。

到达过程（seed 固定）：40 位登记成员，每人每天以概率 p_i 活跃；1 + Poisson(1) 个会话，开始时刻来自「午间 + 晚间」双峰；
会话内连续出图，间隔 = 15 秒 + Exp(均值 25 秒)；每天张数 ~ 对数正态（重尾）；生成耗时 ~ 对数正态（中位数约 9 秒）。
被拒后的客户端自动重试按 RETRY_TRUTH（第 k 次被拒后重试概率 0.75 / 0.6 / 0.4，间隔 2–30 秒）。
可选 deploy_at：在该时刻「部署」——ver 列从 2.0.1 变 2.0.2、每小时上限从 cap_before 变成真值、V5 用完的状态码 429→402，
并写入 admin_actions / share_evidence / req_features 的少量样例。
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np

from . import replay as rp

SCHEMA = """
CREATE TABLE api_keys (id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '', token TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1, daily_images INTEGER NOT NULL DEFAULT 150, daily_v5 INTEGER NOT NULL DEFAULT 0,
  is_admin INTEGER NOT NULL DEFAULT 0, is_test INTEGER NOT NULL DEFAULT 0, quota_auto INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL DEFAULT 0);
CREATE TABLE usage_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, key_id INTEGER, key_name TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, images INTEGER NOT NULL DEFAULT 0,
  anlas REAL NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '',
  wait_ms INTEGER NOT NULL DEFAULT 0, dur_ms INTEGER NOT NULL DEFAULT 0, client TEXT NOT NULL DEFAULT '',
  up_status INTEGER NOT NULL DEFAULT 0, rid TEXT NOT NULL DEFAULT '', src TEXT NOT NULL DEFAULT '', ver TEXT NOT NULL DEFAULT '');
CREATE TABLE counters (key_id INTEGER NOT NULL, day TEXT NOT NULL, images INTEGER NOT NULL DEFAULT 0,
  legacy_free_images INTEGER NOT NULL DEFAULT 0, anlas REAL NOT NULL DEFAULT 0, text_tokens INTEGER NOT NULL DEFAULT 0,
  requests INTEGER NOT NULL DEFAULT 0, v5 INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (key_id, day));
CREATE TABLE site_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE admin_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', ok INTEGER NOT NULL DEFAULT 1);
CREATE TABLE share_evidence (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, key_id INTEGER NOT NULL, kind TEXT NOT NULL,
  points REAL NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '');
CREATE TABLE share_state (key_id INTEGER PRIMARY KEY, score REAL NOT NULL DEFAULT 0, score_ts REAL NOT NULL DEFAULT 0,
  strikes INTEGER NOT NULL DEFAULT 0, paused_until REAL NOT NULL DEFAULT 0, warned_ts REAL NOT NULL DEFAULT 0,
  paused_ts REAL NOT NULL DEFAULT 0);
CREATE TABLE req_features (ts REAL NOT NULL, key_id INTEGER NOT NULL, src TEXT NOT NULL DEFAULT '', fp TEXT NOT NULL DEFAULT '',
  os TEXT NOT NULL DEFAULT '', sig TEXT NOT NULL DEFAULT '', toks TEXT NOT NULL DEFAULT '', busy INTEGER NOT NULL DEFAULT 0);
"""
MESSAGES = {
    "daily_account": "429 本站今天的出图总量已达上限（每个账号 {daily} 张/天，用来保护上游账号），明天 0 点恢复",
    "hourly": "429 本小时出图量已达上限（每小时 {hourly} 张，用来保护上游账号），约 12 分钟后有空位",
    "cap3h": "429 最近 3 小时出图量已达上限（{cap3} 张，用来保护上游账号），约 20 分钟后有空位",
    "key_queue": "429 你的上一张图还没出完：每把 Key 同时只生成 1 张、最多再排 1 张，请等前面的完成后再发",
    "site_queue": "429 当前排队的人太多（全站最多同时排 5 张），请稍后再试",
    "member_daily": "402 今日 V4.5 及以下免费图额度已用完（150 张/天），明日恢复",
    "base": "429 今天的保底 100 张已用完。全站空闲时可以继续用到 150 张，现在有其他人在排队或用量较高，请过几分钟再试",
    "timeout": "429 图片任务排队超时，请稍后再试",
    "other": "{v5code} 已达今日 V5 额度（10 张/天），明天恢复后再用",
}
RETRY_TRUTH = {"p": [0.75, 0.6, 0.4], "delays": np.linspace(2, 30, 15)}


def _diurnal(rng: np.random.Generator, size: int) -> np.ndarray:
    which = rng.random(size)
    h = np.where(which < 0.35, rng.normal(13.5, 1.5, size),
                 np.where(which < 0.90, rng.normal(21.5, 2.0, size), rng.uniform(0, 24, size)))
    return np.clip(h, 0, 23.9) * 3600


def generate(path: str, *, members: int = 40, days: int = 9, start: str = "2026-10-02 00:00",
             end: Optional[str] = None, seed: int = 7, tz: str = "Asia/Shanghai", volume: float = 14.0,
             truth: Optional[dict] = None, deploy_at: Optional[str] = None, cap_before: int = 80,
             retry: Optional[dict] = None) -> dict:
    """写一个合成库到 path，返回摘要。start / end / deploy_at 为本地时间 "YYYY-MM-DD HH:MM"。"""
    rng = np.random.default_rng(seed)
    zone = ZoneInfo(tz)
    loc = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=zone).timestamp()
    t0 = loc(start)
    day0 = loc(start[:10] + " 00:00")
    t_end = loc(end) if end else day0 + days * 86400
    t_dep = loc(deploy_at) if deploy_at else None
    p_active = rng.beta(4, 2, members)
    vol = rng.lognormal(math.log(volume), 0.7, members)
    v5_share = rng.uniform(0, 0.2, members)
    rows = []
    for d in range(days + 1):
        base = day0 + d * 86400
        for m in range(members):
            if rng.random() > p_active[m]:
                continue
            target = max(1, int(rng.lognormal(math.log(vol[m]), 0.5)))
            sessions = 1 + rng.poisson(1.0)
            starts = _diurnal(rng, sessions)
            per = (np.diff(np.concatenate([[0], np.sort(rng.choice(target + sessions, sessions - 1, replace=False)), [target]]))
                   if sessions > 1 else np.array([target]))
            for s_tod, k in zip(starts, per):
                t = base + s_tod
                for _ in range(max(1, int(k))):
                    rows.append((t, m + 1, float(rng.lognormal(math.log(9), 0.25)), rng.random() < v5_share[m],
                                 rng.random() < 0.01, False))
                    t += 20 + rng.exponential(45)
        for _ in range(rng.poisson(3)):
            rows.append((base + rng.uniform(9, 23) * 3600, members + 2, float(rng.lognormal(math.log(9), .25)), False, False, True))
    rows = [r for r in rows if t0 <= r[0] < t_end]
    rows.sort()
    params = rp.resolve(truth or {})
    off = datetime.fromtimestamp(t0, zone).utcoffset().total_seconds()
    spec = retry or RETRY_TRUTH
    phases = [(rows, params, "2.0.2", 402)] if t_dep is None else [
        ([r for r in rows if r[0] < t_dep], {**params, "account_hourly_cap": cap_before}, "2.0.1", 429),
        ([r for r in rows if r[0] >= t_dep], params, "2.0.2", 402)]

    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    for m in range(1, members + 1):
        con.execute("INSERT INTO api_keys(id, name, token, daily_images, created_at) VALUES (?,?,?,?,?)",
                    (m, f"member{m:02d}", f"tok{m}", int(params["quota_target_avg"]), t0))
    con.execute("INSERT INTO api_keys(id, name, token, is_test, created_at) VALUES (?,?,?,1,?)", (members + 1, "test", "tt", t0))
    con.execute("INSERT INTO api_keys(id, name, token, is_admin, created_at) VALUES (?,?,?,1,?)", (members + 2, "admin", "ta", t0))
    counters: dict[tuple, list] = {}
    srcs = ["120.235.*.*", "36.112.*.*", "183.6.*.*", "2408:84*"]
    clients = ["NAIS2/1.4", "SillyTavern", "ComfyUI", "Mozilla/5.0"]
    total = {"requests": 0, "ok": 0, "retries": 0}
    for prows, prm, ver, v5code in phases:
        if not prows:
            continue
        n = len(prows)
        inp = {"arrival": np.array([r[0] for r in prows]), "key": np.array([r[1] for r in prows], dtype=np.int64),
               "dur": np.array([r[2] for r in prows]), "images": np.ones(n, dtype=np.int64),
               "legacy": ~np.array([r[3] for r in prows], dtype=bool), "ok": rng.random(n) > 0.01,
               "exo": np.array([r[4] for r in prows], dtype=bool), "priv": np.array([r[5] for r in prows], dtype=bool),
               "cap": np.full(n, -1, dtype=np.int64)}
        res = rp.replay(inp, prm, seed=seed, tz_offset=off, retry=spec)
        msg = {k: v.format(daily=prm["account_daily_cap"], hourly=prm["account_hourly_cap"], cap3=prm["account_3h_cap"],
                           v5code=v5code) for k, v in MESSAGES.items()}
        chain = res["chain"]
        for i in range(len(res["outcome_all"])):
            h = int(chain[i])
            code = int(res["outcome_all"][i])
            k = int(inp["key"][h])
            arrival = float(res["arrival_all"][i])
            lg = bool(inp["legacy"][h])
            model = "nai-diffusion-5" if not lg else "nai-diffusion-4-5-full"
            detail_ok = f"832x1216/{28 if (i % 7) else 23}step " + ("V5额度+1" if not lg else "免费")
            src, client = srcs[k % len(srcs)], clients[k % len(clients)]
            total["requests"] += 1
            total["retries"] += int(res["attempt_all"][i] > 1)
            if code <= rp.R_ERR:
                s = float(res["start_all"][i])
                dur = float(inp["dur"][h])
                status = "ok" if code == rp.R_OK else "error"
                con.execute("INSERT INTO usage_log(ts,key_id,key_name,kind,model,status,images,detail,wait_ms,dur_ms,client,"
                            "src,up_status,ver) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (s + dur, k, f"k{k}", "image", model, status, 1 if status == "ok" else 0,
                             detail_ok if status == "ok" else "upstream 500", int(round((s - arrival) * 1000)),
                             int(round(dur * 1000)), client, src, 200 if status == "ok" else 500, ver))
                if status == "ok" and k <= members:
                    total["ok"] += 1
                    day = datetime.fromtimestamp(s + dur, zone).strftime("%Y-%m-%d")
                    c = counters.setdefault((k, day), [0, 0])
                    c[0] += 1
                    c[1] += 0 if lg else 1
            else:
                t_rej = float(res["end_all"][i]) if not np.isnan(res["end_all"][i]) else arrival
                con.execute("INSERT INTO usage_log(ts,key_id,key_name,kind,model,status,images,detail,wait_ms,client,src,ver) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (t_rej, k, f"k{k}", "image", model, "rejected", 0, msg[rp.NAME[code]],
                             int(round((t_rej - arrival) * 1000)), client, src, ver))
            con.execute("INSERT INTO req_features(ts,key_id,src,fp,os,sig,toks,busy) VALUES (?,?,?,?,?,?,?,?)",
                        (arrival, k, src, f"fp{k % 5}", "win", f"sig{k % 3}", f"{ver}-{h}", 0))   # 重试与原请求同一哈希
    for (k, day), (img, v5) in counters.items():
        con.execute("INSERT INTO counters(key_id, day, images, legacy_free_images, v5) VALUES (?,?,?,?,?)",
                    (k, day, img, img - v5, v5))
    if t_dep is not None:
        con.execute("INSERT INTO admin_actions(ts, actor, action, target, detail) VALUES (?,?,?,?,?)",
                    (t_dep + 120, "系统", "自动调整：每小时上限", "", f"{cap_before} → {params['account_hourly_cap']}"))
    ev_rng = np.random.default_rng(seed + 1)
    for d in range(days + 1):
        for _ in range(ev_rng.poisson(2)):
            ts = day0 + d * 86400 + ev_rng.uniform(0, 86400)
            if t0 <= ts < t_end:
                kind = ["alternate", "multi_device", "overlap", "allday"][int(ev_rng.integers(0, 4))]
                con.execute("INSERT INTO share_evidence(ts,key_id,kind,points,detail) VALUES (?,?,?,?,?)",
                            (ts, int(ev_rng.integers(1, members + 1)), kind, 15.0, "合成样例"))
    settings = {"guard_" + k: params[k] for k in ("account_daily_cap", "account_hourly_cap", "account_3h_cap",
                                                   "interval_jitter", "key_image_queue", "queue_per_account",
                                                   "base_daily_images", "quiet_start", "quiet_end", "quiet_hourly_cap")}
    settings.update({"quota_ceiling": params["quota_target_avg"], "quota_base_now": params["base_daily_images"],
                     "lab_synthetic": 1, "share_guard_mode": "observe"})
    con.executemany("INSERT INTO site_settings(key, value) VALUES (?,?)", [(k, str(v)) for k, v in settings.items()])
    con.commit()
    con.close()
    return {"path": path, **total, "params": params}
