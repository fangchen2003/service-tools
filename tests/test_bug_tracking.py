"""Bug 追踪：每一种意外错误都要留下记录、能按请求编号定位，并且不向成员泄露内部信息。"""
import asyncio

import httpx
import pytest

from app import main
from app.database import Database
from app.errors import Tracker, signature
from test_generation_integration import FakeState, image_body


def _boom(where):
    try:
        if where == "a":
            raise ValueError("first 123")
        raise ValueError("second 456")
    except ValueError as exc:
        return exc


@pytest.mark.asyncio
async def test_same_bug_is_grouped_and_regression_notifies(tmp_path):
    db = Database(str(tmp_path / "g.sqlite"))
    await db.connect()
    sent = []
    tracker = Tracker(db, notify=lambda kind, msg, cooldown: sent.append(msg))
    exc = _boom("a")
    sig = tracker.capture("request", exc, path="/ai/generate-image", rid="abcd1234")
    tracker.capture("request", exc, path="/ai/generate-image", rid="abcd5678")
    await tracker.drain()
    rows = await tracker.list()
    assert len(rows) == 1 and rows[0]["count"] == 2 and rows[0]["last_rid"] == "abcd5678"
    assert "Traceback" in rows[0]["detail"] and len(sent) == 1 and "新 bug" in sent[0]
    assert await tracker.resolve(sig) and not await tracker.list()
    tracker.capture("request", exc)
    await tracker.drain()
    assert len(sent) == 2 and "复发" in sent[1] and (await tracker.list())[0]["count"] == 3
    # 只有数字不同的消息归为同一个；警告级别不私信
    assert signature("upstream", title="502 timeout after 30s") == signature("upstream", title="502 timeout after 61s")
    tracker.capture("disconnect", title="client gone", level="warn")
    await tracker.drain()
    assert len(sent) == 2
    await db.close()


def test_tracker_never_raises_without_loop_or_db():
    tracker = Tracker(None)
    assert tracker.capture("request", _boom("a"))
    assert tracker.recent[-1]["source"] == "request"


@pytest.fixture
def state(monkeypatch, tmp_path):
    value = FakeState()
    captured = []

    class Bugs:
        def capture(self, source, exc=None, **kw):
            captured.append((source, exc, kw))
            return "sig"
    value.bugs = Bugs()
    value.captured = captured
    monkeypatch.setattr(main, "STATE", value)
    return value


async def _client():
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://fixture.invalid")


@pytest.mark.asyncio
async def test_unexpected_500_has_request_id_and_is_captured(state, monkeypatch):
    async def broken(*args):
        raise RuntimeError("fixture-private-database-path")
    monkeypatch.setattr(state.db, "get_key_by_token", broken)
    async with await _client() as client:
        response = await client.post("/ai/generate-image", json=image_body(), headers={"Authorization": "Bearer fixture-1"})
    assert response.status_code == 500
    rid = response.json()["error"]["request_id"]
    assert len(rid) == 8 and response.headers["x-request-id"] == rid and rid in response.json()["error"]["message"]
    assert "fixture-private" not in response.text
    source, exc, kw = state.captured[-1]
    assert source == "request" and isinstance(exc, RuntimeError) and kw["rid"] == rid


@pytest.mark.asyncio
async def test_every_response_carries_request_id(state):
    async with await _client() as client:
        ok = await client.get("/healthz")
        bad = await client.post("/ai/generate-image", json=image_body(), headers={"Authorization": "Bearer nope"})
    assert len(ok.headers["x-request-id"]) == 8
    assert bad.status_code == 401 and bad.json()["error"]["request_id"] == bad.headers["x-request-id"]


@pytest.mark.asyncio
async def test_client_error_report_is_bounded(state):
    main._CLIENT_ERR.update(window=0.0, total=0, ip={})
    async with await _client() as client:
        for i in range(15):
            r = await client.post("/public/client-error", content=b'{"page":"landing","msg":"TypeError: x is undefined","line":3}')
            assert r.status_code == 204
        await client.post("/public/client-error", content=b"not json")
    web = [c for c in state.captured if c[0] == "web:landing"]
    assert len(web) == 10 and web[0][2]["level"] == "warn" and web[0][2]["title"].startswith("TypeError")


@pytest.mark.asyncio
async def test_maintenance_step_failure_does_not_skip_later_steps(state, monkeypatch):
    calls = []

    async def broken_perf():
        calls.append("perf")
        raise RuntimeError("perf down")

    async def stop(_):
        raise asyncio.CancelledError
    state.settings.data_dir = "."
    monkeypatch.setattr(main, "check_upstream_perf", broken_perf)
    monkeypatch.setattr(main.asyncio, "sleep", stop)
    monkeypatch.setattr(main.shutil, "disk_usage", lambda _p: calls.append("disk") or type("U", (), {"free": 9, "total": 10})())
    with pytest.raises(asyncio.CancelledError):
        await main.maintenance_loop()
    assert calls == ["perf", "disk"]
    assert any(c[0] == "maintenance:perf" for c in state.captured)


@pytest.mark.asyncio
async def test_member_errors_carry_site_prefix(state):
    async with await _client() as client:
        bad = await client.post("/ai/generate-image", json=image_body(), headers={"Authorization": "Bearer nope"})
    assert bad.json()["error"]["message"].startswith("猫头鹰公益站提醒：")
