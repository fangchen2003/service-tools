"""Tests for anti-ban upstream protection mechanisms:
- Realistic browser headers (User-Agent, Origin, Referer, Sec-CH-*, Sec-Fetch-*)
- Post-request human-like delay and jitter
- 403 circuit breaker and consecutive failure protection
- Pre-flight parameter sanitization (-1 seed normalization, scale bounds, sampler validation)
- Upstream proxy configuration propagation
- Strict single image slot enforcement
"""
import asyncio
import time
from unittest.mock import MagicMock
import httpx
import pytest

from app.config import Settings
from app.nai import NaiClient, UpstreamError, default_browser_headers
from app.policy import normalize_image_request, upstream_parameter_problem
from app.state import GateState
from test_generation_integration import FakeDB


class MockDB(FakeDB):
    def __init__(self):
        super().__init__()
        self.concurrency = {}

    async def bump_upstream_image_counter(self, token_id, day, count, weight=1.0):
        pass

    async def set_upstream_token_image_concurrency(self, token_id, limit):
        self.concurrency[token_id] = limit


@pytest.fixture
def fake_db():
    return MockDB()


def test_default_browser_headers_authenticity():
    headers = default_browser_headers(token="test-token-123", accept="application/json")
    assert headers["Authorization"] == "Bearer test-token-123"
    assert "nai-gate" not in headers["User-Agent"]
    assert "Mozilla/5.0" in headers["User-Agent"]
    assert headers["Origin"] == "https://novelai.net"
    assert headers["Referer"] == "https://novelai.net/"
    assert headers["Sec-Fetch-Site"] == "same-site"
    assert headers["Sec-Fetch-Mode"] == "cors"
    assert headers["Sec-Fetch-Dest"] == "empty"
    assert "Sec-Ch-Ua" in headers
    assert "Sec-Ch-Ua-Platform" in headers


def test_custom_user_agent_override(fake_db):
    custom_ua = "Mozilla/5.0 (CustomBrowser/1.0; SpecialProfile) NovelAIClient"
    client = NaiClient(
        tokens=["token-1"], image_host="https://image.novelai.net", text_host="https://text.novelai.net",
        legacy_text_host="https://api.novelai.net", db=fake_db, day_fn=lambda: "2026-10-10",
        v5_daily_limits=[100], allow_anlas=[True], custom_user_agent=custom_ua,
    )
    headers = client._headers(client.pool[0])
    assert headers["User-Agent"] == custom_ua
    assert headers["Origin"] == "https://novelai.net"


@pytest.mark.asyncio
async def test_upstream_client_sends_realistic_headers(fake_db):
    captured_requests = []

    async def mock_handler(request: httpx.Request):
        captured_requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client = NaiClient(
        tokens=["token-1"], image_host="https://image.novelai.net", text_host="https://text.novelai.net",
        legacy_text_host="https://api.novelai.net", db=fake_db, day_fn=lambda: "2026-10-10",
        v5_daily_limits=[100], allow_anlas=[True],
    )
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    try:
        resp = await client.request("POST", "https://image.novelai.net/ai/test", json_body={"input": "test"})
        assert resp.status_code == 200
        assert len(captured_requests) == 1
        req = captured_requests[0]
        assert "nai-gate" not in req.headers.get("user-agent", "")
        assert "Mozilla/5.0" in req.headers.get("user-agent", "")
        assert req.headers.get("origin") == "https://novelai.net"
        assert req.headers.get("referer") == "https://novelai.net/"
        assert req.headers.get("sec-fetch-site") == "same-site"
    finally:
        await client._client.aclose()


