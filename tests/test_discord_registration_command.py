import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import json

import httpx

from integration import discord_registration
from integration.discord_registration import handle_register, handle_charge, handle_find


class FakeResponse:
    def __init__(self):
        self.send_message = AsyncMock()
        self.defer = AsyncMock()


class ConfiguredBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = patch.dict(os.environ, {
            "REGISTRATION_COMMAND_GUILD": "100000000000000001",
            "REGISTRATION_GATE_URL": "http://127.0.0.1:8000",
        })
        env.start()
        self.addCleanup(env.stop)


class CommandTests(ConfiguredBridgeTests):
    async def test_find_is_private_and_requests_registered_key_recovery(self):
        interaction = SimpleNamespace(guild_id=100000000000000001, user=SimpleNamespace(id=777),
                                      response=FakeResponse(), followup=SimpleNamespace(send=AsyncMock()))
        fake = AsyncMock()
        fake.status_code = 200
        fake.json = lambda: {"status": "restored"}
        with patch.dict(os.environ, {"REGISTRATION_BRIDGE_SECRET": "bridge-secret"}), patch(
            "integration.discord_registration.httpx.AsyncClient") as client:
            client.return_value.__aenter__.return_value.post.return_value = fake
            await handle_find(interaction)
            args = client.return_value.__aenter__.return_value.post.await_args
        self.assertTrue(args.args[0].endswith("/self-register/find"))
        self.assertEqual(args.kwargs["json"]["discord_id"], "777")
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        self.assertIn("原 Key", interaction.followup.send.await_args.args[0])

    async def test_charge_is_private_and_only_available_in_command_guild(self):
        interaction = SimpleNamespace(guild_id=100000000000000001, user=SimpleNamespace(id=777),
                                      response=FakeResponse(), followup=SimpleNamespace(send=AsyncMock()))
        fake = AsyncMock()
        fake.status_code = 200
        fake.json = lambda: {"status": "charged"}
        with patch.dict(os.environ, {"REGISTRATION_BRIDGE_SECRET": "bridge-secret"}), patch(
            "integration.discord_registration.httpx.AsyncClient") as client:
            client.return_value.__aenter__.return_value.post.return_value = fake
            await handle_charge(interaction)
            args = client.return_value.__aenter__.return_value.post.await_args
        self.assertTrue(args.args[0].endswith("/self-register/charge"))
        self.assertEqual(args.kwargs["json"]["discord_id"], "777")
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        self.assertIn("重置", interaction.followup.send.await_args.args[0])
        interaction.guild_id = 999
        with patch("integration.discord_registration.httpx.AsyncClient") as client:
            await handle_charge(interaction)
            client.assert_not_called()
    async def test_wrong_guild_does_not_call_backend(self):
        interaction = SimpleNamespace(guild_id=999, user=SimpleNamespace(id=777), response=FakeResponse())
        with patch("integration.discord_registration.httpx.AsyncClient") as client:
            await handle_register(interaction)
            client.assert_not_called()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_right_guild_returns_private_authorization_link_without_model(self):
        interaction = SimpleNamespace(guild_id=100000000000000001, user=SimpleNamespace(id=777), response=FakeResponse(), followup=SimpleNamespace(send=AsyncMock()))
        fake = AsyncMock()
        fake.status_code = 200
        fake.json = lambda: {"url": "https://discord.com/oauth2/authorize?state=example"}
        with patch.dict(os.environ, {"REGISTRATION_BRIDGE_SECRET": "bridge-secret"}), patch(
            "integration.discord_registration.httpx.AsyncClient") as client:
            client.return_value.__aenter__.return_value.post.return_value = fake
            await handle_register(interaction)
            args = client.return_value.__aenter__.return_value.post.await_args
        self.assertEqual(args.kwargs["json"], {"discord_id": "777", "guild_id": "100000000000000001"})
        self.assertTrue(interaction.response.defer.await_args.kwargs["ephemeral"])
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        self.assertIn("discord.com/oauth2", interaction.followup.send.await_args.args[0])


