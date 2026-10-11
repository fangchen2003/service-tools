from pathlib import Path
import re
import subprocess
import tempfile
import pytest

from app.status_stats import tracked


def test_status_stats_tracks_official_nai_and_v1_paths():
    # 常用出图与客户端路径均须被 status_stats 统计
    assert tracked("/ai/generate-image")
    assert tracked("/user/data")
    assert tracked("/v1/chat/completions")
    # /nai/ai/ 前缀由部分生图客户端（如 SillyTavern / 官方客户端）使用，双挂载在 main.py，必须纳入追踪
    assert tracked("/nai/ai/generate-image")
    assert tracked("/nai/ai/upscale")
    assert tracked("/nai/ai/encode-vibe")

    # 非成员调用（健康检查、静态文件、管理后台）不计入状态码统计
    assert not tracked("/healthz")
    assert not tracked("/public/live")
    assert not tracked("/status")
    assert not tracked("/admin")
    assert not tracked("/static/landing.html")


def test_landing_html_clean_and_valid():
    landing_path = Path("app/static/landing.html")
    assert landing_path.exists()
    content = landing_path.read_text(encoding="utf-8")

    # 死代码 bjMinute 已清理
    assert "bjMinute" not in content

    # 死代码：未使用的 SVG 图标已清理
    p_block = re.search(r"var P = \{(.*?)\};", content, re.DOTALL)
    assert p_block is not None
    for dead_icon in ["shield", "eye", "eyeoff", "search", "lock", "check", "sparkle"]:
        assert re.search(rf"\b{dead_icon}\s*:", p_block.group(1)) is None

    # 假数字与原图保留为 0 天时的文案校准（不能用 || 3，0 表示不保留原图）
    assert "(d.image_retention_days||3)" not in content
    assert "retDays === 0" in content

    # 额度为 0（不限）时不再显示 / 0，且 V5 不限时标注全站上限约束
    assert "lim45" in content
    assert "lim5" in content
    assert 'title="个人不限额度，仍受全站日上限约束"' in content

    # 10-11 站长：运行情况不再单独列「成员数」等 4 个数字，和下面的卡片重复；结论并进第一张卡
    assert 'id="kMem"' not in content and 'class="kpis"' not in content
    assert 'class="dtile dme verdict"' in content

    # 提取脚本并通过 node 语法校验
    scripts = re.findall(r"<script>(.*?)</script>", content, re.DOTALL)
    assert len(scripts) >= 2
    for s in scripts:
        with tempfile.NamedTemporaryFile("w", suffix=".js") as tmp:
            tmp.write(s)
            tmp.flush()
            res = subprocess.run(["node", "--check", tmp.name], capture_output=True, text=True)
            assert res.returncode == 0, f"JS syntax error: {res.stderr}"


def test_index_html_clean_and_valid():
    index_path = Path("app/static/index.html")
    assert index_path.exists()
    content = index_path.read_text(encoding="utf-8")

    # 死代码 memberLimitRatio / srcBadge 已清理
    assert "memberLimitRatio" not in content
    assert "function srcBadge" not in content

    # 未使用图标 clock 已清理
    icons_block = re.search(r"const ICONS\s*=\s*\{(.*?)\};", content, re.DOTALL)
    assert icons_block is not None
    assert "clock:" not in icons_block.group(1)

    # ensureMembers 确保完整载入成员与 Key 关联
    assert "async function ensureMembers(){" in content
    assert "await loadMembers()" in content

    # 新建 Key 功能权限单选框默认与 B5 规范及 openCreate 行为一致（custom 默认，而非 legacy）
    assert 'name="f_feat_mode" value="custom" checked' in content

    # 提取脚本并通过 node 语法校验
    scripts = re.findall(r"<script>(.*?)</script>", content, re.DOTALL)
    assert len(scripts) >= 1
    for s in scripts:
        with tempfile.NamedTemporaryFile("w", suffix=".js") as tmp:
            tmp.write(s)
            tmp.flush()
            res = subprocess.run(["node", "--check", tmp.name], capture_output=True, text=True)
            assert res.returncode == 0, f"JS syntax error: {res.stderr}"


@pytest.mark.asyncio
async def test_public_me_returns_legacy_field():
    from unittest.mock import AsyncMock, MagicMock
    from starlette.requests import Request
    from app.registration_routes import public_me

    req = MagicMock(spec=Request)
    req.cookies = {"nai_member": "valid_token"}
    req.app.state.gate.day.return_value = "2026-10-11"
    req.app.state.gate.db.get_counter = AsyncMock(return_value={
        "images": 15, "v5": 5, "legacy_free_images": 10
    })
    req.app.state.gate.db.list_coupons = AsyncMock(return_value=[])
    req.app.state.gate.db.audit_image_count = AsyncMock(return_value=2)
    req.app.state.gate.guard.queue_view.return_value = {"mine": []}

    mock_service = MagicMock()
    mock_service.registration_profile = AsyncMock(return_value={"id": "1234567890", "username": "alice"})
    mock_service.key_row_for = AsyncMock(return_value={
        "id": 1, "token": "nai-test-key", "name": "alice", "enabled": 1,
        "expires_at": 1800000000, "image_model_scope": "all",
        "daily_images": 100, "daily_v5": 20
    })
    req.app.state.registrar = mock_service

    # mock session decode
    import app.registration_routes as rr
    orig_session = rr._member_session
    rr._member_session = AsyncMock(return_value="1234567890")
    try:
        resp = await public_me(req)
        import json
        body = json.loads(resp.body)
        assert body["logged_in"] is True
        assert body["today"]["legacy"] == 10
        assert body["today"]["images"] == 15
        assert body["today"]["v5"] == 5
    finally:
        rr._member_session = orig_session
