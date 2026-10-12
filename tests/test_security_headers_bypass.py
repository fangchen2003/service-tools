"""针对 API 安全专家 Challenge 的防伪造回归测试。

严防客户端通过伪造 X-Admin-Test, X-Test-Channel 或 User-Agent (workbench)
绕过防分享与习惯指纹追踪。只有数据库明确标记为 is_test=1 的 Key 才能获得豁免。
"""
import pytest
from app.share_guard import is_internal_origin


def test_internal_origin_rejects_spoofed_user_agent():
    # 外部公网 IP 即使将 UA 伪造为包含 workbench，也绝不能被判定为 internal origin
    assert not is_internal_origin("1.2.3.4", "Mozilla/5.0 workbench/1.0")
    assert not is_internal_origin("203.0.113.195", "NovelAI-Workbench")
    assert not is_internal_origin("114.114.114.114", "workbench")

    # 真实的内部私网 IP 必须正确识别
    assert is_internal_origin("127.0.0.1", "any-agent")
    assert is_internal_origin("172.30.0.10", "any-agent")
    assert is_internal_origin("10.0.0.5", "any-agent")
    assert is_internal_origin("192.168.1.100", "any-agent")


def test_main_is_test_call_logic():
    from app.main import _row_flag

    normal_key = {"id": 1, "is_test": 0, "is_admin": 0}
    test_key = {"id": 2, "is_test": 1, "is_admin": 0}
    admin_key = {"id": 3, "is_test": 0, "is_admin": 1}

    assert not _row_flag(normal_key, "is_test")
    assert not _row_flag(normal_key, "is_admin")

    assert _row_flag(test_key, "is_test")
    assert _row_flag(admin_key, "is_admin")
