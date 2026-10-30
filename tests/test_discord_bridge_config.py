"""Bridge deployment is explicit; no real Discord or gateway requests."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from integration import discord_registration


class BridgeDeploymentTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_handlers_use_configured_guild_and_gateway(self):
        payloads = {
            "register": {"url": "https://discord.com/oauth2/authorize?state=fixture"},
            "charge": {"status": "charged"},
            "find": {"status": "active"},
            "vibe": {"status": "claimed", "remaining": 3},
        }
        env = {"REGISTRATION_COMMAND_GUILD": "100000000000000001",
               "REGISTRATION_GATE_URL": "http://gateway.example.test:8000/",
               "REGISTRATION_BRIDGE_SECRET": "fixture-bridge-secret"}
        for command, payload in payloads.items():
            with self.subTest(command=command):
                interaction = SimpleNamespace(
                    guild_id=100000000000000001, user=SimpleNamespace(id=777),
                    response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                    followup=SimpleNamespace(send=AsyncMock()))
                response = SimpleNamespace(status_code=200, json=lambda: payload)
                with patch.dict(os.environ, env, clear=True), patch(
                        "integration.discord_registration.httpx.AsyncClient") as client:
                    client.return_value.__aenter__.return_value.post.return_value = response
                    await getattr(discord_registration, "handle_" + command)(interaction)
                    post = client.return_value.__aenter__.return_value.post
                    post.assert_awaited_once()
                    route = "intent" if command == "register" else command
                    self.assertEqual(post.await_args.args[0],
                                     "http://gateway.example.test:8000/self-register/" + route)
                    self.assertEqual(post.await_args.kwargs["json"],
                                     {"discord_id": "777", "guild_id": "100000000000000001"})
                interaction.response.defer.assert_awaited_once_with(ephemeral=True)
                self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])

    async def test_unconfigured_invalid_or_wrong_guild_never_calls_backend(self):
        for configured in ("", "not-a-guild", "0", "-1", "100000000000000002"):
            for command in ("register", "charge", "find", "vibe"):
                with self.subTest(configured=configured, command=command):
                    interaction = SimpleNamespace(
                        guild_id=100000000000000001, user=SimpleNamespace(id=777),
                        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                        followup=SimpleNamespace(send=AsyncMock()))
                    with patch.dict(os.environ, {
                            "REGISTRATION_COMMAND_GUILD": configured,
                            "REGISTRATION_BRIDGE_SECRET": "fixture-secret"}, clear=True), patch(
                            "integration.discord_registration.httpx.AsyncClient") as client:
                        await getattr(discord_registration, "handle_" + command)(interaction)
                        client.assert_not_called()
                    interaction.response.defer.assert_not_awaited()
                    self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
