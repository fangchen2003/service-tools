import time

import pytest

from app.database import Database


@pytest.mark.asyncio
async def test_reset_coupon_used_once_and_expires(tmp_path):
    db = Database(str(tmp_path / "c.db"))
    await db.connect()
    k = await db.create_key({"name": "a", "token": "nai-c", "daily_images": 1, "monthly_anlas": 0,
                             "daily_text_tokens": 0, "rpm": 5, "expires_at": None})
    other = await db.create_key({"name": "b", "token": "nai-d", "daily_images": 1, "monthly_anlas": 0,
                                 "daily_text_tokens": 0, "rpm": 5, "expires_at": None})
    c = await db.add_coupon(k["id"], "reset", 7, "唱得真好听", "奶妹")
    assert [x["id"] for x in await db.list_coupons(k["id"])] == [c["id"]]
    assert await db.use_coupon(c["id"], other["id"]) is None          # 别人的券不能用
    assert (await db.use_coupon(c["id"], k["id"]))["note"] == "唱得真好听"
    assert await db.use_coupon(c["id"], k["id"]) is None              # 只能用一次
    assert await db.list_coupons(k["id"]) == []
    old = await db.add_coupon(k["id"], "reset", 7)
    await db._db.execute("UPDATE coupons SET expires_at=? WHERE id=?", (time.time() - 1, old["id"]))
    await db._db.commit()
    assert await db.use_coupon(old["id"], k["id"]) is None            # 过期不能用
    await db.close()
