"""2026-10-09 发布前审查发现的问题的回归测试（见 docs/REVIEW_2026-10-09.md）。"""
import asyncio
import json
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from app import main, policy, token_store
from app.body import parse_json
from app.database import Database
from app.nai import NaiClient, UpstreamError, clamp_retry_after, parse_retry_after
from app.registration import RegistrationService
from test_generation_integration import FakeState, image_body, post
from test_nai_integration import FakeDB as NaiDB, PNG


@pytest.fixture
def state(monkeypatch):
    s = FakeState()
    monkeypatch.setattr(main, "STATE", s)
    return s


# ---------------------------------------------------------------- 计费 / 参数

@pytest.mark.asyncio
async def test_v5_model_with_whitespace_is_still_v5(state):
    key = state.db.keys["fixture-1"]
    key.update(allow_anlas=False, daily_v5=1, image_model_scope="all")
    state.db.charges.append((1, dict(anlas=0, v5=1, images=1, legacy_free_images=0)))
    for spelling in ("nai-diffusion-5", "nai-diffusion-5 ", " NAI-Diffusion-5"):
        body = image_body()
        body["model"] = spelling
        assert (await post("/ai/generate-image", body)).status_code == 402   # 每日 V5 已用完
    assert not state.nai.calls


@pytest.mark.asyncio
async def test_model_name_is_normalized_before_forwarding(state):
    body = image_body()
    body["model"] = "  NAI-Diffusion-4-5-Full "
    assert (await post("/ai/generate-image", body)).status_code == 200
    assert state.nai.calls[-1][2]["model"] == "nai-diffusion-4-5-full"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("steps", 28.99), ("n_samples", 1.99), ("width", 1024.9),
                                         ("steps", True), ("height", -64)])
async def test_non_integer_image_numbers_rejected(state, field, value):
    state.db.keys["fixture-1"].update(allow_anlas=False)
    r = await post("/ai/generate-image", image_body(**{field: value}))
    assert r.status_code == 400 and r.json()["error"]["message"].removeprefix("猫头鹰公益站提醒：").startswith("图片参数无效")
    assert not state.nai.calls and not state.db.charges


@pytest.mark.asyncio
async def test_integral_float_numbers_are_accepted_as_ints(state):
    assert (await post("/ai/generate-image", image_body(steps=23.0))).status_code == 200
    assert state.nai.calls[-1][2]["parameters"]["steps"] == 23


def test_inpaint_priced_by_larger_strength():
    from test_generation_integration import PNG as PNG_B64
    body = image_body(image=PNG_B64, mask=PNG_B64, strength=1.0, img2img={"strength": 0},
                      width=1536, height=1536)
    body["model"] = "nai-diffusion-4-5-full-inpainting"
    full = image_body(image=PNG_B64, strength=1.0, width=1536, height=1536)
    assert policy.estimate_image_cost(body, is_opus=False)["anlas"] == \
        policy.estimate_image_cost(full, is_opus=False)["anlas"]


@pytest.mark.parametrize("raw", [
    b'{"scale": NaN}', b'{"scale": Infinity}', b'{"x": -Infinity}',
    b'{"x":' + b"[" * 40 + b"]" * 40 + b"}",
])
def test_strict_json_rejects_nan_and_deep_nesting(raw):
    with pytest.raises(ValueError):
        parse_json(raw)


@pytest.mark.asyncio
async def test_nan_and_deep_nesting_return_400_not_500(state):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, raise_app_exceptions=False),
                                 base_url="http://gate") as c:
        h = {"Authorization": "Bearer fixture-1", "content-type": "application/json"}
        nan = json.dumps(image_body()).replace('"steps": 28', '"steps": 28, "scale": NaN')
        assert (await c.post("/ai/generate-image", content=nan, headers=h)).status_code == 400
        deep = '{"model":"nai-diffusion-4-5-full","parameters":{"x":' + "[" * 900 + "]" * 900 + "}}"
        assert (await c.post("/ai/generate-image", content=deep, headers=h)).status_code == 400
    assert not state.nai.calls


def test_clamp_does_not_deepcopy_or_mutate_input():
    body = image_body(width=2048, height=2048, n_samples=4)
    junk = [{} for _ in range(10)]
    body["parameters"]["x"] = junk
    out, notes, problem = policy.clamp_image_params(body, max_pixels=1024 * 1024, max_steps=28,
                                                    allow_img2img=True)
    assert problem is None and notes
    assert out["parameters"]["x"] is junk                      # 未深拷贝大字段
    assert body["parameters"]["n_samples"] == 4                # 原始请求未被修改


@pytest.mark.asyncio
async def test_text_route_body_limit_is_small(state):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, raise_app_exceptions=False),
                                 base_url="http://gate") as c:
        big = json.dumps({"model": "kayra-v1", "input": "x" * (2 * 1024 * 1024)})
        r = await c.post("/ai/generate", content=big,
                         headers={"Authorization": "Bearer fixture-1", "content-type": "application/json"})
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_inflight_requests_per_key_are_capped(state):
    gate = asyncio.Event()

    async def slow(request):
        await gate.wait()
        return httpx.Response(200)

    wrapped = main.limit_inflight(slow)

    class Req:
        headers = {"authorization": "Bearer same"}

    tasks = [asyncio.create_task(wrapped(Req())) for _ in range(main.MAX_INFLIGHT_PER_KEY)]
    await asyncio.sleep(0)
    with pytest.raises(main.GateError) as e:
        await wrapped(Req())
    assert e.value.status == 429
    gate.set()
    await asyncio.gather(*tasks)
    assert not main._INFLIGHT


