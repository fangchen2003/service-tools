import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from integration.discord_registration import handle_register


class FakeResponse:
    def __init__(self):
        self.send_message = AsyncMock()
        self.defer = AsyncMock()


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_guild_does_not_call_backend(self):
        interaction = SimpleNamespace(guild_id=999, user=SimpleNamespace(id=777), response=FakeResponse())
        with patch("integration.discord_registration.httpx.AsyncClient") as client:
            await handle_register(interaction)
            client.assert_not_called()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_right_guild_issues_key_directly_ephemeral_without_model(self):
        interaction = SimpleNamespace(guild_id=1480185480048808009, user=SimpleNamespace(id=777), response=FakeResponse(), followup=SimpleNamespace(send=AsyncMock()))
        fake = AsyncMock()
        fake.status_code = 200
        fake.json = lambda: {"key": "nai-xxx", "message": "🦉 欢迎来到猫头鹰公益站！这是你的 API Key：\n`nai-xxx`"}
        with patch.dict(os.environ, {"REGISTRATION_BRIDGE_SECRET": "bridge-secret"}), patch(
            "integration.discord_registration.httpx.AsyncClient") as client:
            client.return_value.__aenter__.return_value.post.return_value = fake
            await handle_register(interaction)
            args = client.return_value.__aenter__.return_value.post.await_args
        # 直发走 /issue，不再发 OAuth 授权链接
        self.assertTrue(args.args[0].endswith("/self-register/issue"))
        self.assertEqual(args.kwargs["json"]["discord_id"], "777")
        self.assertEqual(args.kwargs["json"]["guild_id"], "1480185480048808009")
        self.assertIn("username", args.kwargs["json"])
        self.assertTrue(interaction.response.defer.await_args.kwargs["ephemeral"])
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        sent = interaction.followup.send.await_args.args[0]
        self.assertIn("nai-xxx", sent)
        self.assertNotIn("oauth2", sent.lower())
