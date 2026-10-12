"""Discord 机器人配置：校验、读写、心跳在线判断、事件只留 50 条。"""
import asyncio
import json

import pytest

from app import bot_config


class FakeDB:
    def __init__(self):
        self.s = {}

    async def get_setting(self, k, default=None):
        return self.s.get(k, default)

    async def set_setting(self, k, v):
        self.s[k] = v if isinstance(v, str) else str(v)

    async def set_settings_bulk(self, d):
        for k, v in d.items():
            await self.set_setting(k, v)


def test_validate_rejects_bad_values():
    assert bot_config.validate({"gallery_like": False, "gallery_ai_daily": 10, "x": 1}) == {"gallery_like": 0, "gallery_ai_daily": 10}
    for bad in ({"gallery_like": 2}, {"gallery_ai_daily": -1}, {"gallery_forum": "  "}, {"gallery_ai_daily": "5"}):
        with pytest.raises(ValueError):
            bot_config.validate(bad)


def test_save_load_and_snapshot():
    async def go():
        db = FakeDB()
        assert (await bot_config.load(db))["gallery_forum"] == "跑图分享"
        await bot_config.save(db, bot_config.validate({"gallery_ai": 0, "gallery_forum": "作品墙"}))
        cfg = await bot_config.load(db)
        assert cfg["gallery_ai"] == 0 and cfg["gallery_forum"] == "作品墙" and cfg["gallery_like"] == 0     # 默认关（fail-closed）
        await bot_config.report(db, {"user": "奶妹", "ai_ready": True, "token": "secret"}, None, now=1000)
        for i in range(60):
            await bot_config.report(db, None, {"kind": "like", "title": f"t{i}"}, now=1000 + i)
        await bot_config.report(db, None, {"kind": "bogus"}, now=2000)
        snap = await bot_config.snapshot(db, now=1100)
        assert snap["status"]["online"] and "token" not in snap["status"]
        assert len(snap["events"]) == 50 and snap["events"][0]["title"] == "t59"
        assert not (await bot_config.snapshot(db, now=1000 + 181))["status"]["online"]
    asyncio.run(go())