@pytest.mark.asyncio
async def test_streaming_responses_hold_inflight_slot_until_stream_ends():
    # 流式出图 / 文本：处理函数立即返回响应对象，真正的上游调用在推流里；名额必须占到推流结束
    from fastapi.responses import StreamingResponse
    done = asyncio.Event()

    async def body():
        await done.wait()
        yield b"x"

    async def streamer(request):
        return StreamingResponse(body())

    wrapped = main.limit_inflight(streamer)

    class Req:
        headers = {"authorization": "Bearer stream"}

    resps = [await wrapped(Req()) for _ in range(main.MAX_INFLIGHT_PER_KEY)]
    with pytest.raises(main.GateError):
        await wrapped(Req())                      # 4 个流还在推，第 5 个必须被拒
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        await asyncio.Event().wait()
    runs = [asyncio.create_task(r({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)) for r in resps]
    await asyncio.sleep(0)
    assert main._INFLIGHT["stream"] == main.MAX_INFLIGHT_PER_KEY
    done.set()
    await asyncio.gather(*runs)
    assert "stream" not in main._INFLIGHT


# ---------------------------------------------------------------- 上游

def _nai(handler):
    c = NaiClient(["fake-a"], "https://offline.invalid", "https://offline.invalid",
                  "https://offline.invalid", db=NaiDB(), day_fn=lambda: "2026-09-22",
                  v5_daily_limits=[0], allow_anlas=[True], image_min_interval=0)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


@pytest.mark.asyncio
async def test_slow_text_request_does_not_block_image_dispatch():
    dispatched = {}

    async def handler(req):
        if req.url.path == "/text":
            await asyncio.sleep(1.0)
            return httpx.Response(200, json={"output": "x"})
        dispatched["image"] = time.monotonic()
        return httpx.Response(200, content=PNG)

    c = _nai(handler)
    t0 = time.monotonic()
    text = asyncio.create_task(c.request("POST", "https://offline.invalid/text"))
    await asyncio.sleep(0.05)
    img = await c.request("POST", "https://offline.invalid/img", image_lane=True, queue_timeout=0.2)
    await text
    assert img.status_code == 200 and dispatched["image"] - t0 < 0.5


@pytest.mark.asyncio
async def test_text_stream_429_does_not_freeze_token_for_images():
    async def handler(req):
        if req.url.path == "/text-stream":
            return httpx.Response(429, headers={"retry-after": "1"})
        return httpx.Response(200, content=PNG)

    c = _nai(handler)
    with pytest.raises(UpstreamError):
        await c.stream("https://offline.invalid/text-stream", {})
    assert (await c.request("POST", "https://offline.invalid/img", image_lane=True)).status_code == 200


@pytest.mark.parametrize("header,expected", [
    ("inf", 20.0), ("nan", 20.0), ("1e9", 20.0), ("-5", 20.0), ("Wed, 21 Oct 2015 07:28:00 GMT", 20.0),
    ("1", 5.0), ("30", 30.0), ("99999999", 3600.0), (None, 20.0),
])
def test_retry_after_is_parsed_and_clamped(header, expected):
    assert parse_retry_after(header) == expected


@pytest.mark.parametrize("value", [float("inf"), float("nan"), 1e30, -1])
def test_retry_after_clamp_is_finite(value):
    assert 5.0 <= clamp_retry_after(value) <= 3600.0


@pytest.mark.asyncio
async def test_retry_after_inf_cannot_poison_cooldown():
    seen = []

    async def handler(req):
        return httpx.Response(429, headers={"retry-after": "inf"})

    c = _nai(handler)

    async def on_rl(seconds):
        seen.append(seconds)

    with pytest.raises(UpstreamError):
        await c.request("POST", "https://offline.invalid/img", image_lane=True, on_rate_limited=on_rl)
    assert seen == [20.0] and c.pool[0].blocked_until < time.time() + 3601


# ---------------------------------------------------------------- 后台

@pytest.mark.parametrize("value", [None, "", "  ", -1, "-3", 1.5, "abc", float("inf"), True])
def test_admin_numbers_never_fail_open(value):
    from fastapi import HTTPException
    from app.admin import _num
    with pytest.raises(HTTPException) as e:
        _num(value, int, 0, 100, "daily_images")
    assert e.value.status_code == 422


def test_admin_numbers_accept_valid_values():
    from app.admin import _num
    assert _num("7", int, 0, 100, "x") == 7 and _num(7.0, int, 0, 100, "x") == 7
    assert _num(2.5, float, 0, 100, "x") == 2.5


# ---------------------------------------------------------------- 注册

ROLE_GUILD = "1480185480048808009"


class _Discord:
    def __init__(self):
        self.calls, self.member_delay, self.role_delay, self.role_status = [], 0.0, 0.0, 204

    async def handler(self, request):
        p = request.url.path
        self.calls.append((request.method, p))
        if request.method == "GET" and "/members/" in p and "/roles/" not in p:     # 机器人核验身份组门槛
            await asyncio.sleep(self.member_delay)
            return httpx.Response(200, json={"roles": ["1335363403870502912"]})
        if p == "/api/users/@me/channels":
            return httpx.Response(200, json={"id": "dm"})
        if p == "/api/channels/dm/messages":
            return httpx.Response(200, json={})
        if "/roles/" in p:
            await asyncio.sleep(self.role_delay)
            return httpx.Response(self.role_status)
        raise AssertionError(p)


async def _service(tmp, **kw):
    db = Database(str(Path(tmp) / "g.sqlite"))
    await db.connect()
    await db.set_setting("register_open", "1")
    discord = _Discord()
    http = httpx.AsyncClient(transport=httpx.MockTransport(discord.handler))
    svc = RegistrationService(db, http, client_id="c", client_secret="s", bot_token="b",
                              bridge_secret="x" * 40, **kw)
    return db, http, svc, discord


async def _issue(svc, uid="777"):
    return await svc.issue_direct(uid, ROLE_GUILD)


@pytest.mark.asyncio
async def test_ban_during_inflight_registration_wins():
    with tempfile.TemporaryDirectory() as tmp:
        db, http, svc, discord = await _service(tmp)
        discord.member_delay = 0.3                       # 身份组核验（网络等待）期间封禁
        issue = asyncio.create_task(_issue(svc))
        await asyncio.sleep(0.1)
        await svc.ban("777")
        try:
            await issue
        except Exception:
            pass
        assert await svc.is_banned("777")
        rows = await db._db.execute_fetchall(
            "SELECT 1 FROM discord_registrations WHERE discord_id='777'")
        assert not rows
        assert (await db._db.execute_fetchall("SELECT count(*) FROM api_keys"))[0][0] == 0
        await http.aclose()
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_register_for_same_user_mints_one_key():
    with tempfile.TemporaryDirectory() as tmp:
        db, http, svc, discord = await _service(tmp)
        discord.member_delay = 0.2
        results = await asyncio.gather(_issue(svc), _issue(svc), return_exceptions=True)
        assert sum(isinstance(r, dict) and r["key"].startswith("nai-") for r in results) == 1
        assert sum(isinstance(r, Exception) and "已经领取过" in str(r) for r in results) == 1
        assert (await db._db.execute_fetchall("SELECT count(*) FROM api_keys"))[0][0] == 1
        await http.aclose()
        await db.close()


@pytest.mark.asyncio
async def test_reregistration_clears_stale_role_removal():
    with tempfile.TemporaryDirectory() as tmp:
        db, http, svc, discord = await _service(tmp, member_role_id="999")
        assert (await _issue(svc))["key"].startswith("nai-")
        discord.role_status = 429
        await svc.revoke("777")
        discord.role_status = 204
        assert (await _issue(svc))["key"].startswith("nai-")
        discord.calls.clear()
        await svc.sync_roles()
        assert ("DELETE", f"/api/v10/guilds/{ROLE_GUILD}/members/777/roles/999") not in discord.calls
        await http.aclose()
        await db.close()


@pytest.mark.asyncio
async def test_sync_roles_does_not_hold_write_lock_across_discord_calls():
    with tempfile.TemporaryDirectory() as tmp:
        db, http, svc, discord = await _service(tmp, member_role_id="999")
        for i in range(3):
            await db._db.execute("INSERT INTO pending_role_removals VALUES (?,?)", (str(100 + i), time.time()))
        await db._db.commit()
        key = await db.create_key({"name": "m", "token": "nai-x", "daily_images": 10, "monthly_anlas": 0,
                                   "daily_text_tokens": 0, "rpm": 5})
        discord.role_delay = 1.0
        sync = asyncio.create_task(svc.sync_roles())
        await asyncio.sleep(1.3)
        await db.record_success(key["id"], "m", "image", "nai-diffusion-3", "2026-10-09", images=1)
        await sync
        await http.aclose()
        await db.close()


@pytest.mark.asyncio
async def test_paused_members_keep_their_slot():
    with tempfile.TemporaryDirectory() as tmp:
        db, http, svc, discord = await _service(tmp)
        await db.set_setting("register_max_users", "1")
        assert (await _issue(svc))["key"].startswith("nai-")
        key = await svc.key_row_for("777")
        await db.update_key(key["id"], {"enabled": False})
        assert await svc.count_active() == 1
        await http.aclose()
        await db.close()


@pytest.mark.asyncio
async def test_register_default_all_features_round_trips():
    from app import ops
    with tempfile.TemporaryDirectory() as tmp:
        db, http, svc, discord = await _service(tmp)
        await ops.set_registration(db, {"features": None})
        assert (await ops.registration_settings(db, svc))["features"] is None      # 全部，而不是“一个都没有”
        await ops.set_registration(db, {"features": []})
        assert (await ops.registration_settings(db, svc))["features"] == []
        await http.aclose()
        await db.close()


def test_token_file_permissions_are_forced(tmp_path):
    path = tmp_path / "upstream_tokens.json"
    tmp = tmp_path / "upstream_tokens.json.tmp"
    tmp.write_text("{}")
    tmp.chmod(0o644)
    token_store.save(path, [{"token": "pst-" + "a" * 20, "allow_anlas": False}])
    assert path.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------- 公开面

@pytest.mark.asyncio
async def test_openapi_hidden_and_admin_headers(state):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, raise_app_exceptions=False),
                                 base_url="http://gate") as c:
        assert (await c.get("/openapi.json")).status_code == 404
        admin = await c.get("/admin")
        assert "frame-ancestors 'none'" in admin.headers["content-security-policy"]
        assert admin.headers["x-frame-options"] == "DENY"

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"cache-control", b"max-age=60")]})
        await send({"type": "http.response.body", "body": b"{}"})

    wrapped = main.AdminNoStoreMiddleware(inner)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://gate") as c:
        assert (await c.get("/admin/api/keys")).headers["cache-control"] == "no-store"
        assert (await c.get("/public/status")).headers["cache-control"] == "max-age=60"


# ---------------------------------------------------------------- 各功能统计

def test_every_recorded_kind_maps_to_a_feature():
    import re
    from app import features
    source = Path("app/main.py").read_text(encoding="utf-8")
    kinds = set(re.findall(r'record\(key, "([a-z_-]+)"', source)) | {"upscale", "augment-image"}
    assert kinds <= set(features.KIND_FEATURE), kinds - set(features.KIND_FEATURE)
    assert set(features.KIND_FEATURE.values()) == set(features.FEATURES)


@pytest.mark.asyncio
async def test_usage_by_kind_and_feature_log_filter():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(str(Path(tmp) / "g.sqlite"))
        await db.connect()
        key = await db.create_key({"name": "m", "token": "nai-x", "daily_images": 10, "monthly_anlas": 0,
                                   "daily_text_tokens": 1000, "rpm": 5})
        await db.record_success(key["id"], "m", "image", "nai-diffusion-4-5-full", "2026-10-09", images=1)
        await db.record_success(key["id"], "m", "upscale", "nai-diffusion-5-curated", "2026-10-09", anlas=7)
        await db.record_success(key["id"], "m", "chat", "llama-3-erato-v1", "2026-10-09", tokens=12)
        rows = {r["kind"]: r for r in await db.usage_by_kind(0)}
        assert rows["upscale"]["anlas"] == 7 and rows["chat"]["tokens"] == 12
        from app import features
        assert await db.count_logs(kinds=features.kinds_for("text")) == 1
        assert await db.count_logs(kinds=features.kinds_for("image")) == 1
        assert await db.count_logs(kinds=[]) == 0
        assert await db.count_logs() == 3
        await db.close()


# ---------------------------------------------------------------- 防 Key 分享：来源网段

def test_network_of_masks_and_groups():
    from app.key_sources import network_of
    assert network_of("120.235.155.213") == ("120.235.155.0/24", "120.235.*.*")
    assert network_of("120.235.155.7")[0] == network_of("120.235.155.213")[0]          # 同 /24 视为同一来源
    assert network_of("::ffff:120.235.155.9")[1] == "120.235.*.*"
    net, label = network_of("2408:8456:1234:5678::1")
    assert net == "2408:8456:1234::/48" and label == "2408:8456:…"
    assert network_of("testclient") is None and network_of("") is None


class _Alerts:
    def __init__(self):
        self.sent = []

    def notify(self, kind, message, *, cooldown=900):
        self.sent.append((kind, message))


