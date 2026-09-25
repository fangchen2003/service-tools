"""Manual comparisons against fake official HTTP, using the real SQLite ledger."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import aiosqlite
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import main
from app.admin import router
from app.config import Settings
from app.nai import TokenState
from app.reconciliation import COOLDOWN_SETTING, ManualReconciliation, ReconciliationError
from app.state import GateState
from test_generation_integration import image_body, post
from test_nai_integration import PNG


def subscription(fixed=1000, purchased=100):
    return {"active": True, "expiresAt": 1900000000,
            "trainingStepsLeft": {"fixedTrainingStepsLeft": fixed, "purchasedTrainingSteps": purchased}}


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    st = GateState(Settings(data_dir=tmp_path, nai_tokens=["fixture-upstream"],
        image_host="https://official.invalid", admin_password="fixture-password",
        secret_key="fixture-secret", admin_cookie_secure=False, image_min_interval=0,
        key_image_min_interval=0))
    await st.db.connect()
    key = await st.db.create_key(dict(name="fixture", token="fixture-1", daily_images=100,
        daily_anlas=1000, daily_v5=100, monthly_anlas=10000, daily_text_tokens=1000,
        rpm=100, allow_anlas=True, allow_img2img=True, image_model_scope="all"))
    e = SimpleNamespace(st=st, rec=st.reconciliation, queries=[], data=subscription(),
                        response=None, entered=asyncio.Event(), release=None, key=key)

    async def handler(request):
        assert request.url.host == "official.invalid"
        if request.method == "POST":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        assert request.url.path == "/user/subscription"
        assert request.headers["authorization"].startswith("Bearer fixture-")
        e.queries.append(request.url.path)
        e.entered.set()
        if e.release:
            await e.release.wait()
        return e.response or httpx.Response(200, content=json.dumps(e.data).encode())

    st.nai._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(main, "STATE", st)
    app = FastAPI()
    app.state.gate = st
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://admin.fixture") as client:
        e.client = client
        yield e
    await st.nai._client.aclose()
    await st.db.close()


async def next_run(e):
    # Test clock boundary only; production exposes no cooldown reset route.
    await e.st.db.set_setting(COOLDOWN_SETTING, 0)
    return (await e.rec.run())["history"][0]


@pytest.mark.asyncio
async def test_balances_compare_persist_without_rebilling_across_months_and_deleted_keys(env):
    db = env.st.db
    await db.bump_counters(env.key["id"], "2026-09-30", anlas=20)
    first = await next_run(env)
    assert first["balance"] == 1100 and first["comparison"] == {"reason": "first"}
    await db.bump_counters(env.key["id"], "2026-10-01", anlas=7)
    await db.add_log(env.key["id"], "fixture", "image", "fixture", "error", unconfirmed_anlas=3)
    await db.reset_daily_image_quota(env.key["id"], "2026-10-01")
    await db.delete_key(env.key["id"])
    before = await db.reconciliation_totals()
    env.data = subscription(990)
    second = await next_run(env)
    assert second["comparison"] == dict(reason=None, since=first["checked_at"],
        balance_decrease=10, recorded=7, difference=3, unconfirmed_anlas=3, unconfirmed_requests=1)
    assert await db.reconciliation_totals() == before
    assert await db.get_key(env.key["id"]) is None
    await db.close()
    await db.connect()
    assert (await env.rec.status())["history"][0] == second
    # A fresh service object reads the persisted cooldown, not a process timer.
    rec = ManualReconciliation(db, env.st.nai, env.st.image_budget_lock)
    with pytest.raises(ReconciliationError) as caught:
        await rec.run()
    assert caught.value.status == 429 and len(env.queries) == 2
    assert "fixture-upstream" not in json.dumps(await rec.status())


@pytest.mark.asyncio
async def test_admin_auth_origin_body_and_read_only_refresh(env):
    client = env.client
    for method in ("GET", "POST"):
        assert (await client.request(method, "/admin/api/reconciliation", json={})).status_code == 401
    await client.post("/admin/api/login", json={"password": "fixture-password"})
    for _ in range(4):
        r = await client.get("/admin/api/reconciliation")
        assert r.status_code == 200 and r.json()["history"] == []
        assert r.headers["cache-control"] == "no-store"
    for origin in ("http://evil.invalid", "http://admin.fixture:9000", "null"):
        assert (await client.post("/admin/api/reconciliation", json={}, headers={"Origin": origin})).status_code == 403
    csrf = r.json()["csrf_token"]
    client.headers["X-NAI-Admin-CSRF"] = csrf
    assert (await client.post("/admin/api/reconciliation", content="{}", headers={"Content-Type": "text/plain"})).status_code == 415
    for body in ([], {"url": "https://untrusted.invalid"}):
        assert (await client.post("/admin/api/reconciliation", json=body)).status_code == 400
    assert (await client.post("/admin/api/reconciliation", json={"big": "a" * 1024})).status_code == 413
    assert not env.queries
    r = await client.post("/admin/api/reconciliation", json={}, headers={"Origin": "http://admin.fixture"})
    assert r.status_code == 200 and not r.json()["running"]
    r = await client.post("/admin/api/reconciliation", json={})
    assert r.status_code == 429 and 0 < int(r.headers["retry-after"]) <= 60
    assert len(env.queries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["http://admin.fixture", "https://admin.fixture:8443"])
async def test_session_csrf_supports_direct_and_https_proxy_without_trusting_headers(env, origin):
    app = FastAPI()
    app.state.gate = env.st
    app.include_router(router)
    # As with a default Uvicorn Docker deployment, bridge peers cannot dictate
    # the ASGI scheme. CSRF validation must not depend on that internal scheme.
    proxy = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy,
            client=("172.19.0.1", 45678)), base_url="http://admin.fixture") as client:
        await client.post("/admin/api/login", json={"password": "fixture-password"})
        status = await client.get("/admin/api/reconciliation")
        csrf = status.json().get("csrf_token", "")
        assert status.headers["cache-control"] == "no-store"
        assert csrf and csrf != client.cookies.get("nai_gate_admin")
        response = await client.post("/admin/api/reconciliation", json={}, headers={
            "Origin": origin, "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "irrelevant.invalid", "X-NAI-Admin-CSRF": csrf})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert len(env.queries) == 1
        assert csrf not in json.dumps(response.json()["history"])


@pytest.mark.asyncio
async def test_csrf_rejects_missing_forged_and_other_session_values_before_query(env):
    client = env.client
    await client.post("/admin/api/login", json={"password": "fixture-password"})
    csrf = (await client.get("/admin/api/reconciliation")).json()["csrf_token"]
    from app.admin import COOKIE, _sign
    # A separately signed valid session must not be able to reuse this token.
    cookie = client.cookies.get(COOKIE)
    expiry = str(int(cookie.split(".")[0]) + 1)
    other_cookie = expiry + "." + _sign(env.st.settings.secret_key, expiry)
    for origin in ("http://admin.fixture", "http://admin.fixture:9000", "https://evil.invalid", "null"):
        for value in ("", "0" * 64):
            response = await client.post("/admin/api/reconciliation", json={}, headers={
                "Origin": origin, "X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.invalid",
                "X-NAI-Admin-CSRF": value})
            assert response.status_code == 403
    response = await client.post("/admin/api/reconciliation", json={}, headers={
        "Cookie": COOKIE + "=" + other_cookie, "X-NAI-Admin-CSRF": csrf})
    assert response.status_code == 403
    assert not env.queries and await env.rec.retry_after() == 0
    assert (await client.get("/admin/api/reconciliation")).json()["history"] == []


@pytest.mark.asyncio
async def test_deduplicate_tokens_but_include_dispatch_disabled_accounts(env):
    env.st.nai.pool += [TokenState("fixture-upstream", 1, 0, True), TokenState("fixture-second", 2, 0, True)]
    env.st.nai.pool[-1].admin_enabled = False
    row = await next_run(env)
    assert row["balance"] == 2200 and len(env.queries) == 2
    env.st.nai.pool.reverse()
    env.st.nai.pool[0].admin_enabled = True
    assert (await next_run(env))["comparison"]["difference"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [("pool", "accounts_changed"), ("cycle", "cycle_changed"),
    ("credit", "balance_increased"), ("ledger", "ledger_reset")])
async def test_discontinuity_starts_new_baseline(env, change, reason):
    await env.st.db.bump_counters(env.key["id"], "2026-09-25", anlas=20)
    await next_run(env)
    if change == "pool":
        env.st.nai.pool.append(TokenState("fixture-new", 1, 0, True))
    elif change == "cycle":
        env.data["expiresAt"] += 86400
    elif change == "credit":
        env.data = subscription(800, 120)  # Total decreased, but one component was credited.
    else:
        await env.st.db._db.execute("UPDATE counters SET anlas=0")
        await env.st.db._db.commit()
    row = await next_run(env)
    assert row["comparison"] == {"reason": reason}


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [httpx.Response(302, headers={"location": "https://untrusted.invalid"}),
    httpx.Response(401, text="fixture-upstream"), httpx.Response(500, text="fixture-upstream"),
    httpx.Response(200, content=b"[" * 70000), httpx.Response(200, content=b"not-json")])
async def test_failure_keeps_previous_and_does_not_retry_or_leak_response(env, response):
    first = await next_run(env)
    env.response = response
    with pytest.raises(ReconciliationError) as caught:
        await next_run(env)
    assert "fixture-upstream" not in str(caught.value)
    assert (await env.rec.status())["history"] == [first]
    assert len(env.queries) == 2 and not env.st.image_budget_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, -1, True, "100", float("nan"), float("inf"), 10**1000])
async def test_invalid_balance_never_advances_baseline(env, value):
    first = await next_run(env)
    env.data["trainingStepsLeft"]["fixedTrainingStepsLeft"] = value
    with pytest.raises(ReconciliationError):
        await next_run(env)
    assert (await env.rec.status())["history"] == [first]


@pytest.mark.asyncio
async def test_partial_pool_failure_and_pool_change_do_not_persist(env):
    first = await next_run(env)
    env.st.nai.pool.append(TokenState("fixture-second", 1, 0, True))
    calls = 0

    async def fail_second(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=subscription()) if calls == 1 else httpx.Response(503)

    await env.st.nai._client.aclose()
    env.st.nai._client = httpx.AsyncClient(transport=httpx.MockTransport(fail_second))
    with pytest.raises(ReconciliationError):
        await next_run(env)
    assert calls == 2 and (await env.rec.status())["history"] == [first]

    async def change_pool(request):
        env.st.nai.pool.clear()
        return httpx.Response(200, json=subscription())

    await env.st.nai._client.aclose()
    env.st.nai._client = httpx.AsyncClient(transport=httpx.MockTransport(change_pool))
    with pytest.raises(ReconciliationError):
        await next_run(env)
    assert (await env.rec.status())["history"] == [first]


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["600", "Fri, 01 Jan 2100 00:00:00 GMT", "invalid"])
async def test_official_rate_limit_is_persisted(env, header):
    env.response = httpx.Response(429, headers={"retry-after": header})
    with pytest.raises(ReconciliationError) as caught:
        await env.rec.run()
    assert caught.value.status == 429 and caught.value.retry_after >= 300
    assert await env.rec.retry_after() >= 299 and not (await env.rec.status())["history"]
    assert len(env.queries) == 1


@pytest.mark.asyncio
async def test_concurrent_clicks_and_cancelled_query_release_owned_locks(env):
    env.release = asyncio.Event()
    task = asyncio.create_task(env.rec.run())
    await asyncio.wait_for(env.entered.wait(), 1)
    with pytest.raises(ReconciliationError) as caught:
        await env.rec.run()
    assert caught.value.status == 409 and len(env.queries) == 1
    assert (await env.rec.status())["running"] and env.st.image_budget_lock.locked()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not env.rec.lock.locked() and not env.st.image_budget_lock.locked()
    assert not (await env.rec.status())["history"] and await env.rec.retry_after() > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_wait_timeout_or_cancel_makes_no_official_request(env, cancel):
    env.rec.wait_timeout = .03
    await env.st.image_budget_lock.acquire()
    task = asyncio.create_task(env.rec.run())
    if cancel:
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(ReconciliationError) as caught:
            await task
        assert caught.value.status == 409
    assert env.st.image_budget_lock.locked() and not env.rec.lock.locked()
    assert not env.queries and await env.rec.retry_after() == 0
    env.st.image_budget_lock.release()


@pytest.mark.asyncio
async def test_real_generation_must_settle_before_balance_read(env, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = env.st.db.bump_counters

    async def settle(*args, **kwargs):
        entered.set()
        await release.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(env.st.db, "bump_counters", settle)
    generation = asyncio.create_task(post("/ai/generate-image", image_body(steps=29)))
    reconcile = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        reconcile = asyncio.create_task(env.rec.run())
        await asyncio.sleep(.02)
        assert not env.queries and env.st.global_active == 1
        release.set()
        assert (await generation).status_code == 200
        row = (await reconcile)["history"][0]
        assert row["ledger"]["anlas"] > 0
        assert env.st.global_active == 0 and not env.st.image_budget_lock.locked()
    finally:
        release.set()
        await asyncio.gather(generation, *([reconcile] if reconcile else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_total_query_deadline_and_history_bound(env):
    for _ in range(22):
        await next_run(env)
    history = (await env.rec.status())["history"]
    assert len(history) == 20 and history[0]["id"] == 22 and history[-1]["id"] == 3
    count = await (await env.st.db._db.execute("SELECT COUNT(*) FROM anlas_reconciliations")).fetchone()
    assert count[0] == 22
    env.release = asyncio.Event()
    env.rec.query_timeout = .01
    with pytest.raises(ReconciliationError):
        await next_run(env)
    assert (await env.rec.status())["history"] == history
    assert not env.rec.lock.locked() and not env.st.image_budget_lock.locked()


@pytest.mark.asyncio
async def test_failed_snapshot_commit_is_invisible_and_cannot_be_committed_by_other_writes(env, monkeypatch):
    first = await next_run(env)
    await env.st.db.set_setting(COOLDOWN_SETTING, 0)
    await env.client.post("/admin/api/login", json={"password": "fixture-password"})
    env.client.headers["X-NAI-Admin-CSRF"] = (
        await env.client.get("/admin/api/reconciliation")).json()["csrf_token"]
    original = aiosqlite.Connection.commit

    async def fail_snapshot_commit(db):
        count = await (await db.execute("SELECT COUNT(*) FROM anlas_reconciliations")).fetchone()
        if db.in_transaction and count[0] > 1:
            raise sqlite3.OperationalError("fixture-only commit failure")
        await original(db)

    with monkeypatch.context() as context:
        context.setattr(aiosqlite.Connection, "commit", fail_snapshot_commit)
        response = await env.client.post("/admin/api/reconciliation", json={})
    assert response.status_code == 503 and "未能保存" in response.json()["detail"]
    assert "fixture-only" not in response.text
    assert await env.st.db.reconciliation_history() == [first]
    assert not env.st.db._db.in_transaction
    # Later writes and restarts must not make the failed snapshot reappear.
    await env.st.db.set_setting("fixture-only-after-failure", True)
    await env.st.db.close()
    await env.st.db.connect()
    assert await env.st.db.reconciliation_history() == [first]
    assert await env.rec.retry_after() > 0
    assert not env.rec.lock.locked() and not env.st.image_budget_lock.locked()
