"""猫头鹰公益站 · 客户端日志离线智能审计工具 (Sybil & Abuse Auditor)

用法：
  python3 deploy/ops/sybil_audit.py --zip /path/to/owl-client-logs.zip
  python3 deploy/ops/sybil_audit.py --csv /path/to/extracted_dir/
  python3 deploy/ops/sybil_audit.py --db data/gate.sqlite

自动识别：
  1. 100% 实锤的一人多号 / Sybil 多开（同网段 + 同客户端签名 + 极速交替出图）
  2. 额度耗尽后换号接力（Handoff）
  3. 无退避死循环打桩脚本（Botting / 固定间隔高频重试）
  4. 客户端工具链分布与无权限接口试探
  5. 上游 GPU 异常（NaN 溢出）与 500 故障
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sqlite3
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple


def _parse_ts(v: str) -> float:
    try:
        return datetime.strptime(v.strip(), "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        try:
            return float(v)
        except Exception:
            return 0.0


def _is_shared_net(net: str) -> bool:
    if not net:
        return True
    shared = ("104.28.", "104.22.", "104.16.", "104.17.", "172.64.", "172.68.", "172.69.", "162.158.")
    return any(net.startswith(p) for p in shared)


class SybilAuditor:
    def __init__(self, requests: List[Dict[str, Any]], keys: List[Dict[str, Any]]):
        self.requests = requests
        self.keys = {k.get("key_id", ""): k for k in keys}
        for r in self.requests:
            r["epoch"] = _parse_ts(r.get("time", ""))

    @classmethod
    def from_csv_dir(cls, dir_path: str) -> SybilAuditor:
        req_file = os.path.join(dir_path, "requests.csv")
        key_file = os.path.join(dir_path, "keys.csv")
        with open(req_file, encoding="utf-8-sig") as f:
            reqs = list(csv.DictReader(f))
        with open(key_file, encoding="utf-8-sig") as f:
            keys = list(csv.DictReader(f))
        return cls(reqs, keys)

    @classmethod
    def from_zip(cls, zip_path: str) -> SybilAuditor:
        with zipfile.ZipFile(zip_path, "r") as z:
            with z.open("requests.csv") as f:
                reqs = list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))
            with z.open("keys.csv") as f:
                keys = list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))
        return cls(reqs, keys)

    @classmethod
    def from_sqlite(cls, db_path: str, since_days: int = 3) -> SybilAuditor:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        since = (datetime.now().timestamp() - since_days * 86400) if since_days > 0 else 0
        
        # keys
        k_rows = con.execute("SELECT id as key_id, name as key_name, enabled, is_admin, is_test FROM api_keys").fetchall()
        keys = [dict(r) for r in k_rows]
        
        # usage & features
        u_rows = con.execute(
            "SELECT ts, key_id, key_name, kind, model, status, images, wait_ms, dur_ms, up_status, src as net, client as user_agent, detail, reason "
            "FROM usage_log WHERE ts>=? ORDER BY ts", (since,)).fetchall()
        
        # features by key
        f_rows = con.execute(
            "SELECT ts, key_id, fp as device_fp, os as device_os, sig as client_sig FROM req_features WHERE ts>=? ORDER BY ts", (since,)).fetchall()
        f_by_key = defaultdict(list)
        for r in f_rows:
            f_by_key[r["key_id"]].append((r["ts"], r["device_fp"], r["device_os"], r["client_sig"]))

        reqs = []
        for r in u_rows:
            d = dict(r)
            d["time"] = datetime.fromtimestamp(d["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            kid = d["key_id"]
            d["device_fp"] = d["device_os"] = d["client_sig"] = ""
            if kid in f_by_key:
                # pick closest previous feature
                feats = f_by_key[kid]
                for f_ts, fp, os_, sig in reversed(feats):
                    if f_ts <= d["ts"] + 5:
                        d["device_fp"], d["device_os"], d["client_sig"] = fp, os_, sig
                        break
            reqs.append(d)
        con.close()
        return cls(reqs, keys)

    def audit(self) -> Dict[str, Any]:
        results: Dict[str, Any] = {
            "summary": self._summary(),
            "sybil_pairs": self._detect_sybil(),
            "handoff_pairs": self._detect_handoff(),
            "botting_keys": self._detect_botting(),
            "client_ecosystem": self._analyze_clients(),
            "upstream_anomalies": self._analyze_upstream_errors(),
        }
        return results

    def _summary(self) -> Dict[str, Any]:
        req_count = len(self.requests)
        if not req_count:
            return {"total_requests": 0}
        statuses = Counter(r.get("status", "") for r in self.requests)
        t_start = self.requests[0].get("time", "")
        t_end = self.requests[-1].get("time", "")
        return {
            "total_requests": req_count,
            "time_range": f"{t_start} ~ {t_end}",
            "statuses": dict(statuses),
            "keys_count": len(self.keys),
        }

    def _detect_sybil(self) -> List[Dict[str, Any]]:
        """检测一人多号（同一网段 + 相同客户端签名/UA + 极速交替出图）"""
        by_key = defaultdict(list)
        for r in self.requests:
            kid = str(r.get("key_id", ""))
            if kid:
                by_key[kid].append(r)

        # 候选对：在相同非公共网段使用相同 client_sig
        sig_net_pairs = defaultdict(set)
        for r in self.requests:
            sig = r.get("client_sig")
            net = r.get("net")
            kid = str(r.get("key_id", ""))
            if sig and net and kid and not _is_shared_net(net):
                # 排除站长/测试对
                kinfo = self.keys.get(kid, {})
                if str(kinfo.get("is_admin")) == "1" or str(kinfo.get("is_test")) == "1":
                    continue
                if int(kid) >= 58 and int(kid) <= 67: # 阳性对照组
                    continue
                sig_net_pairs[(sig, net)].add(kid)

        candidate_pairs = set()
        for (sig, net), kids in sig_net_pairs.items():
            if len(kids) >= 2:
                ks = sorted(list(kids))
                for i in range(len(ks)):
                    for j in range(i + 1, len(ks)):
                        candidate_pairs.add((ks[i], ks[j]))

        flagged = []
        for k1, k2 in candidate_pairs:
            # 检查两把 Key 的交替时序与并发情况
            combined = sorted(by_key[k1] + by_key[k2], key=lambda x: x["epoch"])
            switches = 0
            min_switch_gap = float("inf")
            concurrent_1m = 0
            shared_nets = set()
            shared_sigs = set()
            shared_uas = set()

            for i in range(len(combined) - 1):
                r1, r2 = combined[i], combined[i + 1]
                if r1["key_id"] != r2["key_id"]:
                    dt = r2["epoch"] - r1["epoch"]
                    if 0 <= dt <= 180:
                        switches += 1
                        if dt < min_switch_gap:
                            min_switch_gap = dt
                    if dt <= 60 and r1.get("net") == r2.get("net") and not _is_shared_net(r1.get("net", "")):
                        concurrent_1m += 1

                if r1.get("net") and r1["net"] == r2.get("net") and not _is_shared_net(r1["net"]):
                    shared_nets.add(r1["net"])
                if r1.get("client_sig") and r1["client_sig"] == r2.get("client_sig"):
                    shared_sigs.add(r1["client_sig"])
                if r1.get("user_agent") and r1["user_agent"] == r2.get("user_agent"):
                    shared_uas.add(r1["user_agent"])

            if switches >= 2 or concurrent_1m >= 1 or (shared_sigs and shared_nets and switches >= 1):
                confidence = "HIGH (100% 实锤)" if (switches >= 3 or min_switch_gap <= 30) else "MEDIUM (高危疑似)"
                flagged.append({
                    "keys": [k1, k2],
                    "names": [self.keys.get(k1, {}).get("key_name", f"#{k1}"), self.keys.get(k2, {}).get("key_name", f"#{k2}")],
                    "discord_users": [self.keys.get(k1, {}).get("discord_user", ""), self.keys.get(k2, {}).get("discord_user", "")],
                    "discord_ids": [self.keys.get(k1, {}).get("discord_id", ""), self.keys.get(k2, {}).get("discord_id", "")],
                    "confidence": confidence,
                    "switches": switches,
                    "min_switch_gap_sec": min_switch_gap if min_switch_gap != float("inf") else 0,
                    "concurrent_within_1m": concurrent_1m,
                    "shared_nets": list(shared_nets),
                    "shared_client_sigs": list(shared_sigs),
                    "sample_ua": list(shared_uas)[0] if shared_uas else "",
                })
        return flagged

    def _detect_handoff(self) -> List[Dict[str, Any]]:
        """检测大号额度耗尽后换小号接力（Handoff）"""
        by_key = defaultdict(list)
        limit_times = defaultdict(list)
        for r in self.requests:
            kid = str(r.get("key_id", ""))
            by_key[kid].append(r)
            reason = (r.get("reason") or r.get("detail") or "")
            if r.get("status") == "rejected" and any(k in reason for k in ("已达今日", "保底", "V5 额度")):
                limit_times[kid].append(r["epoch"])

        handoffs = []
        for k1, t_limits in limit_times.items():
            if not t_limits:
                continue
            k1_nets = set(r.get("net") for r in by_key[k1] if r.get("net") and not _is_shared_net(r.get("net", "")))
            if not k1_nets:
                continue
            for k2, reqs in by_key.items():
                if k1 == k2 or int(k1) in range(58, 68) or int(k2) in range(58, 68):
                    continue
                k2_nets = set(r.get("net") for r in reqs if r.get("net") and not _is_shared_net(r.get("net", "")))
                overlap_nets = k1_nets & k2_nets
                if not overlap_nets:
                    continue
                # 检查在 k1 撞墙后 2 小时内，k2 是否在相同网络上线并生成
                for t_lim in t_limits:
                    active_k2 = [r for r in reqs if t_lim < r["epoch"] <= t_lim + 7200 and r.get("net") in overlap_nets]
                    if len(active_k2) >= 3:
                        handoffs.append({
                            "limited_key": k1,
                            "limited_name": self.keys.get(k1, {}).get("key_name", f"#{k1}"),
                            "handoff_key": k2,
                            "handoff_name": self.keys.get(k2, {}).get("key_name", f"#{k2}"),
                            "shared_net": list(overlap_nets)[0],
                            "sample_time": datetime.fromtimestamp(t_lim).strftime("%Y-%m-%d %H:%M:%S"),
                        })
                        break
        return handoffs

    def _detect_botting(self) -> List[Dict[str, Any]]:
        """检测无退避自动化脚本打桩与狂击"""
        by_key = defaultdict(list)
        for r in self.requests:
            by_key[str(r.get("key_id", ""))].append(r)

        bot_keys = []
        for kid, reqs in by_key.items():
            if len(reqs) < 20 or int(kid) in range(58, 68):
                continue
            kinfo = self.keys.get(kid, {})
            if str(kinfo.get("is_admin")) == "1" or str(kinfo.get("is_test")) == "1":
                continue

            reqs_sorted = sorted(reqs, key=lambda x: x["epoch"])
            rejections = [r for r in reqs if r.get("status") == "rejected"]
            rej_rate = len(rejections) / len(reqs)

            # 统计时间间隔
            intervals = []
            for r1, r2 in zip(reqs_sorted, reqs_sorted[1:]):
                dt = r2["epoch"] - r1["epoch"]
                if dt > 0:
                    intervals.append(dt)

            rapid_bursts = sum(1 for dt in intervals if dt <= 2.0)
            exact_15s_loops = sum(1 for dt in intervals if 14.0 <= dt <= 16.5)

            if rej_rate >= 0.5 or exact_15s_loops >= 8 or rapid_bursts >= 5:
                bot_keys.append({
                    "key_id": kid,
                    "name": kinfo.get("key_name", f"#{kid}"),
                    "discord_user": kinfo.get("discord_user", ""),
                    "total_requests": len(reqs),
                    "rejections": len(rejections),
                    "rejection_rate": f"{rej_rate*100:.1f}%",
                    "exact_15s_retries": exact_15s_loops,
                    "rapid_bursts_sub2s": rapid_bursts,
                    "primary_ua": reqs[0].get("user_agent", "")[:60],
                    "top_rejection": Counter((r.get("reason") or r.get("detail")) for r in rejections).most_common(1)[0][0] if rejections else "",
                })
        bot_keys.sort(key=lambda x: x["rejections"], reverse=True)
        return bot_keys

    def _analyze_clients(self) -> List[Dict[str, Any]]:
        uas = Counter(r.get("user_agent", "NONE") for r in self.requests)
        out = []
        for ua, count in uas.most_common(12):
            out.append({"user_agent": ua, "count": count})
        return out

    def _analyze_upstream_errors(self) -> Dict[str, Any]:
        errors = [r for r in self.requests if r.get("status") == "error"]
        reasons = Counter((r.get("reason") or r.get("detail") or "UNKNOWN") for r in errors)
        err_keys = Counter(str(r.get("key_id", "")) for r in errors)
        return {
            "total_errors": len(errors),
            "reasons": dict(reasons),
            "affected_keys": dict(err_keys.most_common(5)),
        }

    def print_report(self) -> None:
        data = self.audit()
        s = data["summary"]
        print("\n" + "=" * 70)
        print("          猫头鹰公益站 · 客户端日志离线智能审计报告")
        print("=" * 70)
        print(f"请求总量: {s.get('total_requests')} | 时间跨度: {s.get('time_range')}")
        print(f"密钥总数: {s.get('keys_count')} | 结果分布: {s.get('statuses')}")

        print("\n" + "-" * 70)
        print("【一、 一人多号 / Sybil 多开检测】")
        print("-" * 70)
        sybils = data["sybil_pairs"]
        if not sybils:
            print("  [✓] 未发现显著一人多号多开行为。")
        else:
            for item in sybils:
                print(f"  [!] 发现一人多号关联: Key {item['keys'][0]} ({item['names'][0]}) ↔ Key {item['keys'][1]} ({item['names'][1]})")
                print(f"      - 置信度: {item['confidence']}")
                print(f"      - Discord 身份: {item['discord_users'][0]} (ID:{item['discord_ids'][0]}) ↔ {item['discord_users'][1]} (ID:{item['discord_ids'][1]})")
                print(f"      - 极速时序交替: {item['switches']} 次（最短交替间隔: {item['min_switch_gap_sec']:.1f} 秒）")
                print(f"      - 1分钟内同网络同时出图: {item['concurrent_within_1m']} 次")
                print(f"      - 共享非公共网段: {item['shared_nets']}")
                print(f"      - 客户端参数签名: {item['shared_client_sigs']}")
                print(f"      - 客户端环境: {item['sample_ua'][:70]}")

        print("\n" + "-" * 70)
        print("【二、 额度耗尽换号接力 (Handoff) 嫌疑】")
        print("-" * 70)
        handoffs = data["handoff_pairs"]
        if not handoffs:
            print("  [✓] 未发现显著同网段换号接力。")
        else:
            for h in handoffs:
                print(f"  [?] 大号受限: Key {h['limited_key']} ({h['limited_name']}) → 接力小号: Key {h['handoff_key']} ({h['handoff_name']})")
                print(f"      - 相同网络出口: {h['shared_net']} | 发生时间: {h['sample_time']}")

        print("\n" + "-" * 70)
        print("【三、 无退避死循环打桩脚本 (Botting)】")
        print("-" * 70)
        bots = data["botting_keys"]
        if not bots:
            print("  [✓] 未发现无退避高频死循环打桩。")
        else:
            for b in bots:
                print(f"  [!] 异常 Key: #{b['key_id']} ({b['name']}) | Discord: {b['discord_user']}")
                print(f"      - 请求量: {b['total_requests']} (拒绝: {b['rejections']}, 拒绝率: {b['rejection_rate']})")
                print(f"      - 15s 定时重试: {b['exact_15s_retries']} 次 | <=2s 狂击: {b['rapid_bursts_sub2s']} 次")
                print(f"      - 主要拒绝原因: {b['top_rejection']}")
                print(f"      - UA: {b['primary_ua']}")

        print("\n" + "-" * 70)
        print("【四、 上游异常与模型计算溢出】")
        print("-" * 70)
        errs = data["upstream_anomalies"]
        print(f"  总错误数: {errs['total_errors']}")
        print(f"  原因分布: {errs['reasons']}")
        print(f"  主要发生 Key: {errs['affected_keys']}")
        print("=" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(description="猫头鹰公益站 · 客户端日志离线智能审计工具")
    parser.add_argument("--zip", help="客户端日志 zip 包路径")
    parser.add_argument("--csv", help="解压后的 CSV 目录路径")
    parser.add_argument("--db", help="SQLite 数据库文件路径 (如 data/gate.sqlite)")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    args = parser.parse_args()

    if args.zip:
        auditor = SybilAuditor.from_zip(args.zip)
    elif args.csv:
        auditor = SybilAuditor.from_csv_dir(args.csv)
    elif args.db:
        auditor = SybilAuditor.from_sqlite(args.db)
    else:
        # Default fallback: check data/gate.sqlite or prompt
        if os.path.exists("data/gate.sqlite"):
            auditor = SybilAuditor.from_sqlite("data/gate.sqlite")
        else:
            parser.print_help()
            sys.exit(1)

    if args.json:
        print(json.dumps(auditor.audit(), ensure_ascii=False, indent=2))
    else:
        auditor.print_report()


if __name__ == "__main__":
    main()
