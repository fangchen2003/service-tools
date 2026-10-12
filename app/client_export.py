"""客户端日志导出：站长定期下载，拿去独立复核有没有共用 / 转卖 Key 的「蛀虫」。

一个 zip，里面是几张 CSV（UTF-8 带 BOM，Excel 直接打开不乱码）和一份字段说明：
- requests.csv   每次请求一行，按时间排好；带上同一时刻的设备特征（只有哈希）
- keys.csv       每把 Key 一行：Discord 身份、用量、网段数 / 设备数、防分享分数、标签
- networks.csv   每把 Key 出现过的网段（只到前两段，如 54.255.*.*）
- share_evidence.csv  防分享模块记下的每条证据
不含提示词、图片和任何 Key / Token 原文。
"""
from __future__ import annotations

import bisect
import csv
import io
import time
import zipfile
from datetime import datetime

README = """猫头鹰公益站 · 客户端日志导出
导出时间：{now}（UTC+8）  范围：{scope}

requests.csv（每次请求一行，按时间升序）
  time 时间 · key_id / key_name / discord_id / discord_user 是谁
  kind 功能（image / image_stream / chat …）· model 模型 · status 结果（ok / rejected / error / cancelled）
  images 张数 · anlas 消耗 · wait_ms 排队 · dur_ms 生成耗时 · up_status 上游状态码
  net 来源网段（只到前两段）· user_agent 客户端自报的 UA
  device_fp 浏览器标识哈希（= UA 前 80 字的哈希，很粗：同版本浏览器的不同人也相同，不能单独当成「同一台设备」）
  device_os 系统 · client_sig 客户端请求特征哈希（同一个软件 + 同一套设置 → 相同；用同一份预设的不同人也可能相同）
  detail / reason 网关备注、拒绝原因 · rid 错误编号
  注：device_* 和 client_sig 取这把 Key 在该请求之前最近一次记下的特征（只记出图请求；10/10 01:58 起才有）。

keys.csv（每把 Key 一行）
  nets 出现过的网段数 · devices 出现过的设备指纹数 · oses 系统种类
  share_score / strikes / paused_until 防分享分数、违规次数、暂停到期
  tags 后台标签 · quota_auto 1=算法管、-1=手动 · is_test 测试 Key

怎么看「蛀虫」（思路，不是结论）：
  - 一把 Key 的设备数 / 网段数明显偏多，或同一时间段在两个网段交替出现
  - 不同 Key 共用同一个 device_fp + client_sig，且时间上互相接替、网段相同（一人多号；只看 device_fp 会大量误报）
  - 24 小时不停、节奏像机器（间隔几乎恒定）
  - client_sig 很多样（多种软件 / 多套设置用同一把 Key）
"""


