"""Tests for Vibe PVP daily Anlas quota, feature check, and safe clamping."""
import asyncio
import base64
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app import main, site_flags
from app.database import Database
from test_nai_integration import PNG as PNG_BYTES


PNG = base64.b64encode(PNG_BYTES).decode()


def encoding_body():
    return dict(image=PNG, model="nai-diffusion-4-5-full", informationExtracted=0.7)


def image_body(**parameters):
    params = dict(width=1024, height=1024, steps=28, n_samples=1,
                  sm=False, sm_dyn=False,
                  v4_prompt={"caption": {"base_caption": "fixture", "char_captions": []}, "use_coords": False, "use_order": True},
                  v4_negative_prompt={"caption": {"base_caption": "", "char_captions": []}, "legacy_uc": False})
    params.update(parameters)
    return dict(input="fixture", model="nai-diffusion-4-5-full",
                action="generate", parameters=params)


class FakeNai:
    image_host = "https://fixture.invalid"

    def __init__(self):
        self.calls = []
        self.status = 200
        self.content = b"fake-vibe-binary-encoding"
        self.content_type = "application/octet-stream"

    async def request(self, method, url, body=None, **kwargs):
        self.calls.append((method, url, body, kwargs))
        return SimpleNamespace(status_code=self.status, content=self.content,
                               headers={"content-type": self.content_type})


async def async_true(*_):
    return True


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    db = Database(":memory:")
    await db.connect()
    monkeypatch.setattr(main.SETTINGS, "data_dir", tmp_path)
    monkeypatch.setattr(main.SETTINGS, "global_daily_anlas", 10)  # 10 Anlas/day = 5 encodes
    monkeypatch.setattr(main.SETTINGS, "site_url", "https://gate.example.top/", raising=False)
    fake_nai = FakeNai()
    from app.state import GateState
    state = GateState(main.SETTINGS)
    state.db = db
    state.nai = fake_nai
    state.guard = None
    state.share = None
    state.upstream_health = lambda: {"status": "ok", "recent": 0, "failed": 0, "image_cooldown_seconds": 0}
    monkeypatch.setattr(main, "STATE", state)

    # Standard member key: features="image", allow_anlas=0, daily_images=100
    key = await db.create_key(dict(
        name="member",
        token="nai-test-standard-key-12345",
        daily_images=100,
        daily_anlas=0,
        monthly_anlas=0,
        daily_v5=0,
        allow_anlas=0,
        allow_img2img=0,
        daily_text_tokens=0,
        rpm=10,
        features="image",
    ))

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="https://gate.example.top")
    yield SimpleNamespace(client=client, db=db, key=key, nai=fake_nai, state=state)
    await client.aclose()
    await db.close()


@pytest.mark.asyncio
async def test_standard_key_can_encode_vibe_under_daily_cap(env):
    """普通成员（无 allow_anlas）也能使用 Vibe 编码，扣费计入全站池。"""
    headers = {"Authorization": "Bearer nai-test-standard-key-12345"}
    resp = await env.client.post("/ai/encode-vibe", json=encoding_body(), headers=headers)
    assert resp.status_code == 200
    assert resp.content == b"fake-vibe-binary-encoding"

    # 消耗了 2 Anlas
    day = env.state.day()
    c = await env.db.get_counter(env.key["id"], day)
    assert c["anlas"] == 2.0
    assert await env.db.day_anlas_total(day) == 2.0


@pytest.mark.asyncio
async def test_vibe_encode_blocked_when_daily_cap_exhausted(env):
    """全站每日 Anlas 限额耗尽时，返回 402 且明确说明全站今日 Vibe 额度已抢完。"""
    # 把限额设为 2 Anlas
    await site_flags.put(env.db, site_flags.GLOBAL_DAILY_ANLAS, 2)

    headers = {"Authorization": "Bearer nai-test-standard-key-12345"}
    # 第一次：消耗 2 Anlas，成功
    resp1 = await env.client.post("/ai/encode-vibe", json=encoding_body(), headers=headers)
    assert resp1.status_code == 200

    # 第二次：超过 2 Anlas 限额，拦截 402
    resp2 = await env.client.post("/ai/encode-vibe", json=encoding_body(), headers=headers)
    assert resp2.status_code == 402
    data = resp2.json()
    assert "全站今日 Vibe 额度已抢完" in data["message"]


@pytest.mark.asyncio
async def test_in_flight_reservation_blocks_concurrent_overflow(env):
    """进行中的预留在 image_budget_lock 下也能及时阻断超额请求。"""
    # 限额 2 Anlas
    await site_flags.put(env.db, site_flags.GLOBAL_DAILY_ANLAS, 2)

    # 模拟有一个进行中的预留占用 2 Anlas
    res = main.ImageReservation(env.key, {"anlas": 2, "v5": 0}, legacy=0)
    env.state.image_reservations[id(res)] = res

    headers = {"Authorization": "Bearer nai-test-standard-key-12345"}
    resp = await env.client.post("/ai/encode-vibe", json=encoding_body(), headers=headers)
    assert resp.status_code == 402
    assert "全站今日 Vibe 额度已抢完" in resp.json()["message"]


