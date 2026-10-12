"""每日服务器总结（统计专家意见 7）：report.png（2×2 日报）+ capacity.png（蒙特卡洛容量）+ summary.md（≤ 600 字）+ results.json。

日报四个面板，全部是描述 + 区间，不做「今天 vs 昨天」的显著性检验、不打星号：
  (1) 每小时成功张数 + 按原因堆叠的拒绝（只给计数）+ 每小时上限线 + 部署 / 规则 / 参数变更时刻；
  (2) 等待时间 ECDF：今天 vs 之前 7 天合并；P(等待 > 5 秒)、p90、几何均值，成员级 cluster bootstrap 95% 区间；
  (3) 成员-天：当天遇到容量拒绝的成员比例（Wilson，单位 = 成员）+ 前 3 名成员的成功张数占比与容量拒绝占比；
  (4) 防分享：各类证据次数、风险分越过提醒门槛的 Key 数、处罚次数（只描述）。
图注写覆盖小时、有效样本量（ICC 与设计效应）、混杂事件。
"""
from __future__ import annotations

import json
import math
import os
import re
from typing import Any, Optional

import numpy as np

from . import stats
from .data import CAPACITY, MODELED, REASON_LABELS, Dataset, changepoints, clean_mask, daily_counts, log_daily_images

SUMMARY_MAX = 600
WAIT_SLOW = 5.0
CJK_FONTS = ("Noto Sans CJK SC", "Noto Sans CJK JP", "Noto Serif CJK SC", "Source Han Sans SC", "Source Han Sans CN",
             "PingFang SC", "Hiragino Sans GB", "Heiti SC", "STHeiti", "Microsoft YaHei", "WenQuanYi Zen Hei",
             "Arial Unicode MS")
SHARE_LABELS = {"habits": "习惯交替", "overlap": "两地同时", "concurrent": "多地同时", "alternate": "网络切换",
                "multi_device": "多设备", "many_clients": "客户端多", "allday": "全天在用"}
SHARE_WARN = 30