@pytest.mark.asyncio
async def test_human_like_post_request_delay(fake_db):
    client = NaiClient(
        tokens=["token-1"], image_host="https://image.novelai.net", text_host="https://text.novelai.net",
        legacy_text_host="https://api.novelai.net", db=fake_db, day_fn=lambda: "2026-10-10",
        v5_daily_limits=[100], allow_anlas=[True],
        image_min_interval=15.0, post_jitter_min=1.0, post_jitter_max=2.0,
    )
    ts = client.pool[0]
    ts.image_next_at = time.monotonic() - 10  # expired in the past
    before = time.monotonic()
    await client._settle(ts, succeeded=True, v5_free=False, image_count=1, send_started=True)
    # image_next_at should be forced to be at least now + 1.0s
    assert ts.image_next_at >= before + 0.95


@pytest.mark.asyncio
async def test_consecutive_403_breaker(fake_db):
    events = []
    client = NaiClient(
        tokens=["token-1"], image_host="https://image.novelai.net", text_host="https://text.novelai.net",
        legacy_text_host="https://api.novelai.net", db=fake_db, day_fn=lambda: "2026-10-10",
        v5_daily_limits=[100], allow_anlas=[True],
    )
    client.on_event = lambda kind, msg, cooldown=900: events.append(kind)
    ts = client.pool[0]
    assert ts.usable and not ts.disabled

    # First and second 403: record fails, not disabled yet
    client.mark_forbidden(ts)
    assert ts.fails == 1 and not ts.disabled
    client.mark_forbidden(ts)
    assert ts.fails == 2 and not ts.disabled

    # Third consecutive 403: cools down for 5 minutes, never permanently disables
    client.mark_forbidden(ts)
    assert ts.fails == 3
    assert ts.disabled is False
    assert not ts.usable
    assert 250 < ts.blocked_until - __import__("time").time() <= 300
    assert "upstream_403_cooldown" in events
    ts.blocked_until = 0
    client.mark_ok(ts)
    assert ts.usable and ts.fails == 0


def test_parameter_preflight_seed_normalization():
    # Negative seed from SD WebUI (-1) should be normalized to uint32
    body = {
        "model": "nai-diffusion-4-5",
        "parameters": {
            "seed": -1,
            "width": 1024,
            "height": 1024,
        }
    }
    normalize_image_request(body)
    assert isinstance(body["parameters"]["seed"], int)
    assert 0 <= body["parameters"]["seed"] <= 4294967295

    # Overflowing seed normalized to uint32
    body["parameters"]["seed"] = 999999999999
    normalize_image_request(body)
    assert 0 <= body["parameters"]["seed"] <= 4294967295


def test_parameter_preflight_scale_bounds():
    # Invalid scale values should be rejected before upstream
    for invalid_scale in (-1, 51, float("inf"), float("nan"), "invalid"):
        body = {
            "model": "nai-diffusion-4-5",
            "parameters": {
                "scale": invalid_scale,
                "width": 1024,
                "height": 1024,
            }
        }
        with pytest.raises(ValueError) as exc:
            normalize_image_request(body)
        assert "scale" in str(exc.value)

    # Valid scale passes
    body = {
        "model": "nai-diffusion-4-5",
        "parameters": {
            "scale": 7.5,
            "width": 1024,
            "height": 1024,
        }
    }
    normalize_image_request(body)
    assert body["parameters"]["scale"] == 7.5


def test_parameter_preflight_sampler_validation():
    # Dangerous or non-ascii sampler injection blocked
    body = {
        "model": "nai-diffusion-4-5",
        "parameters": {
            "sampler": "k_euler\r\nInjected: header",
        }
    }
    assert "采样器名称无效" in upstream_parameter_problem(body)

    # Valid sampler passes
    body["parameters"]["sampler"] = "k_euler_ancestral"
    assert upstream_parameter_problem(body) is None