@pytest.mark.asyncio
async def test_non_vibe_anlas_operations_remain_blocked(env):
    """P0 防护：Vibe PVP 开放绝不会导致其他付费功能（upscale、augment、img2img）被普通 Key 偷跑。"""
    headers = {"Authorization": "Bearer nai-test-standard-key-12345"}

    # upscale 被拒
    resp_up = await env.client.post("/ai/upscale", json={"image": PNG, "width": 2048, "height": 2048}, headers=headers)
    assert resp_up.status_code == 403

    # augment 被拒
    resp_aug = await env.client.post("/ai/augment-image", json={"image": PNG, "req_type": "line_art"}, headers=headers)
    assert resp_aug.status_code == 403

    # img2img 被拒
    resp_img2img = await env.client.post(
        "/ai/generate-image",
        json=image_body(image=PNG),
        headers=headers,
    )
    assert resp_img2img.status_code == 400
    assert "不支持图生图" in resp_img2img.json()["message"]


@pytest.mark.asyncio
async def test_safe_clamp_remains_active_for_standard_key(env):
    """P0 防护：safe_clamp 依然有效，超规格尺寸与步数被自动钳制，绝不产生高额 Anlas。"""
    headers = {"Authorization": "Bearer nai-test-standard-key-12345"}
    env.nai.content = PNG_BYTES
    env.nai.content_type = "image/png"

    # 用户传 2048x2048, 50 步
    resp = await env.client.post(
        "/ai/generate-image",
        json=image_body(width=2048, height=2048, steps=50),
        headers=headers,
    )
    assert resp.status_code == 200
    # 检查发送到上游的参数已被钳制为 1024x1024 / 28 步
    upstream_body = env.nai.calls[-1][2]
    assert upstream_body["parameters"]["width"] <= 1024
    assert upstream_body["parameters"]["height"] <= 1024
    assert upstream_body["parameters"]["steps"] <= 28


@pytest.mark.asyncio
async def test_vibe_in_generate_image_allowed(env):
    """生图时携带 Vibe 引用参数不再被 require_feature 拦截。"""
    headers = {"Authorization": "Bearer nai-test-standard-key-12345"}
    env.nai.content = PNG_BYTES
    env.nai.content_type = "image/png"

    # 携带 1 个 cached vibe (不超过 4 个在 Opus 是免费的；V4.5 需要的是 Vibe 二进制编码而非原图 PNG)
    vibe_data = base64.b64encode(b"vibe_encoded_binary_vector_data_not_png").decode()
    cached_vibe = [{
        "cache_secret_key": "12345678-1234-4123-8123-123456789abc",
        "data": vibe_data,
    }]
    body = image_body(
        reference_image_multiple_cached=cached_vibe,
        reference_strength_multiple=[0.6],
        reference_information_extracted_multiple=[0.7],
    )
    resp = await env.client.post("/ai/generate-image", json=body, headers=headers)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_admin_can_globally_close_vibe(env):
    """站长若在全局功能开关中关闭 vibe，立刻拒绝。"""
    from app import ops
    await ops.set_global_features(env.db, {"vibe": False})

    headers = {"Authorization": "Bearer nai-test-standard-key-12345"}
    resp = await env.client.post("/ai/encode-vibe", json=encoding_body(), headers=headers)
    assert resp.status_code == 403
    assert "站长暂未开放此功能：Vibe 编码" in resp.json()["message"]


@pytest.mark.asyncio
async def test_vibe_pvp_expires_after_until_timestamp(env, monkeypatch):
    """当设置了 GLOBAL_DAILY_ANLAS_UNTIL 且当前时间超过截止时间时，自动关闭 Vibe PVP。"""
    import time
    now = time.time()
    # 截止时间设在过去 10 秒
    await site_flags.put(env.db, site_flags.GLOBAL_DAILY_ANLAS, 1000)
    await site_flags.put(env.db, site_flags.GLOBAL_DAILY_ANLAS_UNTIL, now - 10)

    headers = {"Authorization": "Bearer nai-test-standard-key-12345"}
    resp = await env.client.post("/ai/encode-vibe", json=encoding_body(), headers=headers)
    assert resp.status_code == 403
    assert "Vibe 编码会消耗 Anlas" in resp.json()["message"]

    # 截止时间设在未来 1 小时，正常放行
    await site_flags.put(env.db, site_flags.GLOBAL_DAILY_ANLAS_UNTIL, now + 3600)
    resp_ok = await env.client.post("/ai/encode-vibe", json=encoding_body(), headers=headers)
    assert resp_ok.status_code == 200