class VibeCommandTests(ConfiguredBridgeTests):
    def interaction(self, guild_id: int | None = 100000000000000001, user_id: int | None = 777):
        return SimpleNamespace(
            guild_id=guild_id, user=SimpleNamespace(id=user_id, name="spoofed-888"),
            response=FakeResponse(), followup=SimpleNamespace(send=AsyncMock()),
            data={"options": [{"name": "discord_id", "value": "888"}]},
        )

    async def invoke(self, interaction, payload, status_code=200, error=None):
        handler = getattr(discord_registration, "handle_vibe", None)
        assert callable(handler), "native /vibe bridge handler is missing"
        requests = []

        def respond(request):
            requests.append(request)
            if error is not None:
                raise error
            if isinstance(payload, httpx.Response):
                return payload
            return httpx.Response(status_code, json=payload)

        real_client = httpx.AsyncClient
        with patch.dict(os.environ, {"REGISTRATION_BRIDGE_SECRET": "bridge-secret"}), patch(
            "integration.discord_registration.httpx.AsyncClient",
            side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
        ):
            await discord_registration.handle_vibe(interaction)
        return requests

    async def test_vibe_claim_uses_authenticated_identity_and_private_midnight_notice(self):
        interaction = self.interaction()
        requests = await self.invoke(interaction, {"status": "claimed", "remaining": 3})
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), "http://127.0.0.1:8000/self-register/vibe")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer bridge-secret")
        self.assertEqual(json.loads(requests[0].content), {
            "discord_id": "777", "guild_id": "100000000000000001",
        })
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        message = interaction.followup.send.await_args.args[0]
        self.assertIn("已领取 3 次", message)
        self.assertIn("北京时间", message)
        self.assertIn("00:00", message)
        self.assertIn("失效", message)
        self.assertIn("重新领取", message)

    async def test_vibe_repeat_reports_actual_remaining_without_refill(self):
        interaction = self.interaction()
        for remaining in (1, 0):
            with self.subTest(remaining=remaining):
                requests = await self.invoke(interaction, {"status": "already", "remaining": remaining})
                self.assertEqual(len(requests), 1)
                message = interaction.followup.send.await_args.args[0]
                self.assertIn("已领取过", message)
                self.assertIn(f"剩余 {remaining} 次", message)
                self.assertIn("不会补充", message)
                self.assertIn("北京时间明天 00:00", message)
                self.assertIn("失效", message)
                self.assertIn("重新领取", message)
                self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])

    async def test_vibe_rejects_wrong_guild_dm_or_missing_identity_before_http(self):
        for guild_id, user_id in ((999, 777), (None, 777), (100000000000000001, None)):
            with self.subTest(guild_id=guild_id, user_id=user_id):
                interaction = self.interaction(guild_id=guild_id, user_id=user_id)
                with patch(
                    "integration.discord_registration.httpx.AsyncClient",
                    side_effect=AssertionError("rejected identity must not call HTTP"),
                ) as client:
                    await discord_registration.handle_vibe(interaction)
                client.assert_not_called()
                interaction.response.defer.assert_not_awaited()
                interaction.response.send_message.assert_awaited_once_with(
                    "请在指定服务器使用 /vibe。", ephemeral=True,
                )

    async def test_vibe_missing_secret_fails_closed_without_http(self):
        interaction = self.interaction()
        with patch.dict(os.environ, {"REGISTRATION_BRIDGE_SECRET": ""}), patch(
            "integration.discord_registration.httpx.AsyncClient",
            side_effect=AssertionError("missing secret must not call HTTP"),
        ) as client:
            await discord_registration.handle_vibe(interaction)
        client.assert_not_called()
        interaction.response.defer.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once_with(
            "Vibe 领取暂不可用。", ephemeral=True,
        )

    async def test_vibe_forbidden_explains_active_registration_requirement_privately(self):
        interaction = self.interaction()
        await self.invoke(interaction, {"detail": "请先使用 /register 注册有效 Key。"}, status_code=403)
        interaction.followup.send.assert_awaited_once_with(
            "请先使用 /register 注册有效 Key。", ephemeral=True,
        )

    async def test_vibe_errors_fail_privately_without_retry_or_false_claim(self):
        cases = [
            (500, {"status": "claimed", "remaining": 3}, None),
            (401, {}, None),
            (200, {}, None),
            (200, {"status": "unknown", "remaining": 3}, None),
            (200, {"status": "already", "remaining": "secret-value"}, None),
            (200, {"status": "already", "remaining": -1}, None),
            (200, {"status": "already", "remaining": True}, None),
            (200, {"status": "already", "remaining": 4}, None),
            (200, {"status": "claimed", "remaining": 0}, None),
            (200, [], None),
            (200, httpx.Response(200, text="not-json"), None),
            (200, {}, httpx.ReadTimeout("bridge-secret must not leak")),
        ]
        for status_code, payload, error in cases:
            with self.subTest(status_code=status_code, payload=payload, error=error):
                interaction = self.interaction()
                requests = await self.invoke(interaction, payload, status_code=status_code, error=error)
                self.assertEqual(len(requests), 1, "claims must not be retried")
                interaction.followup.send.assert_awaited_once_with(
                    "Vibe 领取暂不可用，请稍后再试。", ephemeral=True,
                )
