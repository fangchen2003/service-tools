"""蒙特卡洛 / 回测 / 报告：负载放大时指标单调变坏、能找到失控点；模型可疑标注；回测流程；样本不足标注。"""
import json
import pathlib
import re

import pytest

np = pytest.importorskip("numpy")

from app.lab import backtest, data, montecarlo as mc, replay as rp, report, synth  # noqa: E402


@pytest.fixture(scope="module")
def ds(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("mc") / "syn.db")
    synth.generate(path, members=30, days=5, seed=4)
    return data.load(path)


def test_blocks_two_stage(ds):
    b = mc.Blocks(ds)
    assert len(b.days) >= 4 and not b.partial and b.registered == 30
    rng = np.random.default_rng(0)
    inp, info = b.sample(1.0, rng, jitter_min=0)
    assert np.all(np.diff(inp["arrival"]) >= 0) and info["day"] in [d["day"] for d in b.days]
    # 1× 以内不复制同一个成员-天：每个合成成员的请求数与当天某个 block 一致
    d = next(x for x in b.days if x["day"] == info["day"])
    sizes = sorted(len(blk["tod"]) for blk in d["blocks"])
    for k in np.unique(inp["key"]):
        assert int((inp["key"] == k).sum()) in sizes
    inp2, _ = b.sample(1.0, rng, jitter_min=0, n_active=3)
    assert len(np.unique(inp2["key"])) == 3


def test_load_increase_degrades_and_finds_failure(ds):
    b = mc.Blocks(ds)
    res = mc.run(b, {**rp.current_params(ds), "account_hourly_cap": 60}, replicates=30, max_scale=20, seed=1)
    meds = [s["member_reject_rate"]["median"] for s in res["scales"]]
    assert meds[-1] > meds[0]
    assert all(y >= x - 0.02 for x, y in zip(meds, meds[1:]))
    f = res["failure"]["overall"]
    assert f["scale"] is not None and f["binding"]
    assert f["interval"][1] is None or f["interval"][0] <= f["interval"][1]
    assert all(v == 30 for v in res["R_by_scale"].values()) and all(v == 3 for v in res["G_by_scale"].values())
    assert "experience" in res["failure"] and "safety" in res["failure"]


def test_safety_dimension_daily_baseline(ds):
    b = mc.Blocks(ds)
    res = mc.run(b, {**rp.current_params(ds), "safety_daily_baseline": 50}, replicates=20, max_scale=1, seed=1)
    assert res["failure"]["safety"]["scale"] is not None and "daily_total_max" in res["failure"]["safety"]["binding"]


def test_suspicious_when_failing_at_1x(ds):
    b = mc.Blocks(ds)
    res = mc.run(b, {**rp.current_params(ds), "account_hourly_cap": 5}, replicates=20, max_scale=1.5, seed=1)
    assert any("1×" in s for s in res["suspicious"])


def test_monte_carlo_deterministic(ds):
    b = mc.Blocks(ds)
    a = mc.run(b, None, replicates=20, max_scale=1, seed=7)
    c = mc.run(b, None, replicates=20, max_scale=1, seed=7)
    assert [s["first_to_image_p90"] for s in a["scales"]] == [s["first_to_image_p90"] for s in c["scales"]]


def test_sweep_low_cap_fails_earlier(ds):
    b = mc.Blocks(ds)
    sw = mc.sweep(b, rp.current_params(ds), "account_hourly_cap", [20, 150], replicates=20, max_scale=6, seed=2)
    f20, f150 = (v["failure"]["overall"]["scale"] for v in sw["values"])
    assert f20 is not None and (f150 is None or f20 <= f150)


def test_time_budget_reduces_replicates(ds):
    res = mc.run(mc.Blocks(ds), None, replicates=400, max_scale=2, seed=1, time_budget=0.3)
    assert min(res["R_by_scale"].values()) < 400


def test_theoretical_capacity(ds):
    th = mc.theoretical_capacity(ds, rp.current_params(ds))
    assert th["members"] > 0 and th["members"] == min(th["by_hour"], th["by_day"])


