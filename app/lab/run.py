"""运行入口：python -m app.lab.run --db <副本> --out data/lab [--replicates 200] [--max-scale 10]
                  [--sweep account_hourly_cap=100,120,150,180,200] [--day YYYY-MM-DD] [--set name=value ...]

单进程、os.nice 降优先级；输出 <out>/<day>/report.png、capacity.png、summary.md、results.json。
--backtest 跑回测（≥ 14 个完整天）；--register-grid grid.json 事先登记参数网格（--sweep 必须与登记一致）。
--synthetic <路径> 先生成一份合成库再跑（开发 / 演示用）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from typing import Any


def _num(v: str) -> Any:
    try:
        f = float(v)
        return int(f) if f == int(f) and "." not in v else f
    except ValueError:
        return v


def parse_sweep(spec: str) -> tuple[str, list]:
    name, _, vals = spec.partition("=")
    if not name or not vals:
        raise SystemExit(f"--sweep 格式应为 name=v1,v2,...，收到 {spec!r}")
    return name.strip(), [_num(v.strip()) for v in vals.split(",") if v.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.lab.run", description="猫头鹰公益站实验室：回放 / 蒙特卡洛 / 日报")
    ap.add_argument("--db", help="数据库副本路径（只读打开）")
    ap.add_argument("--out", default="data/lab")
    ap.add_argument("--day", help="报告日期（本地），默认 = 数据里最后一天")
    ap.add_argument("--tz", default=os.environ.get("TZ", "Asia/Shanghai") if "/" in os.environ.get("TZ", "") else "Asia/Shanghai")
    ap.add_argument("--replicates", type=int, default=200)
    ap.add_argument("--max-scale", type=float, default=10)
    ap.add_argument("--sweep", help="单参数扫描，如 account_hourly_cap=100,120,150,180,200")
    ap.add_argument("--set", action="append", default=[], help="覆盖参数 name=value（可多次）")
    ap.add_argument("--seed", type=int, default=20261010)
    ap.add_argument("--time-budget", type=float, default=840, help="蒙特卡洛总时间预算（秒），超出时自动减少 R 并写进结果")
    ap.add_argument("--nice", type=int, default=10)
    ap.add_argument("--synthetic", help="先在此路径生成合成库（覆盖），并用它作为 --db")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--backtest", action="store_true", help="跑回测（需要 ≥ 14 个完整天；样本外每个网格只跑一次）")
    ap.add_argument("--register-grid", help="登记参数网格（JSON 文件：{\"model\": {...}, \"sweep\": {...}}），然后退出")
    a = ap.parse_args(argv)

    try:
        os.nice(a.nice)
    except (OSError, AttributeError):
        pass
    import numpy as np  # noqa: F401  延迟导入：让 --help 在没有 numpy 时也能用
    from . import backtest, data, montecarlo as mc, replay as rp, report

    log = (lambda *_: None) if a.quiet else (lambda m: print(m, flush=True))
    if a.register_grid:
        import json
        with open(a.register_grid, encoding="utf-8") as f:
            rec = backtest.register_grid(a.out, json.load(f), note="命令行登记")
        log(f"已登记网格 {rec['sha']}（{rec['registered_at']}）")
        return 0
    t0 = time.time()
    if a.synthetic:
        from . import synth
        if os.path.exists(a.synthetic):
            os.remove(a.synthetic)
        s = synth.generate(a.synthetic, days=16, deploy_at="2026-10-04 00:30")
        log(f"合成库：{s['requests']} 个请求 → {a.synthetic}")
        a.db = a.synthetic
    if not a.db:
        ap.error("需要 --db 或 --synthetic")
    ds = data.load(a.db, tz=a.tz)
    if ds.n == 0:
        log("没有出图记录，退出")
        return 1
    day = a.day or (ds.full_days() or [max(ds.days(), key=ds.coverage_hours)])[-1]   # 默认：最后一个完整天；没有完整天时取覆盖最长的一天
    params = rp.current_params(ds)
    for item in a.set:
        k, _, v = item.partition("=")
        if k not in params:
            ap.error(f"未知参数 {k}")
        params[k] = _num(v)
    log(f"数据：{ds.n} 个出图请求，{len(ds.days())} 天，{len(ds.members)} 位成员；报告日 {day}")

    if ds.synthetic:
        log("⚠ 合成数据：结果只用于检验管线自洽，不能当验证")
    calib = rp.calibrate(ds, params, seed=a.seed)
    log(f"校准（样本内，按规则版本分段）：拒绝率误差 {calib['abs_err_reject_rate']}，重试模型 "
        f"{[round(b['p'], 2) for b in calib['retry_model']['by_attempt']]}")
    theory = mc.theoretical_capacity(ds, params)
    blocks = mc.Blocks(ds, seed=a.seed)
    budget = a.time_budget
    sweep_spec = parse_sweep(a.sweep) if a.sweep else None
    share = 1.0 / (1 + (len(sweep_spec[1]) if sweep_spec else 0))
    mres = mc.run(blocks, params, replicates=a.replicates, max_scale=a.max_scale, seed=a.seed,
                  time_budget=budget * share, progress=log, theory=theory)
    for s_ in mres["suspicious"]:
        log("⚠ " + s_)
    sw = None
    if sweep_spec:
        name, values = sweep_spec
        if name not in params:
            ap.error(f"未知扫描参数 {name}")
        ok, rec = backtest.check_sweep_registered(a.out, name, values)
        if not ok:
            ap.error(f"扫描网格与登记的不一致：{name} 已登记为 {rec['grid']['sweep'][name]}（{rec['registered_at']}，"
                     f"{rec['sha']}）。事先登记的网格不能事后修改；要换网格请先用 --register-grid 登记新网格并说明理由")
        sw = mc.sweep(blocks, params, name, values, replicates=a.replicates, max_scale=a.max_scale, seed=a.seed,
                      time_budget=budget * share, theory=theory)
        sw["registered"] = {"sha": rec["sha"], "registered_at": rec["registered_at"]}
        for v in sw["values"]:
            f = v["failure"]["overall"]
            log(f"扫描 {name}={v['value']}：{f['scale']}× 起可能失控，区间 {f['interval']}")
    bt = backtest.run_backtest(ds, a.out, params=params, replicates=max(20, a.replicates // 4), seed=a.seed) \
        if a.backtest else None
    if bt:
        log(f"回测：{bt.get('note') or bt['oos']['score']}")
    an = report.analyze_day(ds, day, hourly_cap=int(params["account_hourly_cap"]) * int(params["accounts"]), seed=a.seed)
    out_dir = os.path.join(a.out, day)
    meta = {"db": os.path.basename(a.db), "generated_at": datetime.now().isoformat(timespec="seconds"),
            "day": day, "replicates": a.replicates, "max_scale": a.max_scale, "seed": a.seed,
            "seconds": None, "params": params, "data_notes": ds.notes, "events": ds.n, "days": ds.days(),
            "full_days": ds.full_days(), "synthetic": ds.synthetic, "changepoints": data.changepoints(ds),
            "python": sys.version.split()[0]}
    meta["seconds"] = round(time.time() - t0, 1)
    out = report.write_all(out_dir, an, mres, calib, sw, meta, bt)
    log(f"完成：{out['png']}（{meta['seconds']} 秒，字体 {out['font']}）")
    log(out["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