@pytest.mark.asyncio
async def test_source_tracker_counts_networks_alerts_and_never_stores_full_ip():
    from app.key_sources import SourceTracker, WRITE_INTERVAL
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(str(Path(tmp) / "g.sqlite"))
        await db.connect()
        key = await db.create_key({"name": "m", "token": "nai-x", "daily_images": 10, "monthly_anlas": 0,
                                   "daily_text_tokens": 0, "rpm": 5})
        alerts = _Alerts()
        tracker = SourceTracker(db, alerts, threshold=3)
        tracker.notify_owner = True          # 线上默认关闭（由 share_guard 负责），这里测网段计数本身
        now = 1_800_000_000.0
        await tracker.observe(key, "120.235.155.213", now)
        await tracker.observe(key, "120.235.155.99", now + 1)          # 同网段：不增加
        await tracker.observe(key, "36.112.10.5", now + 2)
        assert len(await db.key_source_labels(key["id"], now - 10)) == 2 and not alerts.sent
        await tracker.observe(key, "2408:8456:1234:5678::1", now + 3)
        assert len(alerts.sent) == 1 and "3 个不同网段" in alerts.sent[0][1]
        rows = await db._db.execute_fetchall("SELECT net_hash, label FROM key_sources")
        dump = repr(list(map(tuple, rows)))
        assert "155.213" not in dump and "36.112.10" not in dump and "120.235.155" not in dump
        # 节流：同一来源 5 分钟内不重复写库
        before = (await db._db.execute_fetchall("SELECT SUM(hits) FROM key_sources"))[0][0]
        await tracker.observe(key, "36.112.10.5", now + 10)
        await tracker.observe(key, "36.112.10.5", now + 10 + WRITE_INTERVAL)
        assert (await db._db.execute_fetchall("SELECT SUM(hits) FROM key_sources"))[0][0] == before + 1
        # 管理员 Key 不记录；过期清理；删除 Key 时一并删除
        await tracker.observe({"id": 999, "name": "admin", "is_admin": 1}, "8.8.8.8", now)
        assert await db.key_source_labels(999, 0) == []
        assert await db.purge_key_sources(now + 7 * 86400 + 100) >= 3
        await tracker.observe(key, "1.2.3.4", now + 8 * 86400)
        await db.delete_key(key["id"])
        assert (await db._db.execute_fetchall("SELECT COUNT(*) FROM key_sources"))[0][0] == 0
        await db.close()


@pytest.mark.asyncio
async def test_source_tracker_threshold_zero_only_records():
    from app.key_sources import SourceTracker
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(str(Path(tmp) / "g.sqlite"))
        await db.connect()
        key = await db.create_key({"name": "m", "token": "nai-x", "daily_images": 10, "monthly_anlas": 0,
                                   "daily_text_tokens": 0, "rpm": 5})
        alerts = _Alerts()
        tracker = SourceTracker(db, alerts, threshold=0)
        for i, ip in enumerate(("1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4")):
            await tracker.observe(key, ip, 1_800_000_000.0 + i)
        assert not alerts.sent and len(await db.key_source_labels(key["id"], 0)) == 4
        await db.close()


# ---------------------------------------------------------------- 操作日志

def test_action_summary_hides_secrets():
    from app.action_log import summarize
    text = summarize({"token": "pst-secret-value", "password": "p", "html": "<b>x</b>", "rpm": 5,
                      "name": "x" * 200})
    assert "pst-secret" not in text and '"rpm":5' in text and "已隐去" in text and len(text) <= 301


@pytest.mark.asyncio
async def test_admin_writes_are_logged_with_target_name(tmp_path):
    from app.admin import AuditedRoute, router
    assert router.route_class is AuditedRoute
    db = Database(str(tmp_path / "g.sqlite"))
    await db.connect()
    from app.action_log import log_action
    await log_action(db, "后台 *.*.1.2", "删除 Key", "Key #2 davidzhao_refreshing", "")
    await log_action(db, "系统", "闲置回收 Key", "Key #3 m", "连续 3 天没有任何请求", ok=True)
    rows = await db.list_admin_actions()
    assert [r["action"] for r in rows] == ["闲置回收 Key", "删除 Key"] and await db.count_admin_actions() == 2
    assert await db.purge_admin_actions(time.time() + 1) == 2
    await db.close()


@pytest.mark.asyncio
async def test_inactive_cleanup_is_logged(tmp_path):
    from types import SimpleNamespace
    from app.state import GateState
    db = Database(str(tmp_path / "g.sqlite"))
    await db.connect()
    key = await db.create_key({"name": "idle", "token": "nai-idle", "daily_images": 1, "monthly_anlas": 0,
                               "daily_text_tokens": 0, "rpm": 5})
    await db._db.execute("UPDATE api_keys SET created_at=? WHERE id=?", (time.time() - 10 * 86400, key["id"]))
    await db._db.commit()
    fake = SimpleNamespace(db=db, settings=SimpleNamespace(key_inactivity_delete_days=3))
    assert await GateState.delete_inactive_keys(fake) == 1
    rows = await db.list_admin_actions()
    assert rows[0]["actor"] == "系统" and "idle" in rows[0]["target"]
    await db.close()


@pytest.mark.asyncio
async def test_v1_prefixed_nai_paths_are_aliased(state):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, raise_app_exceptions=False),
                                 base_url="http://gate") as c:
        h = {"Authorization": "Bearer fixture-1"}
        assert (await c.get("/v1/ai/user/subscription")).status_code == 401      # 路由存在，只是缺 Key
        assert (await c.post("/v1/ai/generate-image", json=image_body(), headers=h)).status_code == 200
        assert (await c.get("/v1/models")).status_code == 200                    # OpenAI 路由不受影响
    assert state.nai.calls


# ---------------------------------------------------------------- v1.1：上游参数预检、402/403 告警

@pytest.mark.parametrize("change,needle", [
    ({"width": 786}, "64 的倍数"),
    ({"height": 1000}, "64 的倍数"),
    ({"sampler": "ddim"}, "不支持 V4"),
    ({"sampler": "k_dpmpp_3m_sde"}, "不支持 V4"),
])
def test_upstream_parameter_problem_rejects_known_upstream_failures(change, needle):
    body = image_body(**change)
    body["model"] = "nai-diffusion-4-5-full"
    assert needle in (policy.upstream_parameter_problem(body) or "")


def test_upstream_parameter_problem_allows_normal_and_unknown_samplers():
    for sampler in ("k_euler_ancestral", "k_dpmpp_2m", "k_dpm_2", "some_future_sampler"):
        body = image_body(sampler=sampler)
        body["model"] = "nai-diffusion-4-5-full"
        assert policy.upstream_parameter_problem(body) is None
    v3 = image_body(sampler="ddim")
    v3["model"] = "nai-diffusion-3"
    assert policy.upstream_parameter_problem(v3) is None           # V3 对采样器更宽容
    i2i = image_body(width=786)
    i2i["action"] = "img2img"
    i2i["model"] = "nai-diffusion-4-5-full"
    assert policy.upstream_parameter_problem(i2i) is None          # 只约束文生图


def test_prompt_limit_counts_utf8_bytes():
    ok = image_body()
    ok["input"] = "猫" * 17000                                    # 51000 字节
    assert policy.upstream_parameter_problem(ok) is None
    long = image_body()
    long["parameters"]["v4_prompt"] = {"caption": {"base_caption": "x", "char_captions": [{"char_caption": "猫" * 17100}]}}
    assert "字节" in policy.upstream_parameter_problem(long)


@pytest.mark.asyncio
async def test_bad_dimensions_rejected_before_queue(state):
    r = await post("/ai/generate-image", image_body(width=786, height=786))
    assert r.status_code == 400 and "64" in r.json()["error"]["message"]
    assert not state.nai.calls and not state.db.charges


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [402, 403])
async def test_402_403_alert_but_do_not_disable_token(status):
    events = []

    async def handler(req):
        return httpx.Response(status, json={"message": "x"})

    c = _nai(handler)
    c.on_event = lambda kind, msg, cooldown=900: events.append(kind)
    r = await c.request("POST", "https://offline.invalid/text")
    assert r.status_code == status and events == [f"upstream_{status}"]
    assert c.pool[0].usable and not c.pool[0].disabled


@pytest.mark.asyncio
async def test_rotating_24s_inside_one_16_count_as_one_source():
    from app.key_sources import SourceTracker
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(str(Path(tmp) / "g.sqlite"))
        await db.connect()
        key = await db.create_key({"name": "warp", "token": "nai-w", "daily_images": 10, "monthly_anlas": 0,
                                   "daily_text_tokens": 0, "rpm": 5})
        alerts = _Alerts()
        tracker = SourceTracker(db, alerts, threshold=3)
        for i, ip in enumerate(("104.28.1.5", "104.28.77.9", "104.28.200.3", "104.28.9.1")):
            await tracker.observe(key, ip, 1_800_000_000.0 + i)
        assert await db.key_source_labels(key["id"], 0) == ["104.28.*.*"] and not alerts.sent
        assert (await db.key_source_summary(0))[key["id"]] == ["104.28.*.*"]
        await db.close()


# ---------------------------------------------------------------- v1.2：被拒请求统一记日志

def _rejections(state):
    return [(a[2], a[4], kw.get("detail", "")) for a, kw in state.db.logs if a[4] == "rejected"]


@pytest.mark.asyncio
async def test_disabled_key_rejection_is_logged(state):
    state.db.keys["fixture-1"]["enabled"] = 0
    assert (await post("/ai/generate-image", image_body())).status_code == 403
    await asyncio.sleep(0)
    assert _rejections(state) == [("image", "rejected", "403 该 Key 已被禁用")]


