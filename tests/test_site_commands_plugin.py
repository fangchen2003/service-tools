"""Real Discord tree; sync and interactions are mocked, no external writes."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

try:
    import discord
    from discord.ext import commands
except ImportError:
    raise unittest.SkipTest("Optional Hermes Discord plugin dependencies are not installed") from None

PLUGIN = Path('~/.hermes/plugins/nai-site-commands/__init__.py')

@unittest.skipUnless(PLUGIN.exists(), "Requires the optional local Hermes plugin installation")
class SiteCommandsTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_native_commands_are_restored_and_synced_once(self):
        self.assertTrue(PLUGIN.exists(), 'Durable site command extension is missing')
        spec = importlib.util.spec_from_file_location('site_commands_under_test', PLUGIN)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        bot.wait_until_ready = AsyncMock()
        bot.tree.sync = AsyncMock(return_value=[])
        private = discord.app_commands.Command(name='private_test', description='private', callback=self.private)
        private.default_permissions = discord.Permissions(0)
        bot.tree.add_command(private)
        factories = []
        ctx = SimpleNamespace(register_platform_handler=lambda name, factory: factories.append((name, factory)))
        module.register(ctx)
        self.assertEqual(len(factories), 1)
        self.assertEqual(factories[0][0], 'discord')
        factory = factories[0][1]
        factory(bot, SimpleNamespace())
        await bot._nai_site_commands_sync_task
        for name in ('register', 'charge', 'find', 'vibe'):
            cmd = bot.tree.get_command(name)
            self.assertIsNotNone(cmd)
            data = cmd.to_dict(bot.tree)
            self.assertFalse(data['dm_permission'])
            self.assertIsNone(data['default_member_permissions'])
        self.assertEqual(private.default_permissions.value, 0)
        factory(bot, SimpleNamespace())
        await asyncio.sleep(0)
        bot.tree.sync.assert_awaited_once()
        await bot.close()

    async def test_callbacks_use_private_authenticated_bridge_without_agent(self):
        import os
        import httpx
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('site_commands_callbacks', PLUGIN)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        requests = []
        responses = {'intent': {'url': 'https://discord.com/oauth2/authorize?state=test'},
                     'charge': {'status': 'charged'}, 'find': {'status': 'active'},
                     'vibe': {'status': 'claimed', 'remaining': 3}}
        def http_handler(request):
            import json
            self.assertEqual(request.headers['Authorization'], 'Bearer unit-secret')
            self.assertEqual(json.loads(request.content), {'discord_id': '777', 'guild_id': '100000000000000001'})
            requests.append(request.url.path)
            return httpx.Response(200, json=responses[request.url.path.rsplit('/', 1)[-1]])
        client_class = httpx.AsyncClient
        def client(*args, **kwargs):
            return client_class(transport=httpx.MockTransport(http_handler))
        with patch.dict(os.environ, {'REGISTRATION_BRIDGE_SECRET': 'unit-secret'}), patch('httpx.AsyncClient', client):
            for name in ('register', 'charge', 'find', 'vibe'):
                interaction = SimpleNamespace(guild_id=100000000000000001, user=SimpleNamespace(id=777),
                    response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                    followup=SimpleNamespace(send=AsyncMock()))
                await module._callback(name)(interaction)
                self.assertTrue(interaction.response.defer.await_args.kwargs['ephemeral'])
                self.assertTrue(interaction.followup.send.await_args.kwargs['ephemeral'])
                for bad_guild in (None, 0):
                    interaction.guild_id = bad_guild
                    before = len(requests)
                    await module._callback(name)(interaction)
                    self.assertEqual(len(requests), before)
                    self.assertTrue(interaction.response.send_message.await_args.kwargs['ephemeral'])
        self.assertEqual(requests, ['/self-register/intent', '/self-register/charge', '/self-register/find', '/self-register/vibe'])

    async def private(self, interaction: discord.Interaction):
        pass

if __name__ == '__main__':
    unittest.main()
