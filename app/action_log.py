"""后台操作日志：记录谁在什么时候做了什么（后台、机器人管理命令、系统自动操作）。

写入失败只打印警告，绝不影响操作本身。参数摘要会去掉 Token、密码和公告正文等敏感内容。
"""
from __future__ import annotations

import json
from typing import Any

RETENTION_DAYS = 180
_SECRET_FIELDS = {"token", "password", "current", "new", "current_password", "new_password",
                  "old_password", "html", "secret"}

# (方法, 路由模板) → 操作名；路由模板不含 /admin/api 前缀。
ADMIN_ACTIONS = {
    ("POST", "/login"): "登录后台",
    ("PUT", "/password"): "修改后台密码",
    ("POST", "/logout"): "退出登录",
    ("POST", "/reconciliation"): "Anlas 核对",
    ("POST", "/keys"): "新建 Key",
    ("POST", "/keys/{key_id}/regenerate"): "重新生成 Key",
    ("PATCH", "/keys/{key_id}"): "修改 Key",
    ("POST", "/keys/{key_id}/reset-daily-image-quota"): "重置今日额度",
    ("POST", "/keys/{key_id}/bind-discord"): "手动绑定 Discord",
    ("DELETE", "/keys/{key_id}"): "删除 Key",
    ("PUT", "/keys/{key_id}/tags"): "成员标签",
    ("POST", "/keys/{key_id}/coupons"): "发重置券",
    ("DELETE", "/keys/{key_id}/tags/{tag}"): "移除成员标签",
    ("PUT", "/runtime-limits"): "修改排队与冷却",
    ("POST", "/upstream-tokens"): "添加上游 Token",
    ("PUT", "/upstream-tokens/{token_id}"): "替换上游 Token",
    ("DELETE", "/upstream-tokens/{token_id}"): "删除上游 Token",
    ("PUT", "/upstream-tokens/{token_id}/v5-limit"): "修改上游 Token V5 日限",
    ("PUT", "/upstream-tokens/{token_id}/image-concurrency"): "修改上游 Token 图片并发",
    ("PUT", "/upstream-tokens/{token_id}/enabled"): "启用/停用上游 Token",
    ("PUT", "/settings"): "修改预算",
    ("PUT", "/announcement"): "修改站点公告",
    ("POST", "/alerts/test"): "发送测试告警",
    ("PUT", "/ops/registration"): "修改开放注册设置",
    ("PUT", "/ops/features"): "修改全局功能开关",
    ("PUT", "/ops/audit"): "修改生成记录设置",
    ("PUT", "/guard"): "修改账号保护与排队设置",
    ("PUT", "/anlas-pool"): "修改 Anlas 自动分配",
    ("POST", "/errors/{sig}/resolve"): "标记 Bug 已处理",
    ("PUT", "/quota-algo"): "修改动态额度参数",
    ("PUT", "/discord-bot"): "修改 Discord 机器人设置",
    ("POST", "/keys/{key_id}/share-clear"): "防分享清零（误判）",
    ("PUT", "/share-guard"): "修改防分享模式",
    ("PUT", "/modules/{name}"): "启用 / 关闭模块",
    ("POST", "/ops/reset-all-keys"): "全站杀头重置",
}


def summarize(body: Any, limit: int = 300) -> str:
    """把请求参数压成一行摘要，去掉敏感字段。"""
    if not isinstance(body, dict) or not body:
        return ""
    clean = {}
    for name, value in body.items():
        if name.lower() in _SECRET_FIELDS:
            clean[name] = "（已隐去）"
        elif isinstance(value, str) and len(value) > 80:
            clean[name] = value[:80] + "…"
        else:
            clean[name] = value
    text = json.dumps(clean, ensure_ascii=False, separators=(",", ":"), default=str)
    return text if len(text) <= limit else text[:limit] + "…"


async def log_action(db, actor: str, action: str, target: str = "", detail: str = "",
                     ok: bool = True) -> None:
    try:
        await db.add_admin_action(actor[:80], action[:80], str(target)[:120], str(detail)[:400], ok)
    except Exception as exc:  # 日志写失败不能影响操作
        print(f"[warn] admin action log failed: {type(exc).__name__}")
