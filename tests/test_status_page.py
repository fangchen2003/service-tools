import time

import pytest

from app import status_page as sp
from app.database import Database


@pytest.mark.asyncio
async def test_incident_lifecycle_banner_bars_and_history(tmp_path):
    # 站长 10/10：状态页照 status.claude.com——未解决事件在页首横幅，组件条按当天最严重事件着色，历史按天列
    db = Database(str(tmp_path / "s.sqlite"))
    await db.connect()
    try:
        await sp.ensure_schema(db)
        lt = time.localtime()
        now = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0, 0, 0, -1))   # 固定在当天中午：0 点附近跑也不跨天
        a = await sp.create_incident(db, "部署期间出图接口报错", "major", ["api"], "investigating", "正在调查", now - 3600)
        await sp.add_update(db, a, "identified", "已定位：快照锁库", now - 3000)
        await sp.add_update(db, a, "resolved", "已修复", now - 1800)
        b = await sp.create_incident(db, "V5 额度口径偏差", "minor", ["api"], "monitoring", "已上线修复，观察 0 点复盘", now - 600)
        from app import status_stats
        status_stats._PENDING.clear()
        status_stats.record(200, now - 60); status_stats.record(500, now - 60)   # 和线上一样经 status_stats 写入（hour = 整点秒数）
        await status_stats.flush(db)
        snap = await sp.snapshot(db, now)
        api = next(c for c in snap["components"] if c["key"] == "api")
        assert [i["id"] for i in snap["active"]] == [b] and api["status"] == "性能下降"
        assert api["days"][-1]["level"] == 2                     # 今天最严重的是 major（已解决的也算进当天颜色）
        assert api["days"][0]["level"] is None                   # 记录开始之前：灰色无数据
        assert 0 < api["uptime"] < 100
        page = sp.render(snap)
        assert "V5 额度口径偏差" in page and "监控中" in page and "已确定" in page and "所有系统运行正常" not in page
        await sp.add_update(db, b, "resolved", "复盘结果正确", now)
        page = sp.render(await sp.snapshot(db, now + 1))
        assert "所有系统运行正常" in page and "过去的事件" in page
        # 10-11 站长：只看生图——上游在最前，官网 / Discord 不上状态页，只影响它们的事件也不显示
        c = await sp.create_incident(db, "Discord 应用受限", "minor", ["bot", "web"], "identified", "申诉中", now - 100)
        snap = await sp.snapshot(db, now + 1)
        assert [x["key"] for x in snap["components"]] == ["upstream", "api"]
        assert c not in [i["id"] for i in snap["active"]] and "Discord 应用受限" not in sp.render(snap)
        # 上游颜色按真实发到 NovelAI 的结果：今天 20 次里 5 次 500 → 25% 失败
        for i in range(20):
            await db._db.execute("INSERT INTO usage_log (ts, key_id, key_name, kind, model, status, up_status) VALUES (?,1,'k','image','m',?,?)",
                                 (now - 120, "ok" if i >= 5 else "error", 200 if i >= 5 else 500))
        await db._db.execute("INSERT INTO usage_log (ts, key_id, key_name, kind, model, status, up_status) VALUES (?,1,'k','image','m','rejected',0)", (now - 120,))
        await db._db.commit()
        await db.set_setting(sp.OFFICIAL_KEY, '{"image": "Operational", "image_code": 100}')
        snap = await sp.snapshot(db, now)
        up = snap["components"][0]
        assert up["days"][-1]["level"] == 2 and up["status"] == "部分中断"
        assert "NovelAI 官方：图片生成 Operational" in sp.render(snap)
    finally:
        await db.close()


def test_render_escapes_text():
    snap = {"now": time.time(), "components": [], "incidents": [], "active": [
        {"id": 1, "title": "<script>x</script>", "impact": "minor", "components": ["api"], "started_at": time.time(),
         "resolved_at": None, "updates": [{"status": "investigating", "body": "<b>", "at": time.time()}]}]}
    page = sp.render(snap)
    assert "<script>x" not in page and "&lt;script&gt;" in page


def test_incident_at_rejects_out_of_range():
    import time as _t
    from fastapi import HTTPException
    from app.status_page import _at, _stamp
    assert _at({}) is None
    assert abs(_at({"at": _t.time()}) - _t.time()) < 5
    for bad in (1.7e12, 1e300, float("nan"), True, "x", -5):
        with pytest.raises(HTTPException):
            _at({"at": bad})
    assert _stamp(1e300) == "时间无效"


def test_empty_body_uses_standard_wording():
    # 10-11 站长：事件正文照 status.claude.com 的标准句式，留空就用标准句
    assert sp._check({"status": "investigating", "title": "t"}, True) == ("investigating", "我们正在调查此问题。")
    assert sp._check({"status": "resolved", "body": "  "}, False)[1] == "此事件已解决。"
    assert sp._check({"status": "monitoring", "body": "自定义"}, False)[1] == "自定义"
