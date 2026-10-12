import sqlite3

import pytest

from app.database import Database


@pytest.mark.asyncio
async def test_main_connection_recovers_after_lock_timeout(tmp_path):
    # 线上 10/10 19:20：等锁超时后主连接留着没结束的事务 → 读一次拿到旧快照 → 别人一提交，之后每次写都 locked
    path = str(tmp_path / "g.db")
    db = Database(path)
    await db.connect()
    key = await db.create_key({"name": "a", "token": "nai-x", "daily_images": 1, "monthly_anlas": 0,
                               "daily_text_tokens": 0, "rpm": 5, "expires_at": None})
    await db._db.execute("PRAGMA busy_timeout=100")
    other = sqlite3.connect(path, timeout=0.1)
    other.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError):
        await db.touch_key(key["id"])
    await db.get_setting("x")                       # 主连接读一次
    other.execute("INSERT INTO site_settings(key, value) VALUES ('y', '1')")
    other.commit()                                  # 别的连接提交
    await db.touch_key(key["id"])                   # 修好之前：这里永远 database is locked
    await db.set_setting("z", 1)
    other.close()
    await db.close()
