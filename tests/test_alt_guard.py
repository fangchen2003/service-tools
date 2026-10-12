"""疑似同一人（小号）识别：行为信号才算数，只靠网段相同不标记。"""
import time

import pytest

from app import alt_guard
from app.database import Database

BASE = {"daily_images": 150, "daily_anlas": 0, "daily_v5": 0, "monthly_anlas": 0, "daily_text_tokens": 0,
        "rpm": 10, "allow_anlas": False, "allow_img2img": False, "exclude_global_v5": False, "image_model_scope": "all"}


async def _keys(db, *names):
    return [(await db.create_key({"name": n, "token": f"nai-{n}", **BASE}))["id"] for n in names]


async def _log(db, kid, ts, status="ok", detail="", src="10.1.*.*", client="ClientX/1.0"):
    await db._db.execute("INSERT INTO usage_log(ts, key_id, key_name, kind, model, status, images, detail, client, src) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?)", (ts, kid, f"k{kid}", "image", "m", status, 1, detail, client, src))


@pytest.mark.asyncio
async def test_handoff_after_limit_flags_pair(tmp_path):
    db = Database(str(tmp_path / "a.sqlite"))
    await db.connect()
    try:
        main, alt, other = await _keys(db, "main", "alt", "other")
        now = time.time()
        for day in range(2):                       # 两次：主号撞上限 → 5 分钟后小号上线
            t0 = now - 86400 * day - 7200
            for i in range(5):
                await _log(db, main, t0 + i * 60)
            await _log(db, main, t0 + 400, "rejected", "402 已达今日 V5 额度（36 张/天）")
            for i in range(5):
                await _log(db, alt, t0 + 700 + i * 60)
        for i in range(5):                          # 无关的第三个人
            await _log(db, other, now - 50000 + i * 60, client="Other/2", src="20.2.*.*")
        await db._db.commit()
        links = await alt_guard.scan(db, now)
        assert [p["keys"] for p in links] == [sorted([main, alt])]
        assert links[0]["signals"]["handoff"] == 2 and "用完一把马上换另一把" in alt_guard.describe(links[0]["signals"])
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_same_network_alone_is_not_flagged(tmp_path):
    # 室友 / 同一校园网：网段、客户端相同，但各用各的（时间不挨着、没有撞上限后换号）
    db = Database(str(tmp_path / "b.sqlite"))
    await db.connect()
    try:
        a, b = await _keys(db, "roommate-a", "roommate-b")
        now = time.time()
        for kid, ts in ((a, now - 80000), (b, now - 40000)):
            await db._db.execute("INSERT INTO key_sources(key_id, net_hash, label, first_seen, last_seen, hits) "
                                 "VALUES (?,?,?,?,?,1)", (kid, "net-1", "10.1.*.*", ts, ts))
            for i in range(3):
                await _log(db, kid, ts + i * 60)
        await db._db.commit()
        assert await alt_guard.scan(db, now) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sybil_rapid_switch_and_same_sig_flagged(tmp_path):
    # 一人双开多号：同一出口、同一出图习惯参数签名、两两短时交替出图
    db = Database(str(tmp_path / "c.sqlite"))
    await db.connect()
    try:
        k1, k2, third = await _keys(db, "sybil-1", "sybil-2", "innocent-third")
        now = time.time()
        # 写入出图习惯签名到 req_features
        shared_sig = "sig_custom_preset_123"
        for kid in (k1, k2):
            await db._db.execute(
                "INSERT INTO req_features(ts, key_id, src, fp, os, sig, toks, busy) VALUES (?,?,?,?,?,?,?,?)",
                (now - 1000, kid, "154.64.*.*", "fp123", "Windows", shared_sig, "toks", 0))

        # 模拟两把 Key 相互交替出图，同时有第三个人并发插队
        t0 = now - 500
        for i in range(4):
            await _log(db, k1, t0 + i * 80, src="154.64.*.*")
            # 第三个人并发插队，测试两两交替是否依然能准确识别
            await _log(db, third, t0 + i * 80 + 10, src="99.99.*.*", client="Third/1.0")
            await _log(db, k2, t0 + i * 80 + 25, src="154.64.*.*")

        await db._db.commit()
        links = await alt_guard.scan(db, now)
        assert len(links) == 1
        link = links[0]
        assert link["keys"] == sorted([k1, k2])
        assert link["signals"]["same_sig"] is True
        assert link["signals"]["switch"] >= 4
        desc = alt_guard.describe(link["signals"])
        assert "出图习惯完全相同" in desc
        assert "来回交替" in desc
    finally:
        await db.close()