def test_parameter_preflight_bracket_depth_not_blocked_but_huge_weight_is():
    # 括号层数不拦：NAI 花括号每层只 ×1.05（8 层≈1.48 倍），圆括号不是权重语法；10/11 曾误拦成员 V5 请求
    for deep in ["1girl, {{{{{{{{masterpiece}}}}}}}}, best quality",
                 "1girl, { { { { { { { { masterpiece } } } } } } } }",
                 "1girl, ((((((((masterpiece))))))))",
                 "1girl, [[[[[[[[artist:xxx]]]]]]]], best quality"]:
        body = {"model": "nai-diffusion-4-5", "input": deep, "parameters": {}}
        assert upstream_parameter_problem(body) is None, deep

    # 超大数值权重（画师名里的数字紧挨 ::）会让上游 NaN：拦
    body = {"model": "nai-diffusion-4-5", "input": "artist:bm94199::, 1girl, best quality", "parameters": {}}
    prob = upstream_parameter_problem(body)
    assert prob is not None and "检测到异常权重 94199::" in prob
    for valid_weight in ["1.2::test_tag::, 1girl", "-3::test_tag::, 1girl", "30::test_tag::, 1girl", "artist_94199, ::, 1girl"]:
        body["input"] = valid_weight
        assert upstream_parameter_problem(body) is None, valid_weight

    # 角色提示词也参与检查
    body = {"model": "nai-diffusion-4-5", "input": "1girl",
            "parameters": {"characterPrompts": [{"prompt": "char1", "uc": "lowres, char_tag94199::"}]}}
    prob = upstream_parameter_problem(body)
    assert prob is not None and "检测到异常权重 94199::" in prob
    body["parameters"]["characterPrompts"] = [{"prompt": "a" * 52000, "uc": "lowres"}]
    assert "提示词过长" in upstream_parameter_problem(body)
    body["parameters"]["characterPrompts"] = [{"prompt": "char1, {{{{{{{{blue hair}}}}}}}}", "uc": None}, "not-a-dict"]
    assert upstream_parameter_problem(body) is None


@pytest.mark.asyncio
async def test_strict_single_slot_enforced(fake_db):
    client = NaiClient(
        tokens=["token-1"], image_host="https://image.novelai.net", text_host="https://text.novelai.net",
        legacy_text_host="https://api.novelai.net", db=fake_db, day_fn=lambda: "2026-10-10",
        v5_daily_limits=[100], allow_anlas=[True], single_slot_enforced=True,
    )
    ts = client.pool[0]
    assert ts.image_slots.limit == 1
    # Attempting to set concurrency higher than 1 must be clamped to 1
    await client.set_image_concurrency(ts.token_id, 3)
    assert ts.image_slots.limit == 1


def test_proxy_and_http2_settings_propagation(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        admin_password="test",
        nai_tokens=["fixture-token"],
        upstream_proxy="http://127.0.0.1:10808",
        upstream_user_agent="CustomAntiBanAgent/2.0",
        upstream_http2=True,
        post_request_jitter_min=1.5,
        post_request_jitter_max=3.5,
        single_image_slot_enforced=True,
    )
    state = GateState(settings)
    assert state.nai._proxy == "http://127.0.0.1:10808"
    assert state.nai._custom_user_agent == "CustomAntiBanAgent/2.0"
    assert state.nai._http2 is True
    assert state.nai._post_jitter_min == 1.5
    assert state.nai._post_jitter_max == 3.5
    assert state.nai._single_slot_enforced is True
    assert state.nai.pool[0].browser_profile["user_agent"] == "CustomAntiBanAgent/2.0"


def test_weight_regex_is_linear_on_long_digit_runs():
    # 10/11 审查 P0：5 万位纯数字曾让预检卡 23 秒、阻塞整个事件循环
    import time as _t
    body = {"model": "nai-diffusion-4-5", "input": "1" * 51000, "parameters": {}}
    t0 = _t.perf_counter()
    assert upstream_parameter_problem(body) is None
    assert _t.perf_counter() - t0 < 0.5