@pytest.mark.asyncio
async def test_quota_exhausted_rejection_is_logged_once(state):
    state.db.keys["fixture-1"]["daily_images"] = 1
    assert (await post("/ai/generate-image", image_body())).status_code == 200
    rejected = await post("/ai/generate-image", image_body())
    assert rejected.status_code == 402          # 额度用完不可重试；429 会让客户端自动重试并隐藏原因
    assert rejected.json()["message"].startswith("猫头鹰公益站提醒：") and "额度" in rejected.json()["message"]
    await asyncio.sleep(0)
    rejected = _rejections(state)
    assert len(rejected) == 1 and "额度已用完" in rejected[0][2]


@pytest.mark.asyncio
async def test_already_recorded_rejection_is_not_duplicated(state):
    assert (await post("/ai/generate-image", image_body(width=786))).status_code == 400
    await asyncio.sleep(0)
    assert len(_rejections(state)) == 1


@pytest.mark.asyncio
async def test_invalid_key_is_not_attributed_to_anyone(state):
    assert (await post("/ai/generate-image", image_body(), token="nai-unknown")).status_code == 401
    await asyncio.sleep(0)
    assert not state.db.logs


def test_kind_for_path_maps_member_routes():
    assert main._kind_for_path("/ai/generate-image") == "image"
    assert main._kind_for_path("/nai/ai/generate-image-stream") == "image_stream"
    assert main._kind_for_path("/ai/generate-image/suggest-tags") == "tags"
    assert main._kind_for_path("/ai/generate") == "text"
    assert main._kind_for_path("/v1/chat/completions") == "chat"
    assert main._kind_for_path("/user/subscription") == "account"


@pytest.mark.asyncio
async def test_idle_reminder_sent_once_24h_before_reclaim(tmp_path):
    from types import SimpleNamespace
    from app.state import GateState
    db = Database(str(tmp_path / "g.sqlite"))
    await db.connect()
    sent = []

    async def dm(discord_id, text):
        sent.append((discord_id, text))
        return True

    fake = SimpleNamespace(db=db, settings=SimpleNamespace(key_inactivity_delete_days=3))
    fresh = await db.create_key({"name": "fresh", "token": "nai-f", "daily_images": 1, "monthly_anlas": 0,
                                 "daily_text_tokens": 0, "rpm": 5})
    idle = await db.create_key({"name": "idle", "token": "nai-i", "daily_images": 1, "monthly_anlas": 0,
                                "daily_text_tokens": 0, "rpm": 5})
    await db._db.execute("UPDATE api_keys SET created_at=? WHERE id=?", (time.time() - 2.2 * 86400, idle["id"]))
    for discord_id, key in (("111", fresh), ("222", idle)):
        await db._db.execute("INSERT INTO discord_registrations(discord_id,key_id,created_at) VALUES (?,?,?)",
                             (discord_id, key["id"], time.time()))
    await db._db.commit()
    assert await GateState.remind_idle_keys(fake, dm, "https://gate.example") == 1
    assert sent[0][0] == "222" and "还没有成功生成过图片" in sent[0][1] and "https://gate.example" in sent[0][1]
    assert await GateState.remind_idle_keys(fake, dm, "https://gate.example") == 0          # 同一段闲置只提醒一次
    await db._db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (time.time() - 2.1 * 86400, idle["id"]))
    await db._db.commit()
    assert await GateState.remind_idle_keys(fake, dm, "https://gate.example") == 1          # 有过活动后重新计时
    actions = await db.list_admin_actions()
    assert actions[0]["action"] == "闲置回收前提醒"
    await db.close()


@pytest.mark.asyncio
async def test_get_v1_root_answers_connection_tests(state):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://gate") as c:
        r = await c.get("/v1")
    assert r.status_code == 200 and r.json()["object"] == "list"


# ---------------------------------------------------------------- v1.3：私信教程、图标、耗时与客户端、精简命令

def test_welcome_dm_is_a_full_tutorial_and_fits_discord():
    from app.registration import welcome_dm
    text = welcome_dm("nai-" + "k" * 48, "https://gate.example/", "V4.5 及以下 300 张", 30, 3, "📝 记录声明", "文生图")
    assert "nai-" + "k" * 48 in text and "https://gate.example/" in text
    assert "不要**加 /v1" in text and "柏宝绘" in text and "/resetkey" in text
    assert "30 天" in text and "连续 3 天" in text and "文生图" in text and "📝 记录声明" in text
    assert len(text) < 2000
    plain = welcome_dm("nai-x", "https://g/", "q", 0, 0)
    assert "有效期" not in plain and "回收" not in plain


def test_client_name_is_sanitized_and_bounded():
    from app.request_timing import client_name
    assert client_name("Mozilla/5.0\r\nX-Evil: 1") == "Mozilla/5.0 X-Evil: 1"
    assert len(client_name("a" * 500)) == 256
    assert client_name("") == ""


@pytest.mark.asyncio
async def test_log_records_wait_generation_time_and_client(state, monkeypatch):
    from app import request_timing
    real = state.nai.request

    async def slow_request(*args, **kwargs):
        await asyncio.sleep(0.05)
        request_timing.mark_sent()
        await asyncio.sleep(0.05)
        return await real(*args, **kwargs)
    monkeypatch.setattr(state.nai, "request", slow_request)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                 base_url="http://fixture.invalid") as client:
        r = await client.post("/ai/generate-image", json=image_body(),
                              headers={"Authorization": "Bearer fixture-1", "User-Agent": "BaiBaoHui/2.3"})
    assert r.status_code == 200
    await asyncio.sleep(0)
    _args, kwargs = state.db.logs[-1]
    assert kwargs["client"] == "BaiBaoHui/2.3"
    assert kwargs["wait_ms"] >= 40 and kwargs["dur_ms"] >= 40


@pytest.mark.asyncio
async def test_rejection_without_dispatch_has_no_generation_time(state):
    state.db.keys["fixture-1"]["enabled"] = 0
    assert (await post("/ai/generate-image", image_body())).status_code == 403
    await asyncio.sleep(0)
    rejected = [kw for args, kw in state.db.logs if args[4] == "rejected"]
    assert rejected and rejected[-1]["dur_ms"] == 0


