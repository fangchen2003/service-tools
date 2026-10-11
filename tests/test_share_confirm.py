"""10-11 站长：被标记的成员在首页确认网络情况（不发私信），后台核对误判。"""
import time

import pytest

from app import share_confirm
from app.database import Database


@pytest.mark.asyncio
async def test_ask_once_then_again_after_new_evidence(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    await db.connect()
    try:
        await share_confirm.ensure(db)
        now = time.time()
        assert not await share_confirm.pending(db, 1, now)                      # 没被标记：不问
        await db._db.execute("INSERT INTO share_evidence(ts, key_id, kind, points, detail) VALUES (?,?,?,?,?)",
                             (now - 3600, 1, "overlap", 35, "x"))
        await db._db.commit()
        assert await share_confirm.pending(db, 1, now)
        assert await share_confirm.flagged_keys(db, now) == [1]
        with pytest.raises(ValueError):
            await share_confirm.record(db, 1, "hack", "", now)
        await share_confirm.record(db, 1, "vpn", "机场节点", now)
        assert not await share_confirm.pending(db, 1, now)                      # 回答过：不再问
        a = (await share_confirm.answers(db))[1]
        assert a["answer"] == "vpn" and a["note"] == "机场节点"
        later = now + 8 * 86400
        await db._db.execute("INSERT INTO share_evidence(ts, key_id, kind, points, detail) VALUES (?,?,?,?,?)",
                             (later - 60, 1, "overlap", 35, "y"))
        await db._db.commit()
        assert await share_confirm.pending(db, 1, later)                        # 7 天后又有新记录：再问
        # 24 小时内 ≥ 3 个网段也算标记
        for i in range(3):
            await db._db.execute("INSERT INTO key_sources(key_id, net_hash, label, first_seen, last_seen, hits) VALUES (2,?,?,?,?,1)",
                                 (f"h{i}", "", now - 100, now - 100))
        await db._db.commit()
        assert await share_confirm.pending(db, 2, now)
    finally:
        await db.close()
