"""从 SQLite 副本读数据（只读：mode=ro URI）。不写库、不连线上。

━━ 事件流 ━━
usage_log 中 kind LIKE 'image%' 的记录（与 guard.seed_hour 口径一致），每条一个请求：
  ts（日志时间 = 完成 / 拒绝时刻）、arrival（到达 = ts − dur − wait，与 shadow.py 一致）、key、kind、model、status、
  images、wait_s、dur_s、src、client、up_status、ver（v2.0.2 起的网关版本）、width / height / steps（detail 的「832x1216/28step」）、
  reason（拒绝原因分类，见 REASON_PATTERNS）、cap_in_msg（拒绝文案里的上限数值，例如「每小时 80 张」）、
  reject_code（拒绝文案开头的 HTTP 码，例如 V5 用完 429→402）。

━━ 规则版本与部署（统计专家意见 1）━━
  · 有 ver 列：版本变化时刻 = 部署。
  · 没有 ver 的旧数据：按拒绝文案推断规则版本——每小时上限的数值变了（80→150）、同一类拒绝的 HTTP 码变了（429→402），
    都记为一次「规则变更」（视同部署）。
  · admin_actions 里的设置调整记为「参数变更」（只标注，不剔除）。
  · 部署 / 规则变更前后 30 分钟的请求剔除（clean_mask），回放按规则版本分段（segments）。

━━ 重试（统计专家意见 3）━━
  被拒后 60 秒内同一把 Key 再次请求「同一张图」= 客户端自动重试（真实数据里 60 秒内再请求占 71%）。
  「同一张图」：有 req_features（v2.0.1 起）时要求参数签名 sig 与提示词哈希 toks 相同（按 Key + 时间 ±5 秒匹配）；
  没有特征时退回「同一模型」（较弱，会把紧接着的新请求也算成重试；结果里报告特征覆盖率）。
  mark_retries 把请求串成「需求链」：
  链的第一条是新需求，后面是重试。retry_model 从数据估计：第 k 次被拒后重试的概率（Beta 后验）与重试间隔的经验分布。
成员：api_keys 中 is_test=0 且 is_admin=0。站长 Key 在线上跳过排队与额度，回放里同样跳过。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import numpy as np

# 拒绝原因：按顺序匹配 detail（线上文案见 guard.token_block_reason / guard.admit_image / main.quota_image_check）
REASON_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("cap3h", ("最近 3 小时出图量已达上限",)),
    ("hourly", ("本小时出图量已达上限", "安静时段")),
    ("daily_account", ("出图总量已达上限",)),
    ("paced", ("按时段放行",)),
    ("key_queue", ("上一张图还没出完",)),
    ("site_queue", ("排队的人太多",)),
    ("base", ("保底",)),
    ("member_daily", ("免费图额度已用完",)),
    ("timeout", ("排队超时", "排队人数过多")),
]
MODELED = ("daily_account", "hourly", "cap3h", "key_queue", "site_queue", "member_daily", "base", "timeout", "paced")
CAPACITY = ("daily_account", "hourly", "cap3h", "site_queue", "base", "timeout", "paced")     # 「容量」拒绝（不含个人上限与个人排队）
REASON_LABELS = {
    "paced": "按时段放行", "daily_account": "账号日上限", "hourly": "每小时上限", "cap3h": "3 小时上限", "key_queue": "每 Key 排队",
    "site_queue": "全站排队", "member_daily": "每人日上限", "base": "保底（不空闲）", "timeout": "排队超时",
    "other": "其他（未建模）", "upstream_error": "上游错误",
}
RETRY_WINDOW = 60.0
DEPLOY_EXCLUDE = 1800.0
_RES = re.compile(r"(\d{3,4})x(\d{3,4})/(\d+)step")
_CAP = re.compile(r"每小时 (\d+) 张")
_CODE = re.compile(r"^(\d{3}) ")


def classify(status: str, detail: str) -> str:
    if status == "ok":
        return ""
    if status == "error":
        return "upstream_error"
    for name, needles in REASON_PATTERNS:
        if any(n in detail for n in needles):
            return name
    return "other"


def is_v5(model: str) -> bool:
    m = (model or "").lower()
    return "diffusion-5" in m or "nai-v5" in m


@dataclass
class Dataset:
    tz: str
    ev: dict[str, np.ndarray]                       # 列式事件（按 arrival 排序）
    members: dict[int, dict]                         # 成员（不含测试 / 站长）
    keys: dict[int, dict]                            # 全部 Key
    counters: list[dict]
    settings: dict[str, str]
    actions: list[dict] = field(default_factory=list)          # admin_actions
    evidence: list[dict] = field(default_factory=list)         # share_evidence
    share_state: list[dict] = field(default_factory=list)
    req_features: int = 0
    path: str = ""
    notes: list[str] = field(default_factory=list)
    has_ver: bool = False

    @property
    def n(self) -> int:
        return len(self.ev["arrival"])

    @property
    def synthetic(self) -> bool:
        return str(self.settings.get("lab_synthetic", "")) == "1"

    @property
    def registered(self) -> int:
        """当前登记的成员数（启用、非测试、非站长）。"""
        return sum(1 for m in self.members.values() if m.get("enabled", 1))

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def day_of(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self.zone()).strftime("%Y-%m-%d")

    def day_start(self, day: str) -> float:
        return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=self.zone()).timestamp()

    def tz_offset(self) -> float:
        """回放用的固定 UTC 偏移（秒）。Asia/Shanghai 无夏令时。"""
        t = float(self.ev["arrival"][0]) if self.n else 0.0
        return datetime.fromtimestamp(t, self.zone()).utcoffset().total_seconds()

    def days(self) -> list[str]:
        return sorted(set(self.ev["day"].tolist()))

    def subset(self, mask: np.ndarray) -> "Dataset":
        return Dataset(self.tz, {k: v[mask] for k, v in self.ev.items()}, self.members, self.keys, self.counters,
                       self.settings, self.actions, self.evidence, self.share_state, self.req_features, self.path,
                       list(self.notes), self.has_ver)

    def coverage_hours(self, day: str) -> float:
        """这一天被数据覆盖的小时数（全数据第一条到最后一条，与当天求交）。"""
        if not self.n:
            return 0.0
        start = self.day_start(day)
        t = self.ev["arrival"]
        lo, hi = max(start, float(t.min())), min(start + 86400, float(t.max()))
        return max(0.0, (hi - lo) / 3600)

    def full_days(self, hours: float = 20.0) -> list[str]:
        return [d for d in self.days() if self.coverage_hours(d) >= hours]


def _cols(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def connect_ro(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load(path: str, tz: str = "Asia/Shanghai", since: Optional[float] = None,
         until: Optional[float] = None) -> Dataset:
    con = connect_ro(path)
    try:
        ds = _load(con, path, tz, since, until)
    finally:
        con.close()
    mark_retries(ds)
    return ds


def _rows(con, table: str, cols: list[str], extra: str = "") -> list[dict]:
    have = _cols(con, table)
    if not have:
        return []
    sel = [c for c in cols if c in have]
    return [dict(r) for r in con.execute(f"SELECT {', '.join(sel)} FROM {table} {extra}")]


def _load(con: sqlite3.Connection, path: str, tz: str, since: Optional[float], until: Optional[float]) -> Dataset:
    notes: list[str] = []
    zone = ZoneInfo(tz)
    keys: dict[int, dict] = {}
    for d in _rows(con, "api_keys", ["id", "name", "enabled", "daily_images", "daily_v5", "is_admin", "is_test",
                                     "quota_auto", "created_at"]):
        d.setdefault("is_admin", 0)
        d.setdefault("is_test", 0)
        keys[int(d["id"])] = d
    members = {k: v for k, v in keys.items() if not v["is_admin"] and not v["is_test"]}

    uc = _cols(con, "usage_log")
    want = ["ts", "key_id", "kind", "model", "status", "images", "wait_ms", "dur_ms", "src", "client", "detail",
            "up_status", "ver"]
    missing = [c for c in want if c not in uc]
    if missing:
        notes.append(f"usage_log 缺少列 {missing}（老版本），按 0 / 空处理")
    expr = [c if c in uc else ("0" if c in ("wait_ms", "dur_ms", "images", "up_status") else "''") + f" AS {c}"
            for c in want]
    where = ["kind LIKE 'image%'", "key_id IS NOT NULL"]
    args: list[Any] = []
    if since is not None:
        where.append("ts>=?")
        args.append(since)
    if until is not None:
        where.append("ts<?")
        args.append(until)
    rows = con.execute(f"SELECT {', '.join(expr)} FROM usage_log WHERE {' AND '.join(where)} ORDER BY ts", args).fetchall()

    cols = ("ts", "arrival", "key", "kind", "model", "status", "images", "wait_s", "dur_s", "src", "client", "up_status",
            "ver", "width", "height", "steps", "reason", "cap_in_msg", "reject_code", "v5", "admin", "test", "member",
            "day", "tod")
    ev: dict[str, list] = {k: [] for k in cols}
    for r in rows:
        ts = float(r["ts"])
        wait = max(0.0, float(r["wait_ms"] or 0) / 1000)
        dur = max(0.0, float(r["dur_ms"] or 0) / 1000)
        arrival = ts - wait - dur
        detail = r["detail"] or ""
        status = r["status"] or ""
        m = _RES.search(detail)
        cap = _CAP.search(detail) if status == "rejected" else None
        code = _CODE.match(detail) if status == "rejected" else None
        k = int(r["key_id"])
        info = keys.get(k, {})
        dt = datetime.fromtimestamp(arrival, zone)
        midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        for name, val in (("ts", ts), ("arrival", arrival), ("key", k), ("kind", r["kind"]), ("model", r["model"] or ""),
                          ("status", status), ("images", int(r["images"] or 0)), ("wait_s", wait), ("dur_s", dur),
                          ("src", r["src"] or ""), ("client", r["client"] or ""), ("up_status", int(r["up_status"] or 0)),
                          ("ver", r["ver"] or ""), ("width", int(m.group(1)) if m else 0),
                          ("height", int(m.group(2)) if m else 0), ("steps", int(m.group(3)) if m else 0),
                          ("reason", classify(status, detail)), ("cap_in_msg", int(cap.group(1)) if cap else 0),
                          ("reject_code", int(code.group(1)) if code else 0), ("v5", is_v5(r["model"] or "")),
                          ("admin", bool(info.get("is_admin", 0))), ("test", bool(info.get("is_test", 0))),
                          ("member", k in members), ("day", dt.strftime("%Y-%m-%d")),
                          ("tod", (dt - midnight).total_seconds())):
            ev[name].append(val)
    dtypes = {"ts": float, "arrival": float, "key": np.int64, "images": np.int64, "wait_s": float, "dur_s": float,
              "up_status": np.int64, "width": np.int64, "height": np.int64, "steps": np.int64, "cap_in_msg": np.int64,
              "reject_code": np.int64, "v5": bool, "admin": bool, "test": bool, "member": bool, "tod": float}
    arr = {k: np.array(v, dtype=dtypes.get(k, object)) for k, v in ev.items()}
    order = np.argsort(arr["arrival"], kind="mergesort")
    arr = {k: v[order] for k, v in arr.items()}
    if not rows:
        notes.append("没有出图记录")

    counters = [c for c in _rows(con, "counters", ["key_id", "day", "images", "legacy_free_images", "v5", "requests"])
                if int(c["key_id"]) in members]
    settings = {r["key"]: r["value"] for r in _rows(con, "site_settings", ["key", "value"])}
    actions = _rows(con, "admin_actions", ["ts", "actor", "action", "target", "detail"], "ORDER BY ts")
    evidence = _rows(con, "share_evidence", ["ts", "key_id", "kind", "points", "detail"], "ORDER BY ts")
    state = _rows(con, "share_state", ["key_id", "score", "score_ts", "strikes", "paused_until"])
    rf = con.execute("SELECT COUNT(*) FROM req_features").fetchone()[0] if _cols(con, "req_features") else 0
    arr["fp"] = _match_features(con, arr) if rf else np.array([""] * len(arr["arrival"]), dtype=object)
    return Dataset(tz=tz, ev=arr, members=members, keys=keys, counters=counters, settings=settings, actions=actions,
                   evidence=evidence, share_state=state, req_features=int(rf), path=path, notes=notes,
                   has_ver="ver" in uc and any(arr["ver"]))


def _match_features(con: sqlite3.Connection, ev: dict, tol: float = 5.0) -> np.ndarray:
    """给每条请求配上 req_features 的 sig|toks（同一把 Key、时间最近且相差 ≤ tol 秒）。"""
    from bisect import bisect_left
    by_key: dict[int, tuple[list, list]] = {}
    for ts, kid, sig, toks in con.execute("SELECT ts, key_id, sig, toks FROM req_features ORDER BY key_id, ts"):
        t, f = by_key.setdefault(int(kid), ([], []))
        t.append(float(ts))
        f.append(f"{sig}|{toks}")
    out = [""] * len(ev["arrival"])
    for i, (k, a) in enumerate(zip(ev["key"].tolist(), ev["arrival"].tolist())):
        if k not in by_key:
            continue
        t, f = by_key[k]
        j = bisect_left(t, a)
        best = min((c for c in (j - 1, j) if 0 <= c < len(t)), key=lambda c: abs(t[c] - a), default=None)
        if best is not None and abs(t[best] - a) <= tol:
            out[i] = f[best]
    return np.array(out, dtype=object)


# ---------------------------------------------------------------- 部署 / 规则版本
def changepoints(ds: Dataset) -> list[dict]:
    """部署（ver 变化）、推断的规则变更（拒绝文案里的上限数值 / HTTP 码变化）、参数变更（admin_actions）。"""
    e = ds.ev
    out: list[dict] = []
    if ds.has_ver:
        prev = None
        for t, v in zip(e["arrival"], e["ver"]):
            if v and prev is not None and v != prev:
                out.append({"ts": float(t), "kind": "deploy", "label": f"部署 {prev}→{v}"})
            if v:
                prev = v
    # 没有 ver（或 ver 之前）的旧数据：按文案推断
    legacy = np.array([not v for v in e["ver"]]) if ds.n else np.zeros(0, dtype=bool)
    rej = (e["status"] == "rejected") & legacy
    last_cap = None
    for t, c in zip(e["arrival"][rej & (e["cap_in_msg"] > 0)], e["cap_in_msg"][rej & (e["cap_in_msg"] > 0)]):
        if last_cap is not None and c != last_cap:
            out.append({"ts": float(t), "kind": "rule", "label": f"每小时上限 {last_cap}→{c}（由拒绝文案推断）"})
        last_cap = int(c)
    v5 = rej & (e["reason"] == "other") & (e["reject_code"] > 0)
    last_code = None
    for t, c in zip(e["arrival"][v5], e["reject_code"][v5]):
        if last_code is not None and c != last_code:
            out.append({"ts": float(t), "kind": "rule", "label": f"未建模拒绝的状态码 {last_code}→{c}（由文案推断）"})
        last_code = int(c)
    for a in ds.actions:
        act = str(a.get("action", ""))
        if any(w in act for w in ("调整", "设置", "参数", "上限", "额度")):
            out.append({"ts": float(a["ts"]), "kind": "param", "label": act[:30]})
    out.sort(key=lambda x: x["ts"])
    return out


def clean_mask(ds: Dataset, window: float = DEPLOY_EXCLUDE) -> np.ndarray:
    """剔除部署 / 规则变更前后 window 秒的请求。"""
    keep = np.ones(ds.n, dtype=bool)
    for cp in changepoints(ds):
        if cp["kind"] in ("deploy", "rule"):
            keep &= np.abs(ds.ev["arrival"] - cp["ts"]) > window
    return keep


def segments(ds: Dataset) -> list[dict]:
    """按部署 / 规则变更切成规则版本段；每段附上从拒绝文案推断出的参数（例如每小时上限）。"""
    cps = [c["ts"] for c in changepoints(ds) if c["kind"] in ("deploy", "rule")]
    edges = [-np.inf] + cps + [np.inf]
    out = []
    for i in range(len(edges) - 1):
        m = (ds.ev["arrival"] >= edges[i]) & (ds.ev["arrival"] < edges[i + 1])
        if not m.any():
            continue
        caps = ds.ev["cap_in_msg"][m & (ds.ev["cap_in_msg"] > 0)]
        vers = sorted({v for v in ds.ev["ver"][m] if v})
        inferred = {"account_hourly_cap": int(np.bincount(caps).argmax())} if len(caps) else {}
        out.append({"index": i, "start": float(ds.ev["arrival"][m].min()), "end": float(ds.ev["arrival"][m].max()),
                    "n": int(m.sum()), "versions": vers, "inferred": inferred, "mask": m})
    return out


# ---------------------------------------------------------------- 重试
def mark_retries(ds: Dataset, window: float = RETRY_WINDOW) -> None:
    """在 ds.ev 上加 retry（是否重试）、attempt（链内第几次，1 起）、chain（链编号 = 第一条的下标）、retry_delay。"""
    e = ds.ev
    n = ds.n
    retry = np.zeros(n, dtype=bool)
    attempt = np.ones(n, dtype=np.int64)
    chain = np.arange(n, dtype=np.int64)
    delay = np.full(n, np.nan)
    fp = e.get("fp", np.array([""] * n, dtype=object))
    pending: dict[int, list[int]] = {}           # 每把 Key 还没被「接上」的被拒请求

    def same(i: int, j: int) -> bool:
        if fp[i] and fp[j]:
            return fp[i] == fp[j]
        return e["model"][i] == e["model"][j]
    for i in range(n):
        k = int(e["key"][i])
        lst = [j for j in pending.get(k, []) if e["arrival"][i] - e["ts"][j] <= window]
        for pos in range(len(lst) - 1, -1, -1):        # 最近的、同一张图的那次被拒
            j = lst[pos]
            if same(i, j):
                retry[i] = True
                attempt[i] = attempt[j] + 1
                chain[i] = chain[j]
                delay[i] = max(0.0, e["arrival"][i] - e["ts"][j])
                del lst[pos]
                break
        if e["status"][i] == "rejected":
            lst.append(i)
        pending[k] = lst
    e["retry"], e["attempt"], e["chain"], e["retry_delay"] = retry, attempt, chain, delay
    if "fp" not in e:
        e["fp"] = fp


def retry_model(ds: Dataset, mask: Optional[np.ndarray] = None, max_attempt: int = 3) -> dict:
    """从数据估计重试模型：第 k 次被拒后重试的概率（k=1,2,≥3；Beta(1,1) 先验的后验）、重试间隔的经验分布。
    只用建模原因的拒绝（容量 / 排队 / 额度）。"""
    e = ds.ev
    m = np.ones(ds.n, dtype=bool) if mask is None else mask
    nxt_retry = np.zeros(ds.n, dtype=bool)
    idx_by_chain: dict[int, list[int]] = {}
    for i in np.nonzero(m)[0]:
        idx_by_chain.setdefault(int(e["chain"][i]), []).append(int(i))
    for idx in idx_by_chain.values():
        for a, b in zip(idx, idx[1:]):
            nxt_retry[a] = e["retry"][b]
    rej = m & (e["status"] == "rejected") & np.isin(e["reason"], MODELED)
    by_k = []
    for k in range(1, max_attempt + 1):
        sel = rej & ((e["attempt"] == k) if k < max_attempt else (e["attempt"] >= k))
        n_k, r_k = int(sel.sum()), int((sel & nxt_retry).sum())
        by_k.append({"attempt": k, "rejections": n_k, "retried": r_k, "alpha": r_k + 1, "beta": n_k - r_k + 1,
                     "p": (r_k + 1) / (n_k + 2)})
    delays = e["retry_delay"][m & e["retry"]]
    delays = delays[~np.isnan(delays)]
    if len(delays) == 0:
        delays = np.array([5.0, 10.0, 20.0])
    fp_cov = float(np.mean([bool(x) for x in e["fp"][m]])) if m.any() else 0.0
    return {"by_attempt": by_k, "delays": delays.astype(float), "window": RETRY_WINDOW, "feature_coverage": fp_cov,
            "n_rejections": int(rej.sum()), "share_retried": float((rej & nxt_retry).sum() / rej.sum()) if rej.any() else None}


# ---------------------------------------------------------------- 计数
def daily_counts(ds: Dataset) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in ds.counters:
        out[c["day"]] = out.get(c["day"], 0) + int(c.get("images") or 0)
    return out


def log_daily_images(ds: Dataset) -> dict[str, int]:
    m = (ds.ev["status"] == "ok") & ds.ev["member"]
    out: dict[str, int] = {}
    for t, n in zip(ds.ev["ts"][m], ds.ev["images"][m]):       # counters 按完成时刻记日
        d = ds.day_of(float(t))
        out[d] = out.get(d, 0) + int(n)
    return out


def previous_days(day: str, n: int) -> list[str]:
    d = datetime.strptime(day, "%Y-%m-%d")
    return [(d - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n, 0, -1)]