def test_backtest_insufficient_and_registry(ds, tmp_path):
    bt = backtest.run_backtest(ds, str(tmp_path))
    assert bt["status"] == "insufficient" and "14" in bt["note"]
    g1 = backtest.register_grid(str(tmp_path), {"model": {"jitter_min": [10]}, "sweep": {}})
    g2 = backtest.register_grid(str(tmp_path), {"model": {"jitter_min": [10]}, "sweep": {}})
    assert g1 == g2
    ok, rec = backtest.check_sweep_registered(str(tmp_path), "account_hourly_cap", [100, 150])
    assert ok and rec["grid"]["sweep"]["account_hourly_cap"] == [100, 150]
    assert backtest.check_sweep_registered(str(tmp_path), "account_hourly_cap", [100, 150])[0]
    assert not backtest.check_sweep_registered(str(tmp_path), "account_hourly_cap", [100, 200])[0]


def test_backtest_runs_oos_once(tmp_path):
    path = str(tmp_path / "long.db")
    synth.generate(path, members=20, days=15, seed=9, volume=10)
    d = data.load(path)
    assert len(d.full_days()) >= 14
    grid = {"model": {"jitter_min": [10]}, "sweep": {}}
    bt = backtest.run_backtest(d, str(tmp_path), grid=grid, replicates=10, seed=1)
    assert bt["status"] == "ok" and not bt["oos"]["already_run"] and len(bt["oos_days"]) == 4
    assert 0 <= bt["oos"]["score"]["coverage"] <= 1 and bt["oos"]["score"]["n"] > 0
    again = backtest.run_backtest(d, str(tmp_path), grid=grid, replicates=10, seed=1)
    assert again["oos"]["already_run"]


def test_insufficient_sample_report(tmp_path):
    pytest.importorskip("matplotlib")
    path = str(tmp_path / "short.db")       # 与真实情况一样：只有约 7.7 小时
    synth.generate(path, members=40, start="2026-10-09 18:18", end="2026-10-10 02:00", days=1, seed=8,
                   deploy_at="2026-10-10 00:40")
    d = data.load(path)
    day = d.days()[0]
    an = report.analyze_day(d, day, hourly_cap=150, B=200)
    assert an["coverage_hours"] < 20 and not an["comparable_days"]
    assert not an["wait"]["past"]["sufficient"]
    res = mc.run(mc.Blocks(d), None, replicates=20, max_scale=1.5, seed=1, theory=mc.theoretical_capacity(d, None))
    assert res["partial_data"]
    out = report.write_all(str(tmp_path / "out" / day), an, res, rp.calibrate(d), None, {"day": day},
                           backtest.run_backtest(d, str(tmp_path / "out")))
    s = out["summary"]
    assert len(s) <= report.SUMMARY_MAX and "合成数据" in s
    assert "14" in s or "样本不足" in s
    assert not re.search(r"显著|变长|变短|\*", s.replace("**猫头鹰公益站", "").replace("服务器日报**", ""))
    raw = (tmp_path / "out" / day / "results.json").read_text(encoding="utf-8")
    json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    assert pathlib.Path(out["png"]).stat().st_size > 20000 and pathlib.Path(out["capacity_png"]).exists()


def test_gateway_does_not_import_lab():
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    for f in root.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "app.lab" not in text and "from .lab" not in text and "import lab" not in text, f.name
    req = (root.parent / "requirements.txt").read_text()
    assert "numpy" not in req and "matplotlib" not in req


def test_post_report_dry_run_does_not_send(tmp_path, monkeypatch, capsys):
    import importlib.util
    import urllib.request
    spec = importlib.util.spec_from_file_location(
        "post_report", pathlib.Path(__file__).resolve().parents[1] / "deploy" / "ops" / "post_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    (tmp_path / "summary.md").write_text("**日报** @everyone", encoding="utf-8")
    (tmp_path / "report.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    assert mod.main(["--dir", str(tmp_path)]) == 0
    assert "dry-run" in capsys.readouterr().out
    payload, name, blob = mod.build(str(tmp_path))
    assert payload["allowed_mentions"] == {"parse": []}
    body, ctype = mod.multipart(payload, name, blob)
    assert b'name="files[0]"; filename="report.png"' in body and ctype.startswith("multipart/form-data; boundary=")