def _ts(v) -> str:
    try:
        return datetime.fromtimestamp(float(v)).strftime("%Y-%m-%d %H:%M:%S") if v else ""
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def safe_cell(v):
    """防 CSV 公式注入：以 = + - @ 制表符 回车开头的文本前加 '（UA、Key 名都可能是成员自己填的）。"""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _csv(rows: list[list], header: list[str]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows([[safe_cell(c) for c in r] for r in rows])
    return ("﻿" + buf.getvalue()).encode("utf-8")


async def collect(db, days: int) -> dict:
    """读数据（只读）。days<=0 表示全部。"""
    since = time.time() - days * 86400 if days > 0 else 0
    q = db._db.execute_fetchall
    keys = await q("SELECT id, name, enabled, is_admin, is_test, quota_auto, created_at, last_used_at FROM api_keys")
    reg = {r[0]: r[1:] for r in await q(
        "SELECT key_id, discord_id, username, display_name FROM discord_registrations WHERE key_id IS NOT NULL")}
    usage = await q("SELECT ts, key_id, key_name, kind, model, status, images, anlas, wait_ms, dur_ms, up_status, "
                    "src, client, detail, reason, rid FROM usage_log WHERE ts>=? ORDER BY ts", (since,))
    feats = await q("SELECT ts, key_id, src, fp, os, sig FROM req_features WHERE ts>=? ORDER BY ts", (since - 3600,))
    sources = await q("SELECT key_id, label, first_seen, last_seen, hits FROM key_sources ORDER BY key_id, first_seen")
    share = {r[0]: r[1:] for r in await q("SELECT key_id, score, strikes, paused_until FROM share_state")}
    evidence = await q("SELECT ts, key_id, kind, points, detail FROM share_evidence WHERE ts>=? ORDER BY ts", (since,))
    tags: dict[int, list[str]] = {}
    for kid, tag, note in await q("SELECT key_id, tag, note FROM key_tags"):
        tags.setdefault(kid, []).append(tag + (f"（{note}）" if note else ""))
    return {"keys": keys, "reg": reg, "usage": usage, "feats": feats, "sources": sources,
            "share": share, "evidence": evidence, "tags": tags, "days": days}


def build_zip(d: dict) -> bytes:
    names = {k[0]: k[1] for k in d["keys"]}
    reg = d["reg"]
    # 每把 Key 的特征按时间排好，请求取「之前最近一次」
    by_key: dict[int, tuple[list[float], list[tuple]]] = {}
    for ts, kid, src, fp, os_, sig in d["feats"]:
        t, rows = by_key.setdefault(kid, ([], []))
        t.append(ts)
        rows.append((fp, os_, sig))
    req_rows, ok_images = [], {}
    for ts, kid, kname, kind, model, status, images, anlas, wait, dur, up, src, client, detail, reason, rid in d["usage"]:
        fp = os_ = sig = ""
        if kid in by_key:
            t, rows = by_key[kid]
            # usage_log.ts 是完成时刻，特征是到达时记下的：按到达时刻（完成 − 排队 − 生成）对齐，
            # 否则同一把 Key 并发时会配上后到那次请求的特征（10/11 审查）
            arrive = ts - ((wait or 0) + (dur or 0)) / 1000
            i = bisect.bisect_right(t, arrive + 1) - 1
            if i >= 0 and arrive - t[i] < 900:
                fp, os_, sig = rows[i]
        if status == "ok":
            ok_images[kid] = ok_images.get(kid, 0) + (images or 0)
        r = reg.get(kid, ("", "", ""))
        req_rows.append([_ts(ts), kid, names.get(kid, kname), r[0], r[1], kind, model, status, images, anlas,
                         wait, dur, up, src, client, fp, os_, sig, detail, reason, rid])
    nets: dict[int, set] = {}
    for kid, label, *_ in d["sources"]:
        nets.setdefault(kid, set()).add(label)
    devs: dict[int, set] = {}
    oses: dict[int, set] = {}
    for _, kid, _src, fp, os_, _sig in d["feats"]:
        devs.setdefault(kid, set()).add(fp)
        oses.setdefault(kid, set()).add(os_)
    key_rows = []
    for kid, name, enabled, is_admin, is_test, qa, created, last in d["keys"]:
        r = reg.get(kid, ("", "", ""))
        s = d["share"].get(kid, (0, 0, 0))
        key_rows.append([kid, name, r[0], r[1], r[2], enabled, is_admin, is_test, qa, _ts(created), _ts(last),
                         ok_images.get(kid, 0), len(nets.get(kid, ())), len(devs.get(kid, ())),
                         "/".join(sorted(o for o in oses.get(kid, ()) if o)),
                         round(s[0] or 0, 1), s[1] or 0, _ts(s[2]), "；".join(d["tags"].get(kid, []))])
    net_rows = [[kid, names.get(kid, ""), label, _ts(a), _ts(b), hits] for kid, label, a, b, hits in d["sources"]]
    ev_rows = [[_ts(ts), kid, names.get(kid, ""), kind, points, detail] for ts, kid, kind, points, detail in d["evidence"]]
    scope = f"最近 {d['days']} 天" if d["days"] > 0 else "全部"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", README.format(now=_ts(time.time()), scope=scope))
        z.writestr("requests.csv", _csv(req_rows, [
            "time", "key_id", "key_name", "discord_id", "discord_user", "kind", "model", "status", "images", "anlas",
            "wait_ms", "dur_ms", "up_status", "net", "user_agent", "device_fp", "device_os", "client_sig",
            "detail", "reason", "rid"]))
        z.writestr("keys.csv", _csv(key_rows, [
            "key_id", "key_name", "discord_id", "discord_user", "discord_display", "enabled", "is_admin", "is_test",
            "quota_auto", "created", "last_used", "ok_images", "nets", "devices", "oses",
            "share_score", "strikes", "paused_until", "tags"]))
        z.writestr("networks.csv", _csv(net_rows, ["key_id", "key_name", "net", "first_seen", "last_seen", "hits"]))
        z.writestr("share_evidence.csv", _csv(ev_rows, ["time", "key_id", "key_name", "kind", "points", "detail"]))
    return out.getvalue()
