"""跨模块共用的站点设置：一处定义键名、类型、默认值、范围和归属。

以前同一个键在好几个文件里各写一遍默认值（如 web_login_paused 在路由和首页状态各写 "1"，
global_daily_v5 在 7 处各自 `or 0`），改一处漏一处就互相打架。现在：
- 读写这些键只通过 `await get(db, SPEC)` / `await put(db, SPEC, value)`；
- 值缺失、格式不对 → 回到默认值（默认值本身按 fail-closed 选：不发私信、不开放 OAuth、不处罚）；
- tests/test_site_flags.py 扫描源码：注册过的键不许在别处直接 get_setting，避免再长出第二套默认值；
- `python -m app.site_flags` 输出 SYSTEM-FACTS 用的表格。

只登记「多个模块共用」或「对成员可见的开关」。各自模块内部的参数组（guard_*、runtime_*、bot_*、
quota_algo / anlas_pool 的 DEFAULTS、module_*）已经有自己的类型表，不在这里重复。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class Spec:
    key: str
    kind: str                       # flag（"1"/"0"）· int · float · str · choice
    default: Any
    owner: str
    doc: str
    lo: Optional[float] = None
    hi: Optional[float] = None
    choices: tuple = field(default=())
    env: str = ""                   # 默认值取自 config.Settings 的这个属性（.env 可调）


WEB_LOGIN_PAUSED = Spec("web_login_paused", "flag", True, "registration_routes",
                        "网页 Discord OAuth 登录暂停（申诉期间）。Key 登录不受影响")
DM_ENABLED = Spec("dm_enabled", "flag", False, "registration", "私信总闸门（关 = 任何私信都不发）")
WAITLIST_DM = Spec("waitlist_dm", "flag", False, "registration", "有名额时私信候补成员（关 = 只在公告频道发一条汇总）")
ISSUE_HOURLY_CAP = Spec("issue_hourly_cap", "int", 12, "registration", "每小时最多发放的新 Key", 0, 500)
REGISTER_OPEN = Spec("register_open", "flag", False, "modules / registration", "开放 /register 领取")
GLOBAL_DAILY_V5 = Spec("global_daily_v5", "int", None, "额度 / 首页 / 客户端", "全站每日 V5 张数（0 = 不限）",
                       0, 100000, env="global_daily_v5")
GLOBAL_MONTHLY_ANLAS = Spec("global_monthly_anlas", "float", None, "额度 / Anlas 池", "全站每月 Anlas 预算（0 = 不限）",
                            0, 1_000_000, env="global_monthly_anlas")
from .guard import FIELDS as _GUARD  # noqa: E402  保底张数的默认值和范围以 guard 为准，这里只是登记出来给别的模块读
GUARD_BASE = Spec("guard_base_daily_images", "int", _GUARD["base_daily_images"][0], "guard（注册私信也读）",
                  "V4.5 每人每天保底张数", _GUARD["base_daily_images"][1], _GUARD["base_daily_images"][2])
ALGO_NOTICE = Spec("algo_notice", "str", "", "quota_algo → 首页", "首页显示的今日额度说明")
IMAGE_RETENTION = Spec("audit_image_retention_days", "int", 3, "audit", "（已停用，2026-10-11 起原图按容量上限清理）", 0, 365)
IMAGE_CAP_MB = Spec("audit_image_cap_mb", "int", 8192, "audit", "原图占用上限（MB）：超过后自动删掉最旧的原图；0 = 不限", 0, 500_000)
IMAGE_KEEP_HOURS = Spec("audit_image_keep_hours", "int", 24, "audit", "原图在服务器上保存几小时，到点自动删；0 = 只按容量上限删", 0, 8760)
MEMBER_DOWNLOAD = Spec("member_image_download", "flag", False, "audit", "成员在首页打包下载自己最近的原图（10-11 站长：先关）")
SHARE_MODE = Spec("share_guard_mode", "choice", "observe", "share_guard", "防分享：enforce 处罚 / observe 只记录 / off",
                  choices=("enforce", "observe", "off"))

WAITLIST = Spec("waitlist_enabled", "flag", False, "registration",
                "候补排队（关 = 名额满了直接提示，有空位先到先得；机器人不能私信时候补只会空占名额）")
MEMBER_SWEEP = Spec("member_sweep", "flag", True, "registration", "退群回收：每人每天核对一次是否还在服务器，不在就删 Key")

ALL = (WAITLIST, MEMBER_SWEEP, WEB_LOGIN_PAUSED, DM_ENABLED, WAITLIST_DM, ISSUE_HOURLY_CAP, REGISTER_OPEN,
       GLOBAL_DAILY_V5, GLOBAL_MONTHLY_ANLAS, GUARD_BASE, ALGO_NOTICE, IMAGE_RETENTION, IMAGE_CAP_MB, IMAGE_KEEP_HOURS, MEMBER_DOWNLOAD, SHARE_MODE)


def default_of(spec: Spec, settings=None) -> Any:
    if spec.env and settings is not None and hasattr(settings, spec.env):
        return parse(spec, getattr(settings, spec.env), fallback=0)
    return spec.default if spec.default is not None else 0


def parse(spec: Spec, raw: Any, fallback: Any = None) -> Any:
    """把库里的原始值转成带类型的值；转不了返回 fallback。"""
    if raw is None or (isinstance(raw, str) and raw.strip() == "" and spec.kind != "str"):
        return fallback
    s = str(raw).strip()
    try:
        low = s.lower()
        if spec.kind == "flag":         # 认不出的写法回到默认值（fail-closed），不能当成「关」
            return True if low in ("1", "true", "yes", "on") else False if low in ("0", "false", "no", "off") else fallback
        if spec.kind == "choice":
            return s if s in spec.choices else fallback
        if spec.kind == "str":
            return str(raw)
        num = float(s)
        if num != num:                      # NaN
            return fallback
        if spec.lo is not None:
            num = max(spec.lo, num)
        if spec.hi is not None:
            num = min(spec.hi, num)
        return int(num) if spec.kind == "int" else num
    except (TypeError, ValueError):
        return fallback


async def get(db, spec: Spec, settings=None) -> Any:
    fallback = default_of(spec, settings)
    return parse(spec, await db.get_setting(spec.key, None), fallback)


def encode(spec: Spec, value: Any) -> str:
    if spec.kind == "flag":
        return "1" if value else "0"
    if spec.kind == "choice":
        if value not in spec.choices:
            raise ValueError(f"{spec.key} 只能是 {' / '.join(spec.choices)}")
        return str(value)
    if spec.kind in ("int", "float"):
        v = parse(spec, value)
        if v is None:
            raise ValueError(f"{spec.key} 必须是数字")
        return str(v)
    return str(value)


async def put(db, spec: Spec, value: Any) -> None:
    await db.set_setting(spec.key, encode(spec, value))


def facts_table() -> str:
    rows = ["| 键 | 类型 | 默认 | 范围 | 归属 | 说明 |", "|---|---|---|---|---|---|"]
    for s in ALL:
        d = f"`.env {s.env}`" if s.env else repr(s.default)
        rng = " / ".join(s.choices) if s.choices else (f"{s.lo:g}–{s.hi:g}" if s.lo is not None else "")
        rows.append(f"| `{s.key}` | {s.kind} | {d} | {rng} | {s.owner} | {s.doc} |")
    return "\n".join(rows)


if __name__ == "__main__":
    print(facts_table())
