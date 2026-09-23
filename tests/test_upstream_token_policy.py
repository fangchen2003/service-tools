"""特定上游令牌的免费图和 Anlas 隔离测试。"""

import asyncio
import os
import tempfile

from fastapi import FastAPI
import httpx
import pytest

from app.admin import router
from app.config import Settings
from app.database import Database
from app.nai import NaiClient, UpstreamError
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
            assert await original.set_admin_enabled(second_id, False)
            assert await original.pick_token(v5_free=True) is original.pool[0]

            reordered = NaiClient(
                ["second-token", "first-token"], "https://image.example", "https://text.example",
                "https://legacy.example", db=db, day_fn=lambda: "2026-09-11",
                v5_daily_limits=[0, 0], allow_anlas=[False, True],
            )
            await reordered.load_saved_limits()
            assert reordered.pool[0].token_id == second_id
            assert reordered.pool[0].v5_daily_limit == 1
            assert reordered.pool[0].admin_enabled is False
            status = await reordered.status()
            assert status[0]["v5_used"] == 1 and status[0]["images_today"] == 3
            assert status[0]["admin_enabled"] is False and status[0]["usable"] is False
            assert (await reordered.pick_token(v5_free=True)) is reordered.pool[1]
            assert await reordered.set_admin_enabled(second_id, True)
            assert reordered.pool[0].admin_enabled is True
            assert (await db.get_upstream_token_enabled())[second_id] is True
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
        enabled_path = f"/admin/api/upstream-tokens/{token_id}/enabled"
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://fixture.invalid"
            ) as client:
                assert (await client.put(path, json={"v5_daily_limit": 12})).status_code == 401
                assert (await client.put(enabled_path, json={"enabled": False})).status_code == 401
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
                for invalid in (0, 1, "false", None):
                    assert (await client.put(enabled_path, json={"enabled": invalid})).status_code == 422
                assert (await client.put(
                    "/admin/api/upstream-tokens/token-unknown/enabled",
                    json={"enabled": False},
                )).status_code == 404
                assert (await client.put(enabled_path, json={"enabled": False})).status_code == 200
                assert state.nai.pool[0].admin_enabled is False
                assert (await state.db.get_upstream_token_enabled())[token_id] is False
                assert (await client.put(enabled_path, json={"enabled": True})).status_code == 200
                assert state.nai.pool[0].admin_enabled is True
        finally:
            await state.db.close()
    asyncio.run(run())


def test_disabling_during_image_wait_reroutes_before_dispatch(tmp_path):
    async def run():
        db = Database(str(tmp_path / "reroute.db"))
        await db.connect()
        try:
            client = NaiClient(
                ["first-token", "second-token"], "https://image.example", "https://text.example",
                "https://legacy.example", db=db, day_fn=lambda: "2026-09-11",
                v5_daily_limits=[0, 0], allow_anlas=[True, True],
            )
            entered, release = asyncio.Event(), asyncio.Event()
            sent = []

            async def wait_slot(token):
                if token is client.pool[1]:
                    entered.set()
                    await release.wait()

            class FakeHTTP:
                async def request(self, *args, **kwargs):
                    sent.append(kwargs["headers"]["Authorization"])
                    return httpx.Response(200, content=b"ok")

            client.wait_for_token_image_slot = wait_slot
            client._client = FakeHTTP()
            task = asyncio.create_task(client.request("POST", "https://image.example",
                                                      image_lane=True))
            await entered.wait()
            assert await client.set_admin_enabled(client.pool[1].token_id, False)
            release.set()
            assert (await task).status_code == 200
            assert sent == ["Bearer first-token"]
        finally:
            await db.close()
    asyncio.run(run())


def test_disable_ack_waits_for_dispatched_request_and_blocks_next(tmp_path):
    async def run():
        db = Database(str(tmp_path / "dispatch.db"))
        await db.connect()
        try:
            client = NaiClient(
                ["fixture-token"], "https://image.example", "https://text.example",
                "https://legacy.example", db=db, day_fn=lambda: "2026-09-11",
                v5_daily_limits=[0], allow_anlas=[True],
            )
            entered, release = asyncio.Event(), asyncio.Event()
            sent = []

            class FakeHTTP:
                async def request(self, *args, **kwargs):
                    sent.append(kwargs["headers"]["Authorization"])
                    entered.set()
                    await release.wait()
                    return httpx.Response(200, content=b"ok")

            client._client = FakeHTTP()
            first = asyncio.create_task(client.request("POST", "https://image.example"))
            await entered.wait()
            disable = asyncio.create_task(client.set_admin_enabled(client.pool[0].token_id, False))
            await asyncio.sleep(0)
            assert not disable.done()
            release.set()
            assert (await first).status_code == 200
            assert await disable
            with pytest.raises(UpstreamError) as exc:
                await client.request("POST", "https://image.example")
            assert exc.value.status == 503 and sent == ["Bearer fixture-token"]
        finally:
            await db.close()
    asyncio.run(run())


def test_disable_before_allowance_lookup_sends_nothing(tmp_path):
    async def run():
        db = Database(str(tmp_path / "allowance.db"))
        await db.connect()
        try:
            client = NaiClient(
                ["fixture-token"], "https://image.example", "https://text.example",
                "https://legacy.example", db=db, day_fn=lambda: "2026-09-11",
                v5_daily_limits=[1], allow_anlas=[True],
            )
            token = await client.pick_token(v5_free=True)
            assert token is not None
            calls = []

            class FakeAllowance:
                async def resolve(self, *args):
                    calls.append(args)
                    return False

            client.allowance = FakeAllowance()
            assert await client.set_admin_enabled(token.token_id, False)
            with pytest.raises(UpstreamError) as exc:
                await client._resolve_v5_cost(token, lambda _: None)
            assert exc.value.status == 503 and calls == []
            await client.finish_v5_reservation(token, succeeded=False, v5_free=True)
        finally:
            await db.close()
    asyncio.run(run())
