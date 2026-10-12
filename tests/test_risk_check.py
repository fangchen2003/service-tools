"""风险检查：出站指纹盘点留空项与白嫖信号（网段共用 / 用量集中 / V5 消耗）。"""
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.database import Database
from app.risk_check import collect, DAY

NOW = 1_800_000_000.0


@pytest_asyncio.fixture
async def env():
    tmp = tempfile.TemporaryDirectory()
    db = Database(str(Path(tmp.name) / "r.sqlite"))
    await db.connect()
    state = SimpleNamespace(db=db, share=None, guard=None,
                            settings=SimpleNamespace(global_daily_v5=0, image_min_interval=15,
                                                     key_image_min_interval=15),
                            day=lambda ts=None: "2026-10-10",
                            nai=SimpleNamespace(_client=None, pool=[]))
    yield db, state
    await db.close()
    tmp.cleanup()


async def _key(db, name: str, key_id: int) -> None:
    await db._db.execute(
        "INSERT INTO api_keys (id, name, token, created_at) VALUES (?,?,?,?)",
        (key_id, name, f"nai-{key_id}", NOW - 30 * DAY))
    await db._db.commit()


@pytest.mark.asyncio
async def test_items_are_concise_and_summary_adds_up(env):
    # 10-11 站长：检查项只留有用的，说明一句话；永远「不适用」的纯说明项删掉
    db, state = env
    data = await collect(state, NOW)
    ids = {it["id"] for it in data["items"]}
    assert not ids & {"browser_fingerprint", "egress_ip", "tls_stack"}
    assert all(len(it["detail"]) <= 40 for it in data["items"]), [it["detail"] for it in data["items"]]
    by_id = {it["id"]: it for it in data["items"]}
    assert by_id["tls_fingerprint"]["status"] == "warn" and "Chrome" in by_id["tls_fingerprint"]["detail"]
    assert sum(data["summary"][k] for k in ("ok", "warn", "bad", "na")) == len(data["items"])


@pytest.mark.asyncio
async def test_headers_item_lists_what_we_send(env):
    from app.nai import BROWSER_PROFILES
    _, state = env
    state.nai = SimpleNamespace(_client=object(), _http2=False, _post_jitter_min=1.0, _post_jitter_max=3.0,
                                pool=[SimpleNamespace(position=1, token="pst-abcdefgh1234",
                                                      browser_profile=BROWSER_PROFILES[0])])
    data = await collect(state, NOW)
    item = next(it for it in data["items"] if it["id"] == "outbound_headers")
    assert item["status"] == "ok"
    assert any("User-Agent" in e and "Chrome" in e for e in item["evidence"])
    assert any("Sec-Ch-Ua" in e for e in item["evidence"])
    assert not any("pst-abcdefgh1234" in e for e in item["evidence"])


@pytest.mark.asyncio
async def test_concentration_flags_single_key_dominating(env):
    db, state = env
    await _key(db, "独吃", 1)
    await _key(db, "路人", 2)
    for i in range(60):                       # Key 1 独占绝大多数
        await db._db.execute("INSERT INTO usage_log (ts, key_id, key_name, kind, status, images) "
                             "VALUES (?,?,?,?,?,?)", (NOW - i * 60, 1, "独吃", "image", "ok", 1))
    await db._db.execute("INSERT INTO usage_log (ts, key_id, key_name, kind, status, images) "
                         "VALUES (?,?,?,?,?,?)", (NOW - 120, 2, "路人", "image", "ok", 1))
    await db._db.commit()
    data = await collect(state, NOW)
    item = next(it for it in data["items"] if it["id"] == "concentration")
    assert item["status"] == "warn"
    assert any("独吃" in e for e in item["evidence"])


@pytest.mark.asyncio
async def test_net_overlap_ignores_two_keys_but_flags_three(env):
    db, state = env
    for kid in (1, 2):                        # 两个 Key 共用一个网段：常见（一家人），不提示
        await _key(db, f"k{kid}", kid)
        await db._db.execute("INSERT INTO key_sources (key_id, net_hash, label, first_seen, last_seen, hits) "
                             "VALUES (?,?,?,?,?,?)", (kid, "net-A", "1.2.3.*", NOW - DAY, NOW - 60, 1))
    await db._db.commit()
    item = next(it for it in (await collect(state, NOW))["items"] if it["id"] == "net_overlap")
    assert item["status"] == "ok"
    await _key(db, "k3", 3)                   # 第三个 Key 也在同一网段：值得留意
    await db._db.execute("INSERT INTO key_sources (key_id, net_hash, label, first_seen, last_seen, hits) "
                         "VALUES (?,?,?,?,?,?)", (3, "net-A", "1.2.3.*", NOW - DAY, NOW - 60, 1))
    await db._db.commit()
    item = next(it for it in (await collect(state, NOW))["items"] if it["id"] == "net_overlap")
    assert item["status"] == "warn"


@pytest.mark.asyncio
async def test_burn_today_reads_counters(env):
    db, state = env
    state.nai.pool = [SimpleNamespace(token_id="tok-1", position=1, v5_daily_limit=0, token="pst-xxxx-yyyy")]
    await db._db.execute("INSERT INTO upstream_token_counters (token_id, day, images, v5) VALUES (?,?,?,?)",
                         ("tok-1", "2026-10-10", 40, 90))
    await db._db.commit()
    item = next(it for it in (await collect(state, NOW))["items"] if it["id"] == "burn_today")
    assert item["status"] == "ok"             # 未设置全站日额度时只报告
    state.settings.global_daily_v5 = 100      # 90/100 = 90%，进入注意档
    item = next(it for it in (await collect(state, NOW))["items"] if it["id"] == "burn_today")
    assert item["status"] == "warn"
    assert any("90" in e for e in item["evidence"])
