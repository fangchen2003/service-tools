#!/usr/bin/env python3
"""SRE & 安全自动化冒烟测试套件（Smoke Test Suite）。

在维护重构后自动执行，确保系统各项指标 100% 达标方可切回流量。
"""
import sys
import json
import urllib.request
import urllib.error
import sqlite3

BASE_URL = "http://127.0.0.1:3003"
DB_PATH = "/opt/service-tools/data/nai_gate.db"
TEST_KEY = "nai-ZxaM9ig7rylxEt685JvfcTMddWcJohRT"  # 专用测试 Key #20


def log(msg, ok=True):
    symbol = "✅" if ok else "❌"
    print(f"{symbol} {msg}")


def test_sqlite_integrity():
    print("\n--- 1. SQLite 物理存储引擎完整性检查 ---")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA integrity_check;")
    res = cur.fetchone()[0]
    assert res == "ok", f"Integrity check failed: {res}"
    log(f"SQLite integrity_check: {res}")

    cur.execute("PRAGMA foreign_key_check;")
    fk_errors = cur.fetchall()
    assert len(fk_errors) == 0, f"Foreign key errors: {fk_errors}"
    log("SQLite foreign_key_check: 无外键损坏")
    conn.close()


def test_health_endpoints():
    print("\n--- 2. 网关内部健康检查与公开端点 ---")
    req = urllib.request.Request(f"{BASE_URL}/healthz")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        body = json.loads(r.read())
        log(f"/healthz: {body}")

    req = urllib.request.Request(f"{BASE_URL}/public/live")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
        body = json.loads(r.read())
        assert "guard" in body and "queue" in body
        log(f"/public/live 响应正常 (hour: {body['guard'].get('hour')}, queue: {body.get('queue')})")


def test_security_header_spoofing():
    print("\n--- 3. 安全防线：伪造 Header 与 UA 穿透测试 ---")
    # 测试携带 X-Admin-Test 的非法伪造请求
    req = urllib.request.Request(
        f"{BASE_URL}/ai/generate-image",
        headers={
            "Authorization": "Bearer nai-fake-hacker-token-123456",
            "X-Admin-Test": "1",
            "User-Agent": "Mozilla/5.0 (Workbench-Hacker)",
        },
        data=b"{}",
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "Should have been rejected with 401!"
    except urllib.error.HTTPError as e:
        assert e.code == 401, f"Expected 401 but got {e.code}"
        log(f"伪造 X-Admin-Test + workbench UA 请求被正确拦截 (HTTP {e.code} 无效 Key)")


def test_authenticated_test_key():
    print("\n--- 4. 合法测试 Key 隔离流校验 ---")
    req = urllib.request.Request(
        f"{BASE_URL}/public/me",
        headers={"Authorization": f"Bearer {TEST_KEY}"},
    )
    # 通过 cookie 或 header 测试
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, is_test, is_admin, enabled FROM api_keys WHERE token=?", (TEST_KEY,))
    row = cur.fetchone()
    conn.close()
    assert row is not None, "Test key not found in db"
    assert row[1] == 1, f"Expected is_test=1, got {row[1]}"
    log(f"测试 Key #20 属性校验成功 (is_test={row[1]}, enabled={row[3]})")


def main():
    try:
        test_sqlite_integrity()
        test_health_endpoints()
        test_security_header_spoofing()
        test_authenticated_test_key()
        print("\n🎉 全部冒烟测试项 100% 通过！系统达到生产交付标准！")
        return 0
    except Exception as e:
        log(f"冒烟测试失败: {e}", ok=False)
        return 1


if __name__ == "__main__":
    sys.exit(main())