@pytest.mark.asyncio
async def test_usage_log_timing_columns_round_trip(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    try:
        await db.add_log(1, "k", "image", "m", "ok", wait_ms=1500, dur_ms=8200, client="X" * 100)
        await db.record_success(1, "k", "image", "m", "2026-10-09", images=1, wait_ms=-5, dur_ms=10, client="c")
        rows = await db.list_logs(limit=5, offset=0)
        by_wait = {r["wait_ms"]: dict(r) for r in rows}
        assert by_wait[1500]["dur_ms"] == 8200 and len(by_wait[1500]["client"]) == 100
        assert by_wait[0]["client"] == "c" and by_wait[0]["dur_ms"] == 10
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_old_usage_log_gets_timing_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE usage_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, key_id INTEGER,
                   key_name TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
                   status TEXT NOT NULL, images INTEGER NOT NULL DEFAULT 0, anlas REAL NOT NULL DEFAULT 0,
                   tokens INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '')""")
    con.execute("INSERT INTO usage_log (ts, kind, status) VALUES (1, 'image', 'ok')")
    con.commit(); con.close()
    db = Database(str(path))
    await db.connect()
    try:
        await db.add_log(1, "k", "image", "m", "ok", wait_ms=3, dur_ms=4, client="c")
        rows = await db.list_logs(limit=5, offset=0)
        assert {r["wait_ms"] for r in rows} == {0, 3}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_favicon_served_as_svg(state):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://fixture.invalid") as c:
        for path in ("/favicon.ico", "/favicon.svg"):
            r = await c.get(path)
            assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
            assert r.content.startswith(b"<svg")
    for page in ("landing.html", "index.html"):
        assert 'rel="icon"' in (Path(main.__file__).parent / "static" / page).read_text()


def test_bot_command_set_is_trimmed():
    import ast
    src = (Path(__file__).parent.parent / "integration" / "discord_bot.py").read_text()
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "command":
            names |= {kw.value.value for kw in node.keywords if kw.arg == "name"}
    assert names == {"register", "quota", "resetkey", "help", "open", "limit", "ban", "unban", "slots", "revoke", "反馈"}


# ---------------------------------------------------------------- v1.4：V5 名额提醒、上游表现分析

@pytest.mark.asyncio
async def test_v5_capacity_warns_when_seats_exceed_global(tmp_path):
    from types import SimpleNamespace
    from app import ops
    db = Database(str(tmp_path / "c.db"))
    await db.connect()
    try:
        settings = SimpleNamespace(global_daily_v5=150)
        await db.set_settings_bulk({"register_max_users": 10, "register_daily_v5": 15, "register_image_scope": "all"})
        cap = await ops.v5_capacity(db, settings, None)
        assert cap == {"need": 150, "global": 150, "short": False, "message": ""}
        await db.set_setting("register_max_users", 12)
        cap = await ops.v5_capacity(db, settings, None)
        assert cap["short"] and cap["need"] == 180 and "180" in cap["message"]
        await db.set_setting("register_image_scope", "legacy")          # 不含 V5 时不提醒
        assert not (await ops.v5_capacity(db, settings, None))["short"]
        await db.set_settings_bulk({"register_image_scope": "all", "global_daily_v5": 0})   # 0 = 不限
        assert not (await ops.v5_capacity(db, settings, None))["short"]
    finally:
        await db.close()


def _perf_row(ts, model="nai-diffusion-4-5-full", status="ok", dur=8000, wait=500, up=200, detail=""):
    return (ts, model, status, 1 if status == "ok" else 0, wait, dur, up, detail)


def test_perf_family_split_and_capacity():
    from app import perf
    now = 1_000_000.0
    rows = [_perf_row(now - 60 * i) for i in range(10)] + [_perf_row(now - 30, "nai-diffusion-5", dur=20000)]
    r = perf.analyze(rows, now, slots=1, site_interval=15, key_interval=15)
    v45, v5 = r["families"]["V4.5"]["h1"], r["families"]["V5"]["h1"]
    assert v45["requests"] == 10 and v45["p50_ms"] == 8000 and v45["success_rate"] == 1.0
    assert v45["capacity_per_hour"] == 240 and v45["member_capacity_per_hour"] == 240   # 受 15s 间隔限制
    assert v5["capacity_per_hour"] == 180                                               # 受 20s 生成耗时限制
    assert r["flags"] == []


def test_perf_capacity_capped_by_hourly_cap_and_actual_moves():
    """产能上限要受全站每小时上限封顶（否则常年显示 3600/15=240 不动）；实际每小时出图随负载变化。"""
    from app import perf
    now = 1_000_000.0
    rows = [_perf_row(now - 60 * i) for i in range(10)]
    capped = perf.analyze(rows, now, slots=1, site_interval=15, key_interval=15, hourly_cap=150)["families"]["V4.5"]
    assert capped["h1"]["capacity_per_hour"] == 150 and capped["h1"]["member_capacity_per_hour"] == 150
    assert capped["h1"]["actual_per_hour"] == 10          # 最近 1 小时出了 10 张
    assert capped["d1"]["actual_per_hour"] == round(10 / 24, 1)
    more = perf.analyze(rows + [_perf_row(now - 5 - i) for i in range(20)], now, hourly_cap=150)["families"]["V4.5"]
    assert more["h1"]["actual_per_hour"] == 30            # 负载上来，实际数字跟着动


def test_perf_flags_slowdown_throttle_failures_and_account():
    from app import perf
    now = 2_000_000.0
    base = [_perf_row(now - 3 * 86400 - 60 * i, dur=8000) for i in range(20)]
    slow = [_perf_row(now - 60 * i, dur=16000) for i in range(6)]
    throttled = [_perf_row(now - 100 - i, status="error", dur=300, up=429) for i in range(3)]
    old_text_429 = [_perf_row(now - 200, status="error", dur=0, up=0, detail="上游限流(429)，全站图片生成已进入冷却")]
    account = [_perf_row(now - 3600 * 5, status="error", up=403)]
    r = perf.analyze(base + slow + throttled + old_text_429 + account, now)
    codes = {f["code"] for f in r["flags"]}
    assert {"slow", "throttle", "account"} <= codes
    throttle = next(f for f in r["flags"] if f["code"] == "throttle")
    assert "4 次" in throttle["text"] and throttle["family"] == "V4.5"
    assert r["families"]["V4.5"]["baseline_ready"]


def test_perf_needs_samples_before_judging_slowdown():
    from app import perf
    now = 3_000_000.0
    rows = [_perf_row(now - 3 * 86400, dur=1000), _perf_row(now - 60, dur=60000)]
    r = perf.analyze(rows, now)
    assert not any(f["code"] == "slow" for f in r["flags"])
    assert not r["families"]["V4.5"]["baseline_ready"]


def test_perf_hourly_buckets_cover_24h():
    from app import perf
    now = 3600 * 1000 + 1800.0
    rows = [_perf_row(now - 10), _perf_row(now - 10, status="error", up=429), _perf_row(now - 23 * 3600 - 1700)]
    hours = perf.analyze(rows, now)["families"]["V4.5"]["hourly"]
    assert len(hours) == 24 and hours[-1]["ok"] == 1 and hours[-1]["r429"] == 1 and hours[0]["ok"] == 1


@pytest.mark.asyncio
async def test_upstream_status_is_logged(state, monkeypatch):
    from app import request_timing
    real = state.nai.request

    async def tagged(*args, **kwargs):
        request_timing.mark_sent()
        request_timing.mark_status(200)
        return await real(*args, **kwargs)
    monkeypatch.setattr(state.nai, "request", tagged)
    assert (await post("/ai/generate-image", image_body())).status_code == 200
    await asyncio.sleep(0)
    assert state.db.logs[-1][1]["up_status"] == 200


@pytest.mark.asyncio
async def test_image_perf_rows_reads_usage_log(tmp_path):
    db = Database(str(tmp_path / "p.db"))
    await db.connect()
    try:
        await db.add_log(1, "k", "image", "nai-diffusion-5", "error", wait_ms=10, dur_ms=20, up_status=429)
        await db.add_log(1, "k", "tags", "", "ok")
        await db.add_log(1, "k", "image", "nai-diffusion-5", "rejected")
        rows = await db.image_perf_rows(0)
        assert len(rows) == 1 and rows[0][1:] == ("nai-diffusion-5", "error", 0, 10, 20, 429, "")
    finally:
        await db.close()


# ---------------------------------------------------------------- v1.5：测试 Key、影子模式、Anlas 说明

@pytest.mark.asyncio
async def test_test_keys_are_excluded_from_member_stats_and_never_reclaimed(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    try:
        base = dict(daily_images=10, monthly_anlas=0, daily_text_tokens=0, rpm=5)
        member = await db.create_key({"name": "member", "token": "nai-m", **base})
        tester = await db.create_key({"name": "tester", "token": "nai-t", "is_test": True, **base})
        assert tester["is_test"] == 1 and member["is_test"] == 0
        for key in (member, tester):
            await db.add_log(key["id"], key["name"], "image", "nai-diffusion-4-5-full", "ok", images=1, dur_ms=5)
            await db.add_log(key["id"], key["name"], "image", "nai-diffusion-4-5-full", "error", dur_ms=5)
        assert await db.image_stability(0) == {"ok": 1, "error": 1}
        assert len(await db.image_perf_rows(0)) == 2 and len(await db.shadow_rows(0)) == 2
        assert sum(r["n"] for r in await db.usage_by_kind(0)) == 2
        assert await db.count_logs(hide_test=True) == 2 and await db.count_logs() == 4
        await db.set_setting("key_inactivity_grace_started_at", 0)
        ids = await db.inactive_key_ids(time.time() + 10 * 86400)
        assert member["id"] in ids and tester["id"] not in ids
        await db.update_key(member["id"], {"is_test": True})
        assert (await db.get_key(member["id"]))["is_test"] == 1
    finally:
        await db.close()


def _shadow_row(ts, key, wait=0.0, dur=7.0, status="ok"):
    return (ts, key, f"k{key}", status, 1 if status == "ok" else 0, int(wait * 1000), int(dur * 1000))


def test_shadow_drr_lets_light_users_skip_a_burst():
    from app import shadow
    t0 = 10_000.0
    rows = []
    # A 在 t0 同时到达 4 张（真实系统里按先到先出依次出图），B/C/D 稍后各 1 张
    for i in range(4):
        start = t0 + 15 * i
        rows.append(_shadow_row(start + 7, 1, wait=start - t0))
    for j, key in enumerate((2, 3, 4)):
        arrival = t0 + 1 + j
        start = t0 + 60 + 15 * j
        rows.append(_shadow_row(start + 7, key, wait=start - arrival))
    r = shadow.analyze(rows, slots=1, interval=15, key_interval=15)
    assert r["samples"] == 7
    assert r["fifo"]["worst_member_mean"] == 87 and round(r["drr"]["worst_member_mean"]) == 56   # D 不再等完 A 的 4 张
    assert r["contention"]["peak_waiting_members"] == 3 and r["contention"]["contended_share"] > 0
    assert r["fifo"]["p50"] is not None and abs(r["actual"]["p90"] - r["fifo"]["p90"]) < 2   # 回放与实际一致


def test_shadow_quiet_day_has_no_contention_and_no_borrowing():
    from app import shadow
    rows = [_shadow_row(1000 + 300 * i, 1 + i % 3) for i in range(12)]
    r = shadow.analyze(rows)
    assert r["contention"]["contended_share"] == 0
    assert r["drr"]["p90"] == r["fifo"]["p90"] == 0
    assert r["quota"]["borrowers"] == 0 and r["load"]["busy_minutes"] == 0


def test_shadow_borrowing_and_busy_hysteresis():
    from app import shadow
    rows = [_shadow_row(5000 + 15 * i, 7) for i in range(120)]           # 30 分钟满负荷，同一个人
    rows += [_shadow_row(5000 + 15 * i + 1, 8) for i in range(3)]
    r = shadow.analyze(rows)
    q = r["quota"]
    assert q["members"][0]["images"] == 120 and q["members"][0]["borrowed"] == 120 - shadow.BASE_QUOTA
    assert q["borrowers"] == 1 and q["total"] == 123
    assert r["load"]["peak_util"] >= 0.9 and r["load"]["busy_minutes"] >= 15


def test_untimed_rows_count_for_quota_but_not_replay():
    from app import shadow
    rows = [(100.0, 1, "old", "ok", 1, 0, 0), _shadow_row(200, 1)]
    r = shadow.analyze(rows)
    assert r["samples"] == 1 and r["quota"]["total"] == 2


@pytest.mark.asyncio
async def test_anlas_feature_rejection_explains_why():
    from app import features
    key = {"is_admin": 0, "features": "image"}

    class DB:
        async def get_setting(self, *_):
            return None
    msg = await features.check(DB(), key, "vibe")
    assert "Anlas" in msg and "关闭" in msg
    assert "可向站长申请" in await features.check(DB(), key, "tags")


@pytest.mark.asyncio
async def test_img2img_rejection_tells_member_to_remove_reference(state):
    state.db.keys["fixture-1"].update(allow_img2img=False)
    r = await post("/ai/generate-image", image_body(image="aGVsbG8="))
    assert r.status_code == 400 and "移除参考图" in r.json()["error"]["message"]


def test_welcome_dm_states_free_generation_rules():
    from app.registration import welcome_dm
    text = welcome_dm("nai-x", "https://g/", "q", 30, 3)
    assert "1024×1024" in text and "28 步" in text and "Anlas" in text


# ---------------------------------------------------------------- v1.6：账号保护（P0）与排队上限（P1）

class _CounterDB:
    def __init__(self, images=0):
        self.images, self.saved = images, {}

    async def get_upstream_counter(self, token_id, day):
        return {"images": self.images, "v5": 0}

    async def get_setting(self, name, default):
        return self.saved.get(name, default)

    async def set_settings_bulk(self, values):
        self.saved.update(values)


@pytest.mark.asyncio
async def test_guard_settings_validate_and_persist():
    from app.guard import Guard
    db = _CounterDB()
    g = Guard(db)
    await g.save({"account_daily_cap": 800, "unknown": 5})
    assert g.values["account_daily_cap"] == 800 and db.saved == {"guard_account_daily_cap": 800}
    for bad in ({"account_hourly_cap": 999}, {"quiet_start": 1.5}, {"base_daily_images": True}, {"interval_jitter": -1}):
        with pytest.raises(ValueError):
            await g.save(bad)
    fresh = Guard(db)
    await fresh.load()
    assert fresh.values["account_daily_cap"] == 800 and fresh.values["account_hourly_cap"] == 150


def test_guard_quiet_hours_wrap_and_hourly_cap():
    from app.guard import Guard
    from datetime import datetime
    from zoneinfo import ZoneInfo
    g = Guard()
    g.values["account_hourly_cap"] = 80
    at = lambda h: datetime(2026, 10, 9, h, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    assert not g.in_quiet(at(3)) and g.hourly_cap(at(3)) == 80       # 默认不启用安静时段（夜里不限速）
    g.values.update(quiet_start=3, quiet_end=8)
    assert g.in_quiet(at(3)) and not g.in_quiet(at(8)) and not g.in_quiet(at(1))
    assert g.hourly_cap(at(3)) == 20 and g.hourly_cap(at(12)) == 80
    g.values.update(quiet_start=23, quiet_end=6)
    assert g.in_quiet(at(23)) and g.in_quiet(at(2)) and not g.in_quiet(at(12))
    g.values.update(quiet_start=4, quiet_end=4)
    assert not g.in_quiet(at(4))


@pytest.mark.asyncio
async def test_guard_blocks_token_on_daily_and_hourly_caps():
    from app.guard import Guard
    from datetime import datetime
    from zoneinfo import ZoneInfo
    noon = datetime(2026, 10, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    g = Guard()
    g.values.update(account_hourly_cap=80, daily_burst=0)       # 按时段放行另见 test_fair_pacing
    assert await g.token_block_reason(_CounterDB(999), "t", "d", noon) is None
    assert "明天 0 点" in await g.token_block_reason(_CounterDB(1000), "t", "d", noon)
    for i in range(80):
        g.record_start("t", noon - 3000 + i)
    reason = await g.token_block_reason(_CounterDB(0), "t", "d", noon)
    assert "每小时 80 张" in reason and "分钟后" in reason
    assert g.hour_count("t", noon + 700) == 0                 # 一小时滑出窗口后恢复
    g.values["account_daily_cap"] = g.values["account_hourly_cap"] = 0
    assert await g.token_block_reason(_CounterDB(10 ** 6), "t", "d", noon) is None


def test_guard_image_queue_per_key_and_site():
    from app.guard import Guard
    g = Guard()
    assert g.admit_image(1, accounts=1) is None and g.admit_image(1, accounts=1) is None
    assert "上一张图还没出完" in g.admit_image(1, accounts=1)          # 1 张生成 + 1 张排队
    g.release_image(1)
    assert g.admit_image(1, accounts=1) is None
    g.values["queue_per_account"] = 3
    assert g.admit_image(2, accounts=1) is None
    assert "排队的人太多" in g.admit_image(3, accounts=1)
    assert g.admit_image(3, accounts=1).code == "queue_full"          # 原因码随拒绝说明一起返回，统计按码数
    for _ in range(3):
        g.release_image(1)
    assert 1 not in g.image_inflight


def test_guard_site_idle_for_borrowing():
    from app.guard import Guard
    g = Guard()
    now = 1_000_000.0
    assert g.site_idle(1, ["t"], now)
    g.image_inflight[2] = 1
    assert not g.site_idle(1, ["t"], now)                      # 别人在排队就不借
    g.image_inflight.clear()
    g.image_inflight[1] = 1
    assert g.site_idle(1, ["t"], now)                          # 自己的不算
    for i in range(int(0.6 * g.hourly_cap(now))):
        g.record_start("t", now - 10 - i)
    assert not g.site_idle(1, ["t"], now)                      # 本小时用量达到上限的 60%


@pytest.mark.asyncio
async def test_second_image_from_same_key_is_rejected_while_first_runs(state):
    from app.guard import Guard
    state.guard = Guard()
    state.guard.values["key_image_queue"] = 0
    state.nai.release = asyncio.Event()
    first = asyncio.create_task(post("/ai/generate-image", image_body()))
    await asyncio.wait_for(state.nai.entered.wait(), 2)
    second = await post("/ai/generate-image", image_body())
    assert second.status_code == 429 and "上一张图还没出完" in second.json()["error"]["message"]
    state.nai.release.set()
    assert (await first).status_code == 200
    assert state.guard.image_inflight == {}
    assert (await post("/ai/generate-image", image_body())).status_code == 200


@pytest.mark.asyncio
async def test_base_quota_borrowing_only_when_site_is_idle(state):
    from app.guard import Guard
    state.guard = Guard()
    state.guard.values.update(base_daily_images=1, daily_burst=0)
    state.db.keys["fixture-1"]["daily_images"] = 5
    assert (await post("/ai/generate-image", image_body())).status_code == 200      # 保底内
    state.guard.image_inflight[999] = 1                                               # 另一位成员在排队
    busy = await post("/ai/generate-image", image_body())
    assert busy.status_code == 429 and "保底 1 张已用完" in busy.json()["error"]["message"]
    state.guard.image_inflight.clear()
    assert (await post("/ai/generate-image", image_body())).status_code == 200      # 空闲时借用


@pytest.mark.asyncio
async def test_nai_pick_token_refuses_when_every_account_is_capped():
    from app.guard import Guard
    db = NaiDB()
    db.get_upstream_counter = lambda token_id, day: asyncio.sleep(0, {"images": 5, "v5": 0})
    client = NaiClient(["pst-a"], "https://image.invalid", "https://text.invalid", "https://text.invalid",
                       db=db, day_fn=lambda: "2026-10-09", v5_daily_limits=[], allow_anlas=[False])
    client.guard = Guard()
    client.guard.values["account_daily_cap"] = 5
    with pytest.raises(UpstreamError) as caught:
        await client.pick_token(image_job=True)
    assert caught.value.status == 429 and "出图总量已达上限" in caught.value.message
    assert await client.pick_token() is not None                # 文本 / 标签补全不受出图上限影响
    client.guard.values["account_daily_cap"] = 6
    assert await client.pick_token(image_job=True) is not None


def test_cooling_account_is_not_reported_as_v5_quota_used_up():
    client = NaiClient(["pst-a"], "https://image.invalid", "https://text.invalid", "https://text.invalid",
                       db=NaiDB(), day_fn=lambda: "2026-10-09", v5_daily_limits=[10], allow_anlas=[False])
    client.pool[0].blocked_until = time.time() + 40
    e = client._unavailable(False, v5_free=True)
    assert e.status == 503 and "冷却" in e.message and "不是额度用完" in e.message
    client.pool[0].blocked_until = 0
    assert "V5 免费图片额度已用完" in client._unavailable(False, v5_free=True).message   # 真用完时仍照实说


# ---------------------------------------------------------------- v1.7：实时架构图、Anlas 自动分配、后台通知、防转卖信号

def test_guard_queue_view_tracks_waiting_and_running_positions():
    from app.guard import Guard
    g = Guard()
    g.values["key_image_queue"] = 1
    for key in (1, 2, 1, 3):
        assert g.admit_image(key, accounts=1) is None
    g.mark_running(2)
    view = g.queue_view(1)
    assert view["waiting"] == 3 and view["running"] == 1
    assert view["mine"] == [{"state": "waiting", "position": 1}, {"state": "waiting", "position": 2}]
    assert g.queue_view(2)["mine"] == [{"state": "running"}]
    g.release_image(2)
    assert g.queue_view()["running"] == 0 and len(g.entries) == 3
    for key in (1, 1, 3):
        g.release_image(key)
    assert g.entries == [] and g.image_inflight == {}


@pytest.mark.asyncio
async def test_running_state_follows_first_upstream_dispatch(state, monkeypatch):
    from app.guard import Guard
    from app import request_timing
    state.guard = Guard()
    seen = []
    real = state.nai.request

    async def dispatch(*args, **kwargs):
        seen.append(state.guard.queue_view(1)["mine"])
        request_timing.mark_sent()
        seen.append(state.guard.queue_view(1)["mine"])
        return await real(*args, **kwargs)
    monkeypatch.setattr(state.nai, "request", dispatch)
    assert (await post("/ai/generate-image", image_body())).status_code == 200
    assert seen == [[{"state": "waiting", "position": 1}], [{"state": "running"}]]
    assert state.guard.entries == []


@pytest.mark.asyncio
async def test_public_live_has_only_aggregates(tmp_path):
    from types import SimpleNamespace
    from app import live
    from app.guard import Guard
    db = Database(str(tmp_path / "l.db"))
    await db.connect()
    try:
        g = Guard(db)
        g.admit_image(7, accounts=1)
        tok = SimpleNamespace(token_id="t", usable=True, disabled=False, admin_enabled=True, blocked_until=0, image_next_at=0)
        st = SimpleNamespace(db=db, guard=g, nai=SimpleNamespace(pool=[tok]), day=lambda: "2026-10-09",
                             settings=SimpleNamespace(global_daily_v5=150, image_min_interval=15),
                             image_cooldown_remaining=lambda: 0)

        class Reg:
            async def settings(self): return {"max_users": 30, "open": True}
            async def count_active(self): return 13
            async def waitlist(self): return [{"name": "千分之一的心", "invited_at": None, "discord_id": "9988776655"}]
        live._cache.update(at=0, body=None)
        live.note("ok"); live.note("rejected")
        body = await live.build(st, Reg())
        text = json.dumps(body, ensure_ascii=False)
        assert body["queue"] == {"waiting": 1, "running": 0} and body["quota"]["images_cap"] == 1000
        assert body["members"]["waitlist"] == [{"name": "千分**", "invited": False}]
        assert "9988776655" not in text and "千分之一" not in text and "token" not in text.lower().replace("token_", "")
        assert body["auth"]["rejected"] >= 1
        me = live.mine(st, 7)
        assert me["mine"] == [{"state": "waiting", "position": 1, "eta": 15}]
        # 回归：/public/live 绝不能就地改 live.build() 的共享缓存对象——
        # 否则两个并发请求会在 await 处交错，把 A 的「我的排队」泄给没 Key 的 B。
        assert "me" not in body                      # 缓存对象本身不带 me
        resp = {**body, "me": me}                    # 处理器必须用浅拷贝拼响应（见 main.py public_live）
        assert resp["me"] is not None and "me" not in live._cache["body"]   # 缓存未被污染
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_live_me_is_passive_and_quiet_on_bad_keys(state):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://fixture.invalid") as c:
        ok = await c.get("/v1/live/me", headers={"Authorization": "Bearer fixture-1"})
        bad = await c.get("/v1/live/me", headers={"Authorization": "Bearer nai-nope"})
    assert ok.status_code == 200 and ok.json()["mine"] == []
    assert bad.status_code == 401 and bad.json()["mine"] is None
    assert not [a for a, kw in state.db.logs if a[4] == "rejected"]


def test_anlas_plan_spends_down_before_refill():
    from app.anlas_pool import plan
    now = 1_000_000.0
    p = plan(9949, now + 16 * 86400, now, 10, reserve=1000, cap=78, budget_left=None, month_days_left=20)
    assert p["daily_pool"] == 559 and p["per_member"] == 55
    late = plan(9949, now + 2 * 86400, now, 10, reserve=1000, cap=78, budget_left=None, month_days_left=20)
    assert late["per_member"] == 78                                        # 临近续费，用不完的提高到上限
    assert plan(9949, now + 16 * 86400, now, 10, reserve=1000, cap=78, budget_left=300, month_days_left=20)["per_member"] == 0
    assert plan(1200, now + 16 * 86400, now, 3, reserve=1000, cap=78, budget_left=None, month_days_left=20)["per_member"] == 0
    assert plan(9949, now + 86400, now, 0, reserve=1000, cap=78, budget_left=None, month_days_left=5)["per_member"] == 0


@pytest.mark.asyncio
async def test_anlas_rebalance_grants_active_v5_members_and_revokes(tmp_path):
    from types import SimpleNamespace
    from app import anlas_pool
    db = Database(str(tmp_path / "a.db"))
    await db.connect()
    try:
        now = time.time()
        base = dict(daily_images=300, monthly_anlas=0, daily_text_tokens=0, rpm=5, image_model_scope="all")
        active = await db.create_key({"name": "活跃", "token": "nai-a", **base})
        quiet = await db.create_key({"name": "不活跃", "token": "nai-b", **base})
        manual = await db.create_key({"name": "手动", "token": "nai-c", "allow_anlas": True, "daily_anlas": 500, **base})
        for k, d in ((active, "111"), (quiet, "222"), (manual, "333")):
            await db._db.execute("INSERT INTO discord_registrations(discord_id,key_id,created_at) VALUES (?,?,?)", (d, k["id"], now))
        await db._db.execute("UPDATE api_keys SET created_at=?", (now - 5 * 86400,))
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        await db._db.execute("INSERT INTO counters(key_id, day, images) VALUES (?,?,20)", (active["id"], today))
        await db._db.commit()
        st = SimpleNamespace(db=db, nai=SimpleNamespace(pool=[]), day=lambda: today, month=lambda: today[:7],
                             settings=SimpleNamespace(global_monthly_anlas=0))
        sent = []

        async def notify(key_id, text):
            sent.append(key_id)
        r = await anlas_pool.rebalance(st, now=now, account={"anlas": 5000, "refill_at": now + 10 * 86400}, notify=notify)
        assert r["per_member"] == 90 and r["members"] == ["活跃"]
        row = await db.get_key(active["id"])
        assert row["allow_anlas"] == 1 and row["anlas_auto"] == 1 and row["daily_anlas"] == 90 and sent == [active["id"]]
        assert (await db.get_key(manual["id"]))["daily_anlas"] == 500            # 手动设置不受影响
        await db._db.execute("DELETE FROM counters"); await db._db.commit()
        await anlas_pool.rebalance(st, now=now, account={"anlas": 5000, "refill_at": now + 10 * 86400}, notify=notify)
        row = await db.get_key(active["id"])
        assert row["allow_anlas"] == 0 and row["anlas_auto"] == 0 and sent == [active["id"]]
        assert json.loads(await db.get_setting(anlas_pool.STATE_KEY, "{}"))["per_member"] == 0
        # 站长在成员页选了「Anlas 关闭」（-1）：即使又活跃了，算法也不会再开
        await db._db.execute("UPDATE api_keys SET anlas_auto=-1 WHERE id=?", (active["id"],))
        await db._db.execute("INSERT INTO counters(key_id, day, images) VALUES (?,?,20)", (active["id"], today))
        await db._db.commit()
        r = await anlas_pool.rebalance(st, now=now, account={"anlas": 5000, "refill_at": now + 10 * 86400}, notify=notify)
        assert r["members"] == [] and (await db.get_key(active["id"]))["allow_anlas"] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v5_top_up_only_when_account_v5_is_empty(state):
    """账号 V5 还有剩余时，NovelAI 扣的是 V5 额度不是 Anlas：续杯只在账号 V5 用完（≤ 1%）时才走 Anlas。"""
    import json as _json
    key = state.db.keys["fixture-1"]
    key.update(allow_anlas=True, anlas_auto=1, daily_v5=1, daily_anlas=100, image_model_scope="all")
    state.db.charges.append((1, dict(anlas=0, v5=1, images=1, legacy_free_images=0)))
    body = image_body(width=832, height=1216)
    body["model"] = "nai-diffusion-5-full"
    await state.db.set_setting("quota_algo_last", _json.dumps({"v5": {"percent": 97}}))
    assert (await post("/ai/generate-image", body)).status_code == 402        # 个人 V5 用完，账号还有额度：不续杯
    await state.db.set_setting("quota_algo_last", _json.dumps({"v5": {"percent": 0.5}}))
    r = await post("/ai/generate-image", body)
    assert r.status_code == 200
    _, kwargs = state.nai.calls[-1][2], state.nai.calls[-1][3]
    assert kwargs["requires_anlas"] and not kwargs["v5_free"]
    key.update(daily_anlas=1000)                                           # 超规格图按像素扣，给够当天 Anlas
    big = image_body(width=1536, height=1536)
    big["model"] = "nai-diffusion-4-5-full"
    assert (await post("/ai/generate-image", big)).status_code == 200
    params = state.nai.calls[-1][2]["parameters"]
    assert params["width"] * params["height"] > 1024 * 1024               # 有 Anlas：不钳制，按 Anlas 扣


@pytest.mark.asyncio
async def test_auto_anlas_pays_for_paid_only_operations(state):
    """放开 Anlas（10-10）：自动分配的 Anlas 可用于超规格 / 放大 / 导演工具等付费操作，仍受每日上限约束。"""
    from app.main import GateError
    key = state.db.keys["fixture-1"]
    key.update(allow_anlas=True, anlas_auto=1, daily_v5=1, daily_anlas=100, image_model_scope="all")
    await main.quota_image_check(key, {"anlas": 20, "v5": 0, "images": 1})
    with pytest.raises(GateError) as caught:
        await main.quota_image_check(key, {"anlas": 200, "v5": 0, "images": 1})
    assert caught.value.status == 402 and "Anlas" in caught.value.message


def test_admin_change_description_for_member_dm():
    from app.admin import _describe_changes
    before = {"enabled": 1, "daily_images": 300, "daily_v5": 15, "daily_anlas": 0, "allow_anlas": 0,
              "image_model_scope": "all", "expires_at": 1_000_000.0}
    same_ish = _describe_changes(before, {"enabled": True, "daily_images": 300, "expires_at": 1_000_000.0 + 3600})
    assert same_ish == []
    msgs = _describe_changes(before, {"enabled": False, "daily_images": 150, "image_model_scope": "legacy",
                                      "expires_at": None})
    assert any("暂停" in m for m in msgs) and any("300 → 150" in m for m in msgs)
    assert any("仅 V4.5" in m for m in msgs) and any("长期有效" in m for m in msgs)


def test_resale_signals_alternation_clients_and_all_day():
    from app.key_sources import SourceTracker
    t = SourceTracker(None)
    key = {"id": 5, "is_admin": 0}
    now = 1_000_000.0
    out = []
    for i, label in enumerate(["1.2.*.*", "3.4.*.*", "1.2.*.*", "3.4.*.*"]):
        out = t.signals(key, label, "", now + i * 60)
    assert any(k == "alternate" for k, _ in out)
    t2 = SourceTracker(None)
    assert not any(k == "alternate" for k, _ in [s for i, lb in enumerate(["1.2.*.*", "3.4.*.*", "5.6.*.*"])
                                                    for s in t2.signals(key, lb, "", now + i * 60)])
    for i, ua in enumerate(["A/1", "B/1", "C/1"]):
        out = t.signals(key, None, ua, now + i)
    assert any(k == "clients" for k, _ in out)
    t3 = SourceTracker(None)
    for h in range(20):
        out = t3.signals(key, None, "", now + h * 3600)
    assert any(k == "allday" for k, _ in out)


@pytest.mark.asyncio
async def test_source_signals_reach_autopilot_without_alerter():
    """没配告警渠道（alerter=None）时，网段信号也必须记进 events，否则自动驾驶 key_guard 收不到。"""
    from app.key_sources import SourceTracker
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(str(Path(tmp) / "g.sqlite"))
        await db.connect()
        key = await db.create_key({"name": "m", "token": "nai-x", "daily_images": 10, "monthly_anlas": 0,
                                   "daily_text_tokens": 0, "rpm": 5})
        tracker = SourceTracker(db, None)
        now = 1_800_000_000.0
        for i, ip in enumerate(("1.1.1.1", "2.2.2.2", "1.1.1.9", "2.2.2.9", "1.1.1.7")):
            await tracker.observe(key, ip, now + i * 30)
        assert any(k == "alternate" for _, kid, k in tracker.events if kid == key["id"])
        await db.close()


@pytest.mark.asyncio
async def test_audit_disclosure_covers_full_images_and_zero_means_none():
    """披露要写明原图及其保留天数；原图保留天数 0 = 不保存原图（不能被 `or 3` 吞掉变回 3）。"""
    from types import SimpleNamespace
    from app.audit import audit_disclosure, audit_image_days
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(str(Path(tmp) / "g.sqlite"))
        await db.connect()
        cfg = SimpleNamespace(audit_prompts=True, audit_thumbs=True, audit_retention_days=14)
        assert await audit_image_days(db) == 3                       # 未设置：默认 3
        text = await audit_disclosure(db, cfg)
        assert "原图（3 天后自动删除" in text and "提示词（14 天后自动删除）" in text
        assert "缩略图" not in text and "测试" not in text
        await db.set_setting("audit_image_retention_days", 0)
        assert await audit_image_days(db) == 0
        assert "原图" not in await audit_disclosure(db, cfg)
        await db.close()



def test_stream_tracker_keeps_first_final_image_for_audit():
    """流式出图以前不把图交给生成记录，后台永远是空占位；现在保留第一张完整的最终图。"""
    import base64, io, json
    from PIL import Image
    from app.image_events import ImageEventTracker
    buf = io.BytesIO(); Image.new("RGB", (8, 8), (200, 10, 10)).save(buf, "PNG"); png = buf.getvalue()
    t = ImageEventTracker(1)
    ev = json.dumps({"event_type": "final", "samp_ix": 0, "image": base64.b64encode(png).decode()})
    t.feed(f"event: final\ndata: {ev}\n\n".encode())
    assert t.completed_images == 1 and t.first_image == png


@pytest.mark.asyncio
async def test_admin_thumb_is_generated_from_original(tmp_path):
    """不再另存缩略图：画廊小图从原图现场缩小。"""
    import io
    from PIL import Image
    from app.audit import make_thumbnail
    buf = io.BytesIO(); Image.new("RGB", (832, 1216), (10, 120, 200)).save(buf, "PNG")
    thumb = make_thumbnail(buf.getvalue())
    assert thumb and thumb[:2] == b"\xff\xd8"                    # JPEG
    assert max(Image.open(io.BytesIO(thumb)).size) <= __import__("app.audit", fromlist=["THUMB_SIDE"]).THUMB_SIDE


@pytest.mark.asyncio
async def test_upstream_4xx_is_logged_once_not_also_as_rejected(state, monkeypatch):
    """上游 4xx：子任务里已记一行 error，外层异常处理器不能再补一行 rejected（以前会，多出的 rejected 推高 Key 限流计数）。"""
    async def bad_request(*args, **kwargs):
        from app import request_timing
        request_timing.mark_sent()
        request_timing.mark_status(400)
        return httpx.Response(400, text='{"message":"bad params"}')
    monkeypatch.setattr(state.nai, "request", bad_request)
    before = len(state.db.logs)
    r = await post("/ai/generate-image", image_body())
    assert r.status_code == 400
    await asyncio.sleep(0)
    rows = [a[4] for a, kw in state.db.logs[before:]]
    assert rows.count("rejected") == 0 and rows.count("error") == 1, rows


@pytest.mark.asyncio
async def test_inflight_limit_normalizes_authorization_spelling():
    # 审查 P2：「Bearer K」「bearer K」「Bearer  K」是同一把 Key，不能各算一份并发
    gate = asyncio.Event()

    async def slow(request):
        await gate.wait()
        return httpx.Response(200)

    wrapped = main.limit_inflight(slow)

    def req(h):
        return type("R", (), {"headers": {"authorization": h}})()
    spellings = ["Bearer K", "bearer K", "Bearer  K", "BEARER K"]
    tasks = [asyncio.create_task(wrapped(req(h))) for h in spellings]
    await asyncio.sleep(0)
    with pytest.raises(main.GateError):
        await wrapped(req("Bearer K "))
    gate.set()
    await asyncio.gather(*tasks)
    assert not main._INFLIGHT


def test_flags_fail_closed_on_unknown_spelling():
    from app import site_flags as sf
    assert sf.parse(sf.WEB_LOGIN_PAUSED, "TRUE", True) is True
    assert sf.parse(sf.WEB_LOGIN_PAUSED, "enabled?", True) is True      # 认不出 → 回到默认（暂停）
    assert sf.parse(sf.WEB_LOGIN_PAUSED, "0", True) is False
    assert sf.parse(sf.DM_ENABLED, "Yes", False) is True


def test_release_image_by_entry_keeps_the_running_one():
    from app.guard import Guard
    g = Guard()
    assert g.admit_image(1, accounts=1) is None
    a = g.entries[-1]["id"]
    g.mark_running(1, a)
    g.values["key_image_queue"] = 1
    assert g.admit_image(1, accounts=1) is None
    b = g.entries[-1]["id"]
    g.release_image(1, b)                       # 排队的 B 被取消
    assert [(e["id"], e["running"]) for e in g.entries] == [(a, True)]


def test_injected_browser_scripts_are_not_bugs():
    # 荣耀自带浏览器注入的脚本报错（堆栈全是 <anonymous>）不进 Bug 追踪；本站自己的报错照常上报
    honor = "TypeError: Cannot read properties of undefined (reading 'trim')\n    at is_mark_able_element (<anonymous>:1:19339)\n    at hit_test (<anonymous>:1:13070)"
    assert main._injected_script_error("Uncaught TypeError", honor)
    assert main._injected_script_error("Script error.", "")
    ours = "TypeError: x is undefined\n    at renderMe (https://gate.davidzhao.top/:812:9)"
    assert not main._injected_script_error("Uncaught TypeError", ours, "gate.davidzhao.top")
    assert not main._injected_script_error("Uncaught TypeError", "")       # 没堆栈：保留，宁可多看一条


def test_upstream_parameter_problem_rejects_more_than_six_characters():
    # 2026-10-10 随风飞扬：7 个角色，上游直接 500；6 个以内照常放行，V5 不受这条限制
    def body(n, model="nai-diffusion-4-5-full"):
        caps = [{"char_caption": f"c{i}", "centers": [{"x": 0.5, "y": 0.5}]} for i in range(n)]
        b = image_body(v4_prompt={"caption": {"base_caption": "x", "char_captions": caps}},
                       characterPrompts=[{"prompt": f"c{i}"} for i in range(n)])
        b["model"] = model
        return b
    assert "最多 6 个" in (policy.upstream_parameter_problem(body(7)) or "")
    assert policy.upstream_parameter_problem(body(6)) is None
    assert policy.upstream_parameter_problem(body(7, "nai-diffusion-5-full")) is None
