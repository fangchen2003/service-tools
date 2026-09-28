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
