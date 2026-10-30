"""Discord /top restoration, using real command objects and no live writes."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
try:
    import discord
    from discord.ext import commands
except ImportError:
    raise unittest.SkipTest("Optional Hermes Discord plugin dependencies are not installed") from None

PLUGIN = Path(os.environ["HERMES_TOP_PLUGIN_PATH"]) if os.environ.get("HERMES_TOP_PLUGIN_PATH") else None

def load():
    spec = importlib.util.spec_from_file_location('discord_top_test', PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

@unittest.skipUnless(PLUGIN is not None and PLUGIN.is_file(), "Requires the optional local Hermes plugin installation")
class TopTests(unittest.IsolatedAsyncioTestCase):
    async def test_top_is_public_guild_only_and_syncs_once(self):
        assert PLUGIN is not None
        self.assertTrue(PLUGIN.exists(), 'Durable /top plugin missing')
        m = load()
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        bot.wait_until_ready = AsyncMock()
        bot.tree.sync = AsyncMock(return_value=[])
        factories=[]
        m.register(SimpleNamespace(register_platform_handler=lambda p,f: factories.append((p,f))))
        self.assertEqual(factories[0][0], 'discord')
        wire=factories[0][1]
        wire(bot, SimpleNamespace())
        await bot._discord_top_sync_task
        cmd=bot.tree.get_command('top')
        self.assertIsNotNone(cmd)
        payload=cmd.to_dict(bot.tree)
        self.assertFalse(payload['dm_permission'])
        self.assertIsNone(payload['default_member_permissions'])
        wire(bot, SimpleNamespace())
        bot.tree.sync.assert_awaited_once()
        await bot.close()

    async def test_one_oldest_message_query_returns_private_jump_link(self):
        m = load()
        async def history(**kwargs):
            yield SimpleNamespace(id=123456)
            raise AssertionError('Must not enumerate history after first message')
        channel=SimpleNamespace(id=999, history=Mock(side_effect=history))
        interaction=SimpleNamespace(guild_id=888, channel_id=999, channel=channel,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            edit_original_response=AsyncMock())
        await m._handle_top(interaction)
        channel.history.assert_called_once()
        options=channel.history.call_args.kwargs
        self.assertEqual(options['limit'], 1)
        self.assertTrue(options['oldest_first'])
        self.assertEqual(options['after'].id, 0)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        self.assertIn('https://discord.com/channels/888/999/123456',
                      interaction.edit_original_response.await_args.kwargs['content'])

    async def test_missing_channel_is_resolved_through_native_client(self):
        m=load()
        async def history(**kwargs):
            yield SimpleNamespace(id=123)
        channel=SimpleNamespace(id=999, history=Mock(side_effect=history))
        client=SimpleNamespace(fetch_channel=AsyncMock(return_value=channel))
        i=SimpleNamespace(guild_id=888, channel_id=999, channel=None, client=client,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            edit_original_response=AsyncMock())
        await m._handle_top(i)
        client.fetch_channel.assert_awaited_once_with(999)
        self.assertIn('/888/999/123', i.edit_original_response.await_args.kwargs['content'])

    async def test_dm_empty_history_and_denied_history_are_safe(self):
        m=load()
        async def empty(**kwargs):
            if False:
                yield None
        for case in ('dm','empty','denied'):
            with self.subTest(case=case):
                channel=SimpleNamespace(id=999, history=Mock(side_effect=empty))
                if case=='denied':
                    channel.history.side_effect=PermissionError('private diagnostic')
                i=SimpleNamespace(guild_id=None if case=='dm' else 888, channel_id=999, channel=channel,
                    response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                    edit_original_response=AsyncMock())
                await m._handle_top(i)
                if case=='dm':
                    channel.history.assert_not_called()
                    self.assertTrue(i.response.send_message.await_args.kwargs['ephemeral'])
                else:
                    self.assertTrue(i.response.defer.await_args.kwargs['ephemeral'])
                    text=i.edit_original_response.await_args.kwargs['content']
                    self.assertNotIn('private diagnostic',text)
                    self.assertIn('没有可跳转' if case=='empty' else '读取消息历史',text)

if __name__ == '__main__':
    unittest.main()
