"""特定上游令牌的免费图和 Anlas 隔离测试。"""

import asyncio
import os
import tempfile

from fastapi import FastAPI
import httpx

from app.admin import router
from app.config import Settings
from app.database import Database
from app.nai import NaiClient
from app.state import GateState


def test_second_upstream_token_free_v5_limit_and_anlas_block():
    async def run():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        db = Database(path)
        try:
            await db.connect()
            client = NaiClient(
                ["first-token", "second-token"], "https://image.example", "https://text.example",
                "https://legacy.example", db=db, day_fn=lambda: "2026-09-11",
                v5_daily_limits=[0, 150], allow_anlas=[True, False],
            )
            second = client.pool[1]

            # 第二把令牌绝不会承接需要 Anlas 的图片请求。
            assert (await client.pick_token(requires_anlas=True)) is client.pool[0]

            # 达到第二把的免费 V5 日限后，免费 V5 自动改由第一把承接。
            for _ in range(150):
                await db.bump_upstream_v5_counter(second.token_id, "2026-09-11")
            client._rr = 0  # 下一次轮询优先第二把，验证它确实被跳过。
            assert (await client.pick_token(v5_free=True)) is client.pool[0]

            # 非 V5 的免费图没有第二把的张数限制，仍可进入轮询。
            client._rr = 0
            assert (await client.pick_token()) is second

            # 上游实际成功生成量按成功响应的 n_samples 累加，不影响 V5 额度。
            await client.record_successful_images(second, 3)
            counter = await db.get_upstream_counter(second.token_id, "2026-09-11")
            assert counter == {"images": 3, "v5": 150}
        finally:
            await db.close()
            os.unlink(path)

    asyncio.run(run())


def test_saved_v5_limit_and_usage_follow_token_after_reorder(tmp_path):
    async def run():
        db = Database(str(tmp_path / "upstream.db"))
        await db.connect()
        try:
            original = NaiClient(
                ["first-token", "second-token"], "https://image.example", "https://text.example",
                "https://legacy.example", db=db, day_fn=lambda: "2026-09-11",
                v5_daily_limits=[0, 150], allow_anlas=[True, False],
            )
            second_id = original.pool[1].token_id
            old_id = "token-2-" + second_id.removeprefix("token-")
            await db.bump_upstream_v5_counter(old_id, "2026-09-11")
            await db.bump_upstream_image_counter(old_id, "2026-09-11", 3)
            await db.migrate_upstream_token_ids([token.token_id for token in original.pool])
            await db.migrate_upstream_token_ids([token.token_id for token in original.pool])
            assert await db.get_upstream_counter(second_id, "2026-09-11") == {"images": 3, "v5": 1}
            assert await original.set_v5_daily_limit(second_id, 1)
            assert await original.pick_token(v5_free=True) is original.pool[0]

            reordered = NaiClient(
                ["second-token", "first-token"], "https://image.example", "https://text.example",
                "https://legacy.example", db=db, day_fn=lambda: "2026-09-11",
                v5_daily_limits=[0, 0], allow_anlas=[False, True],
            )
            await reordered.load_saved_limits()
            assert reordered.pool[0].token_id == second_id
            assert reordered.pool[0].v5_daily_limit == 1
            status = await reordered.status()
            assert status[0]["v5_used"] == 1 and status[0]["images_today"] == 3
            assert (await reordered.pick_token(v5_free=True)) is reordered.pool[1]
        finally:
            await db.close()
    asyncio.run(run())


def test_setting_limit_after_unlimited_usage_respects_existing_count(tmp_path):
    async def run():
        db = Database(str(tmp_path / "limit.db"))
        await db.connect()
        try:
            client = NaiClient(
                ["fixture-token"], "https://image.example", "https://text.example",
                "https://legacy.example", db=db, day_fn=lambda: "2026-09-11",
                v5_daily_limits=[0], allow_anlas=[True],
            )
            token = await client.pick_token(v5_free=True)
            assert token is client.pool[0]
            await client.finish_v5_reservation(token, succeeded=True, v5_free=True)
            assert (await db.get_upstream_counter(token.token_id, "2026-09-11"))["v5"] == 1
            assert await client.set_v5_daily_limit(token.token_id, 1)
            assert await client.pick_token(v5_free=True) is None
        finally:
            await db.close()
    asyncio.run(run())


def test_admin_can_set_only_configured_upstream_limit(tmp_path):
    async def run():
        state = GateState(Settings(
            admin_password="fixture-password", secret_key="fixture-secret",
            admin_cookie_secure=False, nai_tokens=["fixture-upstream"],
            data_dir=tmp_path,
        ))
        await state.db.connect()
        app = FastAPI()
        app.state.gate = state
        app.include_router(router)
        token_id = state.nai.pool[0].token_id
        path = f"/admin/api/upstream-tokens/{token_id}/v5-limit"
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://fixture.invalid"
            ) as client:
                assert (await client.put(path, json={"v5_daily_limit": 12})).status_code == 401
                assert (await client.post("/admin/api/login", json={"password": "fixture-password"})).status_code == 200
                for invalid in (-1, 100001, 1.5, "12", True):
                    assert (await client.put(path, json={"v5_daily_limit": invalid})).status_code == 422
                assert (await client.put(
                    "/admin/api/upstream-tokens/token-unknown/v5-limit",
                    json={"v5_daily_limit": 12},
                )).status_code == 404
                assert (await client.put(path, json={"v5_daily_limit": 12})).status_code == 200
                assert state.nai.pool[0].v5_daily_limit == 12
                assert (await state.db.get_upstream_token_limits())[token_id] == 12
        finally:
            await state.db.close()
    asyncio.run(run())
