"""实验室回放：确定性、各项上限、重试模型、规则版本分段、与日志的校准、只读打开。"""
import sqlite3

import pytest

np = pytest.importorskip("numpy")

from app.lab import data, replay as rp, synth  # noqa: E402


def make(arrivals, keys, *, dur=5.0, legacy=True):
    n = len(arrivals)
    return {"arrival": np.array(arrivals, dtype=float), "key": np.array(keys, dtype=np.int64),
            "dur": np.full(n, dur), "images": np.ones(n, dtype=np.int64), "legacy": np.full(n, legacy),
            "ok": np.ones(n, dtype=bool), "exo": np.zeros(n, dtype=bool), "priv": np.zeros(n, dtype=bool),
            "cap": np.full(n, -1, dtype=np.int64)}


BASE = {"interval_jitter": 0, "image_min_interval": 1.0, "key_image_min_interval": 0.0, "base_daily_images": 0,
        "quota_target_avg": 1000, "account_daily_cap": 0, "account_hourly_cap": 0, "account_3h_cap": 0,
        "queue_per_account": 100}


def codes(res):
    return [rp.NAME.get(int(c), "ok" if c == rp.R_OK else "err") for c in res["outcome"]]


def test_unknown_param_rejected():
    with pytest.raises(KeyError):
        rp.resolve({"no_such_param": 1})


def test_defaults_come_from_code():
    from app import guard, quota_algo
    p = rp.default_params()
    assert p["account_hourly_cap"] == guard.FIELDS["account_hourly_cap"][0]
    assert p["queue_per_account"] == guard.FIELDS["queue_per_account"][0]
    assert p["quota_target_avg"] == quota_algo.DEFAULTS["quota_target_avg"]
    assert p["service_mode"] == "max"


def test_hourly_cap():
    res = rp.replay(make([i * 60.0 for i in range(10)], list(range(10))), {**BASE, "account_hourly_cap": 5})
    assert codes(res) == ["ok"] * 5 + ["hourly"] * 5
    assert res["hours_at_cap"] == 1 and res["hourly_peak"] == 5


def test_three_hour_cap():
    res = rp.replay(make([i * 1500.0 for i in range(8)], list(range(8))),
                    {**BASE, "account_hourly_cap": 3, "account_3h_cap": 5})
    assert res["rejects"]["cap3h"] >= 1 and res["rejects"]["hourly"] == 0


def test_daily_account_cap_and_exhaust_time():
    res = rp.replay(make([36000.0 + i * 100 for i in range(6)], list(range(6))), {**BASE, "account_daily_cap": 4},
                    tz_offset=0)
    assert codes(res) == ["ok"] * 4 + ["daily_account"] * 2
    assert 10 <= res["exhaust_hour"] < 11 and res["daily_total_max"] == 4


def test_key_queue_and_key_concurrency():
    res = rp.replay(make([0.0, 0.0, 0.0], [1, 1, 1], dur=10), {**BASE, "key_image_queue": 1})
    assert codes(res) == ["ok", "ok", "key_queue"]
    assert np.nanmax(res["start"]) >= 10


def test_member_daily_cap():
    res = rp.replay(make([i * 100.0 for i in range(5)], [1] * 5), {**BASE, "quota_target_avg": 3})
    assert codes(res) == ["ok"] * 3 + ["member_daily"] * 2


def test_base_with_idle_borrowing():
    p = {**BASE, "quota_target_avg": 10, "base_daily_images": 2}
    assert codes(rp.replay(make([i * 100.0 for i in range(5)], [1] * 5), p)) == ["ok"] * 5
    # 两个服务台：Key 2 占着一个长任务（一直「有人在用」）→ Key 1 超过保底后不空闲 → 被拒
    inp = make([0.0, 100.0, 200.0, 300.0], [2, 1, 1, 1], dur=1.0)
    inp["dur"][0] = 1000.0
    busy = rp.replay(inp, {**p, "accounts": 2})
    assert codes(busy) == ["ok", "ok", "ok", "base"]


def test_queue_timeout():
    res = rp.replay(make([0.0] * 6, list(range(6)), dur=30), {**BASE, "queue_timeout": 70})
    c = codes(res)
    assert c[:3] == ["ok"] * 3 and set(c[3:]) == {"timeout"}


def test_site_queue_limit():
    res = rp.replay(make([0.0] * 5, list(range(5)), dur=30), {**BASE, "queue_per_account": 3, "queue_timeout": 1000})
    assert res["rejects"]["site_queue"] == 2


def test_service_cycle_is_max_of_duration_and_interval():
    """nai.py:349：间隔从开工算起，服务周期 = max(耗时, 间隔 + 抖动)。"""
    inp = make([0.0, 0.0], [1, 2], dur=10)
    assert np.nanmax(rp.replay(inp, {**BASE, "image_min_interval": 15})["start"]) == 15     # 间隔更长
    assert np.nanmax(rp.replay(make([0.0, 0.0], [1, 2], dur=20), {**BASE, "image_min_interval": 15})["start"]) == 20
    assert np.nanmax(rp.replay(inp, {**BASE, "image_min_interval": 15, "service_mode": "sum"})["start"]) == 25


