"""site_flags：跨模块共用的设置只有一处定义（键名 / 类型 / 默认值 / 范围），别处不许再各写一套默认值。"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import site_flags as sf

APP = Path(__file__).resolve().parents[1] / "app"


def test_no_module_reads_a_registered_key_directly():
    # 以前 web_login_paused 在两处各写默认 "1"、global_daily_v5 在 7 处各自 `or 0`，改一处漏一处就互相打架
    keys = {s.key for s in sf.ALL}
    offenders = []
    for path in APP.rglob("*.py"):
        if path.name == "site_flags.py":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in re.finditer(r"get_setting\(\s*['\"]([a-z0-9_.]+)['\"]", line):
                if m.group(1) in keys:
                    offenders.append(f"{path.name}:{n} {m.group(1)}")
    assert not offenders, "请改用 site_flags.get：" + ", ".join(offenders)


def test_specs_are_unique_and_documented():
    assert len({s.key for s in sf.ALL}) == len(sf.ALL)
    for s in sf.ALL:
        assert s.owner and s.doc
        assert s.kind in ("flag", "int", "float", "str", "choice")
        if s.kind == "choice":
            assert s.default in s.choices


def test_fail_closed_defaults():
    assert sf.WEB_LOGIN_PAUSED.default is True        # 不发起 OAuth
    assert sf.DM_ENABLED.default is False             # 不发私信
    assert sf.WAITLIST_DM.default is False
    assert sf.SHARE_MODE.default == "observe"         # 不处罚
    assert sf.REGISTER_OPEN.default is False


class _DB:
    def __init__(self, **kv):
        self.kv = dict(kv)

    async def get_setting(self, key, default=None):
        return self.kv.get(key, default)

    async def set_setting(self, key, value):
        self.kv[key] = value


@pytest.mark.asyncio
async def test_parse_clamps_and_falls_back():
    env = SimpleNamespace(global_daily_v5=190, global_monthly_anlas=9000)
    db = _DB()
    assert await sf.get(db, sf.GLOBAL_DAILY_V5, env) == 190                 # 缺省：.env
    assert await sf.get(db, sf.IMAGE_RETENTION) == 3
    db.kv.update(global_daily_v5="abc", audit_image_retention_days="0", share_guard_mode="nuke",
                 issue_hourly_cap="9999", web_login_paused="0")
    assert await sf.get(db, sf.GLOBAL_DAILY_V5, env) == 190                 # 坏值回到默认
    assert await sf.get(db, sf.IMAGE_RETENTION) == 0                        # 0 是有效值（不保存原图）
    assert await sf.get(db, sf.SHARE_MODE) == "observe"                     # 不认识的模式 → 不处罚
    assert await sf.get(db, sf.ISSUE_HOURLY_CAP) == 500                     # 夹到上限
    assert await sf.get(db, sf.WEB_LOGIN_PAUSED) is False
    await sf.put(db, sf.WEB_LOGIN_PAUSED, True)
    assert db.kv["web_login_paused"] == "1"
    with pytest.raises(ValueError):
        await sf.put(db, sf.SHARE_MODE, "nuke")


def test_facts_doc_lists_every_registered_key():
    facts = (APP.parent / "deploy" / "ops" / "SYSTEM-FACTS.md").read_text(encoding="utf-8")
    missing = [s.key for s in sf.ALL if f"`{s.key}`" not in facts]
    assert not missing, f"SYSTEM-FACTS.md 缺这些键（运行 python -m app.site_flags 生成表格）：{missing}"