# ---------------------------------------------------------------- 分析
def analyze_day(ds: Dataset, day: str, *, hourly_cap: Optional[int] = None, past_days: int = 7, seed: int = 0,
                full_day_hours: float = 20.0, B: int = 1000) -> dict[str, Any]:
    e = ds.ev
    off = ds.tz_offset() if ds.n else 28800.0
    keep = clean_mask(ds)
    past = [d for d in ds.days() if d < day][-past_days:]
    comparable = [d for d in past if ds.coverage_hours(d) >= full_day_hours]
    mt = (e["day"] == day) & ~e["test"]
    mp = np.isin(e["day"], comparable) & ~e["test"] & keep
    day0 = ds.day_start(day)
    cps = [c for c in changepoints(ds) if day0 <= c["ts"] < day0 + 86400]
    for c in cps:
        c["hour"] = ((c["ts"] + off) % 86400) / 3600

    # (1) 每小时
    def hour_of(t):
        return (((t + off) % 86400) // 3600).astype(int)
    okm = mt & (e["status"] == "ok")
    ok_h = np.bincount(hour_of(e["ts"][okm] - e["dur_s"][okm]), weights=np.maximum(1, e["images"][okm]), minlength=24)[:24]
    rej_h = {}
    for r in MODELED + ("other",):
        m = mt & (e["status"] == "rejected") & (e["reason"] == r)
        if m.any():
            rej_h[r] = np.bincount(hour_of(e["arrival"][m]), minlength=24)[:24].tolist()
    caps_h = [None] * 24
    for h in range(24):
        sel = mt & (e["cap_in_msg"] > 0) & (hour_of(e["arrival"]) == h)
        if sel.any():
            caps_h[h] = int(np.bincount(e["cap_in_msg"][sel]).argmax())
    retries_today = int((mt & e["retry"]).sum())

    # (2) 等待（成员请求，已出图 / 发往上游的）
    def waits(mask):
        m = mask & e["member"] & (e["status"] != "rejected") & (e["dur_s"] > 0)
        return e["wait_s"][m], e["key"][m]
    w_t, g_t = waits(mt & keep)
    w_p, g_p = waits(mp)

    def wstats(w, g, n_days, seed_):
        units = int(len(set(g.tolist())))
        ok_, why = stats.unit_sufficiency(units * max(1, n_days) if n_days else units, n_days)
        icc = stats.icc_oneway((w > WAIT_SLOW).astype(float), g)
        return {"n": int(len(w)), "members": units, "sufficient": ok_, "why": why, "icc": icc,
                "p_slow": stats.cluster_bootstrap(w, g, stats.p_over(WAIT_SLOW), B=B, seed=seed_),
                "p90": stats.cluster_bootstrap(w, g, stats.p90, B=B, seed=seed_ + 1),
                "gm": stats.cluster_bootstrap(w, g, stats.geo_mean_plus1, B=B, seed=seed_ + 2)}
    member_days_today = int(len(set(e["key"][mt & e["member"]].tolist())))
    wt = wstats(w_t, g_t, 1, seed)
    wt["sufficient"], wt["why"] = stats.unit_sufficiency(member_days_today, 1, min_days=1)
    pmd = len({(k, d) for k, d in zip(e["key"][mp & e["member"]].tolist(), e["day"][mp & e["member"]].tolist())})
    wp = wstats(w_p, g_p, len(comparable), seed + 10)
    wp["sufficient"], wp["why"] = stats.unit_sufficiency(pmd, len(comparable))

    # (3) 成员-天
    def member_view(mask):
        mem = mask & e["member"]
        ks = sorted(set(e["key"][mem].tolist()))
        hit = {int(k) for k in e["key"][mem & (e["status"] == "rejected") & np.isin(e["reason"], CAPACITY)]}
        imgs: dict[int, int] = {}
        for k, n in zip(e["key"][mem & (e["status"] == "ok")].tolist(), e["images"][mem & (e["status"] == "ok")].tolist()):
            imgs[k] = imgs.get(k, 0) + max(1, n)
        caps: dict[int, int] = {}
        for k in e["key"][mem & (e["status"] == "rejected") & np.isin(e["reason"], CAPACITY)].tolist():
            caps[k] = caps.get(k, 0) + 1
        top = lambda d: (sum(sorted(d.values(), reverse=True)[:3]) / sum(d.values())) if d and sum(d.values()) else None
        return {"active": len(ks), "hit": len(hit), "wilson": list(stats.wilson(len(hit), len(ks))),
                "top3_images": top(imgs), "top3_cap_rejects": top(caps), "images": int(sum(imgs.values())),
                "cap_rejects": int(sum(caps.values()))}
    mv_t = member_view(mt)
    mv_p = [member_view((e["day"] == d) & ~e["test"]) for d in comparable]

    # (4) 防分享
    t0, t1 = day0, day0 + 86400
    evd = [x for x in ds.evidence if t0 <= float(x["ts"]) < t1]
    kinds: dict[str, int] = {}
    pts: dict[int, float] = {}
    actions: dict[str, int] = {}
    for x in evd:
        if x["kind"] == "action":
            lab = next((w for w in ("提醒", "暂停", "重置", "停用", "清零") if w in str(x.get("detail", ""))), "其他")
            actions[lab] = actions.get(lab, 0) + 1
        else:
            kinds[x["kind"]] = kinds.get(x["kind"], 0) + 1
            pts[int(x["key_id"])] = pts.get(int(x["key_id"]), 0.0) + float(x.get("points") or 0)
    share = {"kinds": kinds, "keys_over_warn": sum(1 for v in pts.values() if v >= SHARE_WARN), "actions": actions,
             "mode": ds.settings.get("share_guard_mode", "enforce"), "warn": SHARE_WARN, "req_features": ds.req_features}

    cnt = daily_counts(ds).get(day)
    logged = log_daily_images(ds).get(day, 0)
    rej_total = int((mt & (e["status"] == "rejected") & np.isin(e["reason"], MODELED)).sum())
    return {
        "day": day, "coverage_hours": round(ds.coverage_hours(day), 1), "past_days": past, "comparable_days": comparable,
        "requests": int(mt.sum()), "retries": retries_today, "new_requests": int((mt & ~e["retry"]).sum()),
        "ok_images": int(ok_h.sum()), "rejected": rej_total,
        "hourly": {"ok": ok_h.tolist(), "rejects": rej_h, "caps": caps_h, "cap_setting": hourly_cap,
                   "peak": int(ok_h.max()) if len(ok_h) else 0},
        "changepoints": cps, "excluded_requests_today": int((mt & ~keep).sum()),
        "wait": {"today": wt, "past": wp, "threshold": WAIT_SLOW,
                 "today_values": w_t.tolist(), "past_values": w_p.tolist()},
        "members": {"today": mv_t, "past": mv_p, "member_days_today": member_days_today},
        "share": share, "synthetic": ds.synthetic,
        "crosscheck": {"counters_images": cnt, "log_images": logged,
                       "ok": None if cnt is None else abs(cnt - logged) <= 2,
                       "note": "成员成功张数：usage_log 汇总 ↔ counters 表（两个独立写入路径）"},
    }


# ---------------------------------------------------------------- 绘图工具
def _font() -> Optional[str]:
    from matplotlib import font_manager
    names = {f.name for f in font_manager.fontManager.ttflist}
    return next((c for c in CJK_FONTS if c in names), None)


_TOKEN = re.compile(r"[A-Za-z0-9_.'%\-–]+|\s|.", re.S)
_NO_START = set("，。、；：）」』】》,.;:)%")


def _wrap(text: str, width: int) -> str:
    """按显示宽度折行（中文算 2）：英文单词 / 数字不拆开，标点不放在行首。"""
    lines, cur, w = [], "", 0
    for tok in _TOKEN.findall(text):
        if tok == "\n":
            lines.append(cur)
            cur, w = "", 0
            continue
        tw = sum(2 if ord(ch) > 0x2E80 else 1 for ch in tok)
        if w + tw > width and cur and tok not in _NO_START:
            lines.append(cur.rstrip())
            cur, w = ("", 0) if tok.isspace() else (tok, tw)
            continue
        cur += tok
        w += tw
    lines.append(cur)
    return "\n".join(lines)


def _style():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    font = _font()
    plt.rcParams.update({
        "font.family": "sans-serif", "font.sans-serif": ([font] if font else []) + ["DejaVu Sans"],
        "axes.unicode_minus": False, "font.size": 7.5, "axes.titlesize": 8, "axes.labelsize": 7.5,
        "xtick.labelsize": 7, "ytick.labelsize": 7, "legend.fontsize": 6.5, "axes.linewidth": 0.6,
        "xtick.major.width": 0.6, "ytick.major.width": 0.6, "axes.spines.top": False, "axes.spines.right": False,
        "legend.frameon": False, "savefig.dpi": 220, "hatch.linewidth": 0.5,
    })
    return plt, font


INK, MID, LIGHT, ACC = "#111111", "#666666", "#bdbdbd", "#1f4e79"
GRAYS = ["#4d4d4d", "#7a7a7a", "#a6a6a6", "#cfcfcf", "#e6e6e6"]
HATCH = ["", "////", "....", "xxxx", "\\\\\\\\", "----", "++", "oo", ""]


def _ci(b: dict, fmt: str) -> str:
    if b.get("est") is None:
        return "—"
    if b.get("lo") is None or (isinstance(b.get("lo"), float) and math.isnan(b["lo"])):
        return format(b["est"], fmt)
    return f"{format(b['est'], fmt)} [{format(b['lo'], fmt)}, {format(b['hi'], fmt)}]"


def make_figure(an: dict, path: str) -> dict:
    plt, font = _style()
    fig = plt.figure(figsize=(7.4, 7.3))
    gs = fig.add_gridspec(2, 2, left=0.1, right=0.975, top=0.93, bottom=0.30, hspace=0.45, wspace=0.3)
    ax = [fig.add_subplot(gs[i, j]) for i in range(2) for j in range(2)]
    title = f"猫头鹰公益站 · 服务器日报 {an['day']}" + ("（合成数据，仅自洽检验）" if an["synthetic"] else "")
    fig.suptitle(title, fontsize=10, y=0.985)
    cap: list[str] = []

    # (a) 每小时：成功 + 拒绝堆叠
    a = ax[0]
    h = np.arange(24)
    okh = np.array(an["hourly"]["ok"], dtype=float)
    a.bar(h, okh, width=0.78, color=INK, label="成功张数")
    bottom = okh.copy()
    for i, (r, vals) in enumerate(an["hourly"]["rejects"].items()):
        v = np.array(vals, dtype=float)
        a.bar(h, v, width=0.78, bottom=bottom, color=GRAYS[min(i + 1, len(GRAYS) - 1)], edgecolor=MID, linewidth=0.3,
              hatch=HATCH[i % len(HATCH)], label=f"拒绝：{REASON_LABELS.get(r, r)}")
        bottom += v
    caps = an["hourly"]["caps"]
    cap_line = [c if c is not None else an["hourly"]["cap_setting"] for c in caps]
    if any(c for c in cap_line):
        a.step(np.arange(25) - 0.5, cap_line + [cap_line[-1]], where="post", color=ACC, lw=0.8, ls=":",
               label="每小时上限（文案 / 设置）")
    cps_all = an["changepoints"]
    if len(cps_all) <= 6:
        for c in cps_all:
            a.axvline(c["hour"] - 0.5, color=ACC if c["kind"] != "param" else MID, lw=0.7, ls="--" if c["kind"] != "param" else "-.")
            a.text(c["hour"] - 0.4, a.get_ylim()[1] * 0.98, {"deploy": "部署", "rule": "规则", "param": "参数"}[c["kind"]],
                   fontsize=5.5, va="top", color=ACC)
    else:
        # 变更太密（10/10 一天约 60 次部署）：逐条画线会糊满整图，改成按小时浅色底纹标出「有变更的小时」
        for h in sorted({int(c["hour"]) for c in cps_all}):
            a.axvspan(h - 0.5, h + 0.5, color=ACC, alpha=0.07, lw=0)
    top = max(float(bottom.max()) if len(bottom) else 0, max([c or 0 for c in cap_line] + [0]))
    a.set_ylim(0, max(10, top) * 1.5)
    a.set_xlim(-0.6, 23.6)
    a.set_xticks(range(0, 24, 3))
    a.set_xlabel("小时（北京时间）")
    a.set_ylabel("请求 / 张数")
    a.set_title("(a) 每小时成功与拒绝（计数）", loc="left")
    a.legend(loc="upper left", ncol=2)
    if len(an["changepoints"]) <= 6:
        cp_txt = "；".join(f"{c['hour']:.1f} 时 {c['label']}" for c in an["changepoints"]) or "无"
    else:
        kinds = {"deploy": "部署", "rule": "规则变更", "param": "参数调整"}
        cnt = {}
        for c in an["changepoints"]:
            cnt[kinds[c["kind"]]] = cnt.get(kinds[c["kind"]], 0) + 1
        cp_txt = "、".join(f"{k} {v} 次" for k, v in cnt.items()) + "（浅色底纹 = 有变更的小时；变更过密，当天不适合做对照）"
    cap.append(f"(a) 今天成功 {an['ok_images']} 张、建模原因拒绝 {an['rejected']} 次（其中客户端自动重试请求 {an['retries']} 个，"
               f"新请求 {an['new_requests']} 个）；只给计数，不做检验。当天变更：{cp_txt}"
               + (f"（前后 30 分钟的 {an['excluded_requests_today']} 个请求不计入 (b)–(c) 的区间估计）" if an["excluded_requests_today"] else "") + "。")

    # (b) 等待 ECDF
    b = ax[1]
    w = an["wait"]
    for vals, lab, ls, col in ((w["today_values"], f"今天（{w['today']['members']} 人，{w['today']['n']} 张）", "-", INK),
                               (w["past_values"], f"之前 {len(an['comparable_days'])} 天（{w['past']['n']} 张）", "--", MID)):
        if vals:
            x, y = stats.ecdf(np.maximum(vals, 0.01))
            b.step(x, y, where="post", color=col, lw=1.0, ls=ls, label=lab)
    b.axvline(WAIT_SLOW, color=ACC, lw=0.7, ls=":")
    b.set_xscale("log")
    b.set_xlim(0.01, max(300, max(w["today_values"] + w["past_values"] + [1]) * 1.2))
    b.set_ylim(0, 1.02)
    b.set_xlabel("等待（秒，对数轴；0 记为 0.01）")
    b.set_ylabel("累积比例")
    b.set_title("(b) 等待时间分布（ECDF）", loc="left")
    b.legend(loc="lower right")
    lines = []
    for lab, s in (("今天", w["today"]), ("之前", w["past"])):
        if s["n"] == 0:
            lines.append(f"{lab}：无数据")
            continue
        flag = "" if s["sufficient"] else "（样本不足）"
        lines.append(f"{lab}{flag}：P(>5s) {_ci(s['p_slow'], '.2f')}\n    p90 {_ci(s['p90'], '.1f')} s；GM {_ci(s['gm'], '.2f')} s")
    b.text(0.03, 0.64, "\n".join(lines), transform=b.transAxes, va="top", ha="left", fontsize=6.2, linespacing=1.4)
    it, ip = w["today"]["icc"], w["past"]["icc"]
    neff = lambda i: "—" if not i or i.get("n_eff") is None else f"{i['n_eff']:.0f}（ICC {i['icc']:.2f}，设计效应 {i['deff']:.1f}）"
    cap.append(f"(b) 只含成员已发往上游的请求。P(等待 > 5 秒)、p90、几何均值 GM(等待+1)−1，括号为按成员整体重抽样的 cluster bootstrap "
               f"95% 区间（B = {w['today']['p_slow']['B']}）。有效样本量：今天 {neff(it)}，之前 {neff(ip)}。"
               f"等待中位数接近 0 秒，不作比较；今天与之前不做显著性检验。"
               + ("" if w["today"]["sufficient"] else f"今天{stats.INSUFFICIENT}（{w['today']['why']}）。")
               + ("" if w["past"]["sufficient"] else f"对照{stats.INSUFFICIENT}（{w['past']['why']}）。"))

    # (c) 成员-天
    c = ax[2]
    mv = an["members"]["today"]
    rows = [("遇到容量拒绝\n的成员比例", mv["wilson"][0], mv["wilson"][1], mv["wilson"][2]),
            ("前 3 名占\n成功张数", mv["top3_images"], None, None),
            ("前 3 名占\n容量拒绝", mv["top3_cap_rejects"], None, None)]
    for i, (lab, v, lo, hi) in enumerate(rows):
        if v is None:
            c.text(0.01, i, "—", va="center", fontsize=6.5)
            continue
        c.barh(i, v * 100, height=0.55, color=LIGHT, edgecolor=MID, linewidth=0.4)
        if lo is not None:
            c.errorbar(v * 100, i, xerr=[[(v - lo) * 100], [(hi - v) * 100]], fmt="none", ecolor=INK, elinewidth=0.8, capsize=2)
        c.text(min(v * 100, 92) + 2, i - 0.32, f"{v:.0%}", fontsize=6.5, va="center")
    past_vals = [[p["wilson"][0] for p in an["members"]["past"] if p["wilson"][0] is not None],
                 [p["top3_images"] for p in an["members"]["past"] if p["top3_images"] is not None],
                 [p["top3_cap_rejects"] for p in an["members"]["past"] if p["top3_cap_rejects"] is not None]]
    for i, vals in enumerate(past_vals):
        if vals:
            c.plot(np.array(vals) * 100, [i + 0.38] * len(vals), "|", color=MID, ms=6, mew=0.8,
                   label="之前各天" if i == 0 else None)
    c.set_yticks(range(3))
    c.set_yticklabels([r[0] for r in rows])
    c.invert_yaxis()
    c.set_xlim(0, 105)
    c.set_xlabel("%（单位 = 成员-天）")
    c.set_title("(c) 成员视角", loc="left")
    if any(past_vals):
        c.legend(loc="upper right")
    cap.append(f"(c) 今天活跃成员 {mv['active']} 人，其中 {mv['hit']} 人至少遇到一次容量拒绝（账号上限、全站排队、保底不空闲、排队超时；"
               f"不含每人上限与每 Key 排队），误差棒为 Wilson 95% 区间（单位 = 成员）；前 3 名占比只描述集中度。竖线为之前各天的值。"
               + ("" if mv["active"] >= stats.MIN_MEMBER_DAYS else f"成员-天 {mv['active']} < {stats.MIN_MEMBER_DAYS}，{stats.INSUFFICIENT}。"))

    # (d) 防分享
    d = ax[3]
    sh = an["share"]
    names = list(SHARE_LABELS)
    vals = [sh["kinds"].get(k, 0) for k in names]
    d.bar(range(len(names)), vals, color=LIGHT, edgecolor=MID, linewidth=0.4)
    for i, v in enumerate(vals):
        if v:
            d.text(i, v, str(v), ha="center", va="bottom", fontsize=6)
    d.set_xticks(range(len(names)))
    d.set_xticklabels([SHARE_LABELS[k] for k in names], rotation=35, ha="right")
    d.set_ylabel("证据次数")
    d.set_ylim(0, max(4, max(vals + [0]) * 1.5))
    d.set_title("(d) 防分享（只描述）", loc="left")
    act = "、".join(f"{k} {v}" for k, v in sh["actions"].items()) or "无"
    d.text(0.98, 0.97, f"模式：{sh['mode']}\n当天证据分 ≥ {sh['warn']} 的 Key：{sh['keys_over_warn']}\n处罚：{act}",
           transform=d.transAxes, ha="right", va="top", fontsize=6.3)
    cap.append(f"(d) 当天写入的防分享证据按类别计数（含不计分的辅助证据）；「证据分 ≥ {sh['warn']}」按当天证据分简单相加，"
               f"不含半衰期衰减；处罚按记录计数。模式为 {sh['mode']}（observe 只记录不处罚）。只描述，不推断。")

    head = (f"图 1  数据覆盖 {an['coverage_hours']} 小时"
            + ("（不足 20 小时，不是完整的一天）" if an["coverage_hours"] < 20 else "")
            + f"；对照 = 之前 {len(an['comparable_days'])} 个完整天（不含部署前后 30 分钟）。")
    if an["synthetic"]:
        head += "本图使用合成数据，只用于检验管线是否自洽，不代表真实情况。"
    cap_text = head + " " + " ".join(cap)
    fig.text(0.04, 0.225, _wrap(cap_text, 126), ha="left", va="top", fontsize=6.2, linespacing=1.42)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fig.savefig(path)
    plt.close(fig)
    return {"path": path, "font": font, "caption": cap_text}


def make_capacity_figure(mc: dict, path: str, *, synthetic: bool = False) -> dict:
    plt, font = _style()
    fig = plt.figure(figsize=(7.4, 3.7))
    gs = fig.add_gridspec(1, 2, left=0.08, right=0.98, top=0.86, bottom=0.38, wspace=0.28)
    sc = mc["scales"]
    x = np.array([s["members"] for s in sc])
    thr = mc["thresholds"]
    f = mc["failure"]["overall"]
    th = mc.get("theory") or {}
    panels = [("member_reject_rate", 100, "按人计新请求失败率（%）", "share_members_rej3"),
              ("first_to_image_p90", 1, "首次请求到出图 p90（秒）", None)]
    for j, (k, mul, ylab, k2) in enumerate(panels):
        a = fig.add_subplot(gs[0, j])
        med = np.array([s[k]["median"] for s in sc], dtype=float) * mul
        lo = np.array([s[k]["lo"] for s in sc], dtype=float) * mul
        hi = np.array([s[k]["hi"] for s in sc], dtype=float) * mul
        a.plot(x, med, color=INK, lw=1.0, marker="o", ms=2.5, label="中位数")
        a.fill_between(x, lo, hi, color=MID, alpha=0.2, lw=0, label="典型日 95% 区间")
        a.axhline(thr[k][0] * mul, color=ACC, lw=0.7, ls=":", label="失控阈值")
        if k2:
            m2 = np.array([s[k2]["hi"] for s in sc], dtype=float) * 100
            a.plot(x, m2, color=MID, lw=0.8, ls="--", label="被拒 ≥3 次成员比例（区间上界）")
            a.axhline(thr[k2][0] * 100, color=MID, lw=0.5, ls=":")
        if f["scale"] is not None:
            a.axvline(f["scale"] * mc["registered"], color=INK, lw=0.8)
        if th.get("members"):
            a.axvline(th["members"], color=ACC, lw=0.8, ls="-.", label=f"理论容量 {th['members']:.0f} 人")
        a.axvspan(55, 70, color=ACC, alpha=0.07, lw=0, label="专家估计 55–70 人")
        a.axvline(mc["registered"], color=MID, lw=0.5, ls="--")
        a.set_xlabel(f"登记成员数（当前 {mc['registered']} 人 = 1×）")
        a.set_ylabel(ylab)
        a.set_ylim(bottom=0)
        a.set_title(f"({'ab'[j]}) {ylab.split('（')[0]}", loc="left")
        if j == 0:
            a.legend(loc="upper left", fontsize=5.8)
    fig.suptitle("容量压力测试（蒙特卡洛）" + ("（合成数据，仅自洽检验）" if synthetic else ""), fontsize=9.5, y=0.975)
    rs = sorted(set(mc["R_by_scale"].values()))
    rtxt = f"R = {rs[0]}" if len(rs) == 1 else f"R = {rs[0]}–{rs[-1]}"
    fe, fs = mc["failure"]["experience"], mc["failure"]["safety"]
    gs_ = sorted(set((mc.get("G_by_scale") or {}).values()))
    txt = (f"图 2  两阶段 block bootstrap：先从 {len(mc['days'])} 个{'完整' if not mc['partial_data'] else '（不完整）'}天中抽一天，"
           f"再在当天的成员里抽（先不放回，超出当天人数才复制）；嵌套设计：{gs_[0] if gs_ else '—'} 组 × 10 天，每组重抽天并从 Beta 后验"
           f"抽活跃比例与重试概率，每个规模共 {rtxt} 天。线 = 典型日中位数，带 = 组中位数的 2.5–97.5% 分位（典型日 95% 区间），"
           f"失控按区间不利一侧判定。成员体验：{_fail_txt(fe)}；账号安全：{_fail_txt(fs)}"
           f"（上游 429 / 5xx 尖峰无法模拟）。理论容量 {th.get('members', 0) or 0:.0f} 人"
           f"（按小时 {th.get('by_hour', 0) or 0:.0f}、按日 {th.get('by_day', 0) or 0:.0f}）；专家估计 55–70 人。"
           + ("".join("【注意】" + s + "。" for s in mc["suspicious"])))
    fig.text(0.04, 0.245, _wrap(txt, 126), ha="left", va="top", fontsize=6.2, linespacing=1.42)
    fig.savefig(path)
    plt.close(fig)
    return {"path": path, "caption": txt}


def _fail_txt(f: dict) -> str:
    if f["scale"] is None:
        return "范围内未失控"
    pt = f"，中位数越线 ≈ {f['point']:.2f}×" if f.get("point") else ""
    hi = f"{f['interval'][1]}×" if f["interval"][1] is not None else "范围外"
    return f"{f['scale']}×（约 {f['members']} 人）起可能失控（{'、'.join(f['binding_labels'])}），区间 [{f['scale']}×, {hi}]{pt}"


# ---------------------------------------------------------------- 文字摘要
def make_summary(an: dict, mc: Optional[dict], calib: Optional[dict], sweep: Optional[dict] = None,
                 backtest: Optional[dict] = None) -> str:
    """只写描述和区间；不写「显著」、不做今天 vs 昨天的检验。长度 ≤ 600 字。"""
    L = [f"**猫头鹰公益站 · {an['day']} 服务器日报**（覆盖 {an['coverage_hours']} 小时）"]
    if an["synthetic"]:
        L.append("· ⚠ 合成数据，仅检验管线自洽，不代表真实情况。")
    mv = an["members"]["today"]
    L.append(f"· 成功 {an['ok_images']} 张；新请求 {an['new_requests']} 个，自动重试 {an['retries']} 个；活跃成员 {mv['active']} 人。")
    if mv["wilson"][0] is not None:
        L.append(f"· {mv['hit']}/{mv['active']} 位成员遇到过容量拒绝（{mv['wilson'][0]:.0%}，95% CI {mv['wilson'][1]:.0%}–{mv['wilson'][2]:.0%}）。")
    wt = an["wait"]["today"]
    if wt["n"]:
        L.append(f"· 等待 > 5 秒占 {_ci(wt['p_slow'], '.0%')}，p90 {_ci(wt['p90'], '.0f')} 秒（按成员 bootstrap）"
                 + ("" if wt["sufficient"] else "，样本不足，不下结论") + "。")
    if an["changepoints"]:
        n_cp = len(an["changepoints"])
        L.append(("· 当天有变更：" + "；".join(c["label"] for c in an["changepoints"][:3]) if n_cp <= 3 else
                  f"· 当天变更 {n_cp} 次（部署 / 规则 / 参数）") + "，前后数据不可直接比较。")
    if mc and mc.get("scales"):
        f = mc["failure"]["overall"]
        if f["scale"] is not None:
            L.append(f"· 容量（蒙特卡洛）：约 {f['members']} 人起可能失控（{'、'.join(f['binding_labels'][:2])}），"
                     f"理论估计 {((mc.get('theory') or {}).get('members') or 0):.0f} 人。")
        else:
            L.append(f"· 容量：放大到 {mc['scales'][-1]['members']} 人仍未失控。")
        for s in mc.get("suspicious", [])[:2]:
            L.append("· ⚠ " + s + "。")
        if mc.get("partial_data"):
            L.append("· 还没有完整的一天数据，容量结论仅供参考。")
    if backtest:
        if backtest.get("status") != "ok":
            L.append(f"· 回测：{backtest.get('note')}。")
        else:
            sc = backtest["oos"]["score"]
            L.append(f"· 回测样本外覆盖率 {sc['coverage']:.0%}（名义 95%，n={sc['n']}）。")
    if calib and calib.get("abs_err_reject_rate") is not None:
        L.append(f"· 回放校准（样本内）：拒绝率误差 {calib['abs_err_reject_rate']:.1%}，成功数误差 {(calib['rel_err_ok'] or 0):+.1%}。")
    cc = an["crosscheck"]
    if cc["ok"] is False:
        L.append(f"· 交叉校验不一致：日志 {cc['log_images']} 张 vs 计数 {cc['counters_images']} 张。")
    text = ""
    for line in L:
        if len(text) + len(line) + 1 > SUMMARY_MAX:
            break
        text += line + "\n"
    return text.rstrip() + "\n"


# ---------------------------------------------------------------- 输出
def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items() if not isinstance(v, np.ndarray) or v.ndim <= 1}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return _clean(o.tolist())
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    if isinstance(o, np.floating):
        f = float(o)
        return None if math.isnan(f) or math.isinf(f) else f
    return o


def write_all(out_dir: str, an: dict, mc: Optional[dict], calib: Optional[dict], sweep: Optional[dict] = None,
              meta: Optional[dict] = None, backtest: Optional[dict] = None) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    png = os.path.join(out_dir, "report.png")
    fig = make_figure(an, png)
    cap_fig = make_capacity_figure(mc, os.path.join(out_dir, "capacity.png"), synthetic=an["synthetic"]) \
        if mc and mc.get("scales") else None
    summary = make_summary(an, mc, calib, sweep, backtest)
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write(summary)
    slim = dict(an)
    slim["wait"] = {k: v for k, v in an["wait"].items() if k not in ("today_values", "past_values")}
    results = {"meta": meta or {}, "analysis": slim, "montecarlo": mc, "calibration": calib, "sweep": sweep,
               "backtest": backtest, "figure": {"font": fig["font"], "caption": fig["caption"],
                                                "capacity_caption": cap_fig["caption"] if cap_fig else None},
               "summary": summary}
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
        json.dump(_clean(results), f, ensure_ascii=False, indent=1, default=str)
    return {"png": png, "capacity_png": cap_fig["path"] if cap_fig else None, "summary": summary, "dir": out_dir,
            "font": fig["font"]}