def test_retry_model_generates_retries_and_chains():
    inp = make([i * 60.0 for i in range(10)], list(range(10)))
    p = {**BASE, "account_hourly_cap": 5}
    none = rp.replay(inp, p)
    always = rp.replay(inp, p, retry={"p": [1.0, 1.0, 0.0], "delays": np.array([10.0])})
    assert none["retries"] == 0 and always["retries"] > 0
    assert always["member_reject_rate"] == pytest.approx(0.5)      # 5 条需求链最终仍失败（重试也被每小时上限拒）
    assert always["rejected_modeled"] > none["rejected_modeled"]


def test_first_to_image_includes_generation_time():
    res = rp.replay(make([0.0], [1], dur=12), BASE)
    assert res["first_to_image_p90"] == pytest.approx(12)


def test_exogenous_and_privileged():
    inp = make([0.0, 1.0, 2.0], [1, 2, 3])
    inp["exo"][0] = True
    inp["priv"][1] = True
    res = rp.replay(inp, {**BASE, "quota_target_avg": 0})
    assert codes(res)[0] == "other" and res["denominator"] == 2


@pytest.fixture(scope="module")
def syn(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("lab") / "syn.db")
    synth.generate(path, members=30, days=4, seed=3, deploy_at="2026-10-03 12:00")
    return path


def test_replay_is_deterministic(syn):
    ds = data.load(syn)
    p = {**rp.current_params(ds), "interval_jitter": 5}
    spec = rp.retry_spec(data.retry_model(ds))
    a = rp.replay(rp.inputs_from_dataset(ds, seed=1), p, seed=9, retry=spec)
    b = rp.replay(rp.inputs_from_dataset(ds, seed=1), p, seed=9, retry=spec)
    assert np.array_equal(a["outcome_all"], b["outcome_all"]) and a["retries"] == b["retries"]
    assert a["first_to_image_p90"] == b["first_to_image_p90"]


def test_retry_identification_recovers_truth(syn):
    ds = data.load(syn)
    rm = data.retry_model(ds)
    truth = synth.RETRY_TRUTH["p"]
    assert abs(rm["by_attempt"][0]["p"] - truth[0]) < 0.15
    assert rm["feature_coverage"] > 0.9


def test_deploy_changepoint_segments_and_exclusion(syn):
    ds = data.load(syn)
    cps = data.changepoints(ds)
    assert any(c["kind"] == "deploy" for c in cps)
    segs = data.segments(ds)
    assert len(segs) == 2 and segs[0]["inferred"]["account_hourly_cap"] == 80
    assert segs[1]["inferred"].get("account_hourly_cap", 150) == 150
    keep = data.clean_mask(ds)
    dep = next(c["ts"] for c in cps if c["kind"] == "deploy")
    assert not keep[np.abs(ds.ev["arrival"] - dep) <= 1800].any()


def test_rule_change_inferred_without_ver(tmp_path, syn):
    path = str(tmp_path / "nover.db")
    src = sqlite3.connect(syn)
    dst = sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.execute("UPDATE usage_log SET ver=''")
    dst.commit()
    dst.close()
    ds = data.load(path)
    assert not ds.has_ver
    labels = [c["label"] for c in data.changepoints(ds) if c["kind"] == "rule"]
    assert any("429→402" in x for x in labels)            # V5 用完的状态码变化
    caps_after = {int(c) for c, t in zip(ds.ev["cap_in_msg"], ds.ev["arrival"]) if c and c != 80}
    assert not caps_after or any("80→" in x for x in labels)


def test_calibration_on_synthetic_matches_logs(syn):
    c = rp.calibrate(data.load(syn))
    assert c["abs_err_reject_rate"] < 0.02 and abs(c["rel_err_ok"]) < 0.03
    assert len(c["segments"]) == 2 and c["in_sample"]
    assert any("80" in n for n in c["notes"])


def test_load_is_read_only_and_parses(syn):
    ds = data.load(syn)
    assert ds.n > 100 and ds.registered == 30 and ds.synthetic
    assert set(ds.ev["reason"]) <= {"", "upstream_error", "other", *data.MODELED}
    assert (ds.ev["steps"][ds.ev["status"] == "ok"] > 0).all()
    con = data.connect_ro(syn)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM usage_log")
    con.close()


def test_classify():
    assert data.classify("rejected", "429 本小时出图量已达上限（每小时 80 张，用来保护上游账号）") == "hourly"
    assert data.classify("rejected", "429 最近 3 小时出图量已达上限（400 张") == "cap3h"
    assert data.classify("rejected", "429 今天的保底 100 张已用完。全站空闲时……有其他人在排队") == "base"
    assert data.classify("rejected", "402 已达今日 V5 额度") == "other"
    assert data.classify("error", "upstream 500") == "upstream_error"


def test_replay_models_time_paced_daily_cap():
    # v2.17 起每日上限按时段放行：0 点只放出 burst 张，之后每小时补 daily/24（回放要和线上一致才能回测加号）
    t0 = -28800.0                                        # 北京时间 0 点
    res = rp.replay(make([t0 + i * 20 for i in range(6)], list(range(6))),
                    {**BASE, "account_daily_cap": 24, "daily_burst": 2, "queue_per_account": 100})
    got = codes(res)
    assert got[:3] == ["ok"] * 3 and got[3:] == ["paced"] * 3       # 份额是连续累积的：放出 2.01 张时第 3 张也能过（和线上同一判断）
    res2 = rp.replay(make([t0 + i * 20 for i in range(6)], list(range(6))),
                     {**BASE, "account_daily_cap": 24, "daily_burst": 2, "accounts": 2, "queue_per_account": 100})
    assert codes(res2).count("ok") == 5                  # 2 个账号：放出的份额翻倍
