"""Public HTTP failures must not expose upstream bodies or internal exceptions."""
import asyncio

import httpx
import pytest

from app import main
from test_generation_integration import FakeState, image_body


@pytest.fixture
def state(monkeypatch):
    value = FakeState()
    value.settings.max_text_output_tokens = 200
    value.settings.max_input_chars = 10000
    value.nai.text_host = value.nai.legacy_text_host = "https://fixture.invalid"
    for key in value.db.keys.values():
        key["daily_text_tokens"] = -1
    monkeypatch.setattr(main, "STATE", value)
    return value


async def post(path, body):
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://fixture.invalid") as client:
        return await client.post(path, json=body, headers={"Authorization": "Bearer fixture-1"})


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", [
    ("/ai/generate-image", image_body()),
    ("/ai/generate-image/suggest-tags", {"prompt": "fixture"}),
    ("/ai/generate", {"model": "kayra-v1", "input": "fixture", "parameters": {}}),
    ("/ai/generate-voice", {"text": "fixture"}),
])
@pytest.mark.parametrize("status,content_type,content", [
    (403, "application/json", b'{"message":"fixture-private-account"}'),
    (502, "text/html", b'<html>fixture-private-proxy</html>'),
])
async def test_http_errors_keep_status_without_upstream_body(state, path, body, status, content_type, content):
    state.nai.status, state.nai.content_type, state.nai.content = status, content_type, content
    response = await post(path, body)
    await asyncio.sleep(0)
    assert len(state.nai.calls) == 1
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/json")
    assert str(status) in response.json()["error"]["message"]
    assert "fixture-private" not in response.text
    assert not state.db.charges


@pytest.mark.asyncio
async def test_unexpected_exception_returns_safe_json(state, monkeypatch):
    async def broken(*args):
        raise RuntimeError("fixture-private-database-path")
    monkeypatch.setattr(state.db, "get_key_by_token", broken)
    response = await post("/ai/generate-image", image_body())
    assert response.status_code == 500
    assert response.json()["error"]["message"] == "服务器内部错误，请联系站长"
    assert "fixture-private" not in response.text and not state.nai.calls