@pytest.mark.asyncio
async def test_403_cooldown_backs_off_and_resets(fake_db):
    import time as _t
    client = NaiClient(
        tokens=["token-1"], image_host="https://image.novelai.net", text_host="https://text.novelai.net",
        legacy_text_host="https://api.novelai.net", db=fake_db, day_fn=lambda: "2026-10-11",
        v5_daily_limits=[100], allow_anlas=[True],
    )
    client.on_event = lambda *a, **k: None
    ts = client.pool[0]
    client.mark_rate_limited(ts, 1); client.mark_rate_limited(ts, 1)
    client.mark_forbidden(ts)                       # 两次 429 + 一次 403 不触发 403 冷却
    assert ts.forbidden_trips == 0
    ts.blocked_until = 0
    waits = []
    for _ in range(4):
        for _ in range(3):
            client.mark_forbidden(ts)
        waits.append(round((ts.blocked_until - _t.time()) / 60))
        ts.blocked_until = 0
    assert waits == [5, 15, 45, 60]
    client.mark_ok(ts)
    assert ts.forbidden_trips == 0 and ts.forbidden_streak == 0


def test_name_digit_weight_is_auto_fixed_not_rejected():
    from app.policy import fix_name_digit_weights
    body = {"model": "nai-diffusion-4-5", "input": "1.2::artist:bm94199::, 1girl",
            "parameters": {"negative_prompt": "lowres, tag2024::", "characterPrompts": [{"prompt": "x99::", "uc": ""}],
                           "v4_prompt": {"caption": {"base_caption": "a bm94199::", "char_captions": [{"char_caption": "c7::"}]}}}}
    assert fix_name_digit_weights(body) == 5
    assert body["input"] == "1.2::artist:bm94199 ::, 1girl"
    assert body["parameters"]["negative_prompt"] == "lowres, tag2024 ::"
    assert upstream_parameter_problem(body) is None
    # 独立数字权重不动：正常的照常放行，超大的照常拦
    plain = {"model": "nai-diffusion-4-5", "input": "1.2::tag::, -3::x::, 94199::y::", "parameters": {}}
    assert fix_name_digit_weights(plain) == 0
    assert "94199::" in upstream_parameter_problem(plain)


def test_sanitize_and_translate_prompts():
    from app.policy import clean_prompt_syntax, sanitize_and_translate_prompts

    # 1. 基础语法测试：WebUI 格式转译、孤立 :: 清洗、多余逗号合并
    raw = "(masterpiece:1.2), 1girl, (solo:1.1), ::, blue eyes, ,, (smile:0.9)"
    cleaned = clean_prompt_syntax(raw)
    assert cleaned == "1.2::masterpiece::, 1girl, 1.1::solo::, blue eyes, 0.9::smile::"

    # 2. 完整请求体多字段深层转译
    body = {
        "model": "nai-diffusion-4-5",
        "input": "(masterpiece:1.3), 1girl, ::",
        "parameters": {
            "prompt": "(best quality:1.2), cat ears",
            "negative_prompt": "lowres, (bad hands:1.4), ::",
            "characterPrompts": [
                {"prompt": "(red hair:1.1), ::", "uc": "(ugly:1.2)"}
            ],
            "v4_prompt": {
                "caption": {
                    "base_caption": "(sunset:1.5), beach",
                    "char_captions": [{"char_caption": "(standing:1.0), ::"}]
                }
            }
        }
    }
    n = sanitize_and_translate_prompts(body)
    assert n == 7
    assert body["input"] == "1.3::masterpiece::, 1girl"
    assert body["parameters"]["prompt"] == "1.2::best quality::, cat ears"
    assert body["parameters"]["negative_prompt"] == "lowres, 1.4::bad hands::"
    assert body["parameters"]["characterPrompts"][0]["prompt"] == "1.1::red hair::"
    assert body["parameters"]["characterPrompts"][0]["uc"] == "1.2::ugly::"
    assert body["parameters"]["v4_prompt"]["caption"]["base_caption"] == "1.5::sunset::, beach"
    assert body["parameters"]["v4_prompt"]["caption"]["char_captions"][0]["char_caption"] == "1.0::standing::"

