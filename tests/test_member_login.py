"""成员网页登录：会话 Cookie 的签名 / 校验 / 过期 / 防篡改。"""
import time
from types import SimpleNamespace

from app import registration_routes as mr


class FakeReq:
    def __init__(self, cookies=None, secret="x" * 40):
        self.cookies = cookies or {}
        self.app = SimpleNamespace(state=SimpleNamespace(
            gate=SimpleNamespace(settings=SimpleNamespace(secret_key=secret))))


def test_member_cookie_roundtrip_and_tamper():
    req = FakeReq()
    exp = int(time.time()) + 3600
    cookie = mr._sign_member(req, "12345", exp)
    assert mr._parse_member(FakeReq({mr.MEMBER_COOKIE: cookie})) == ("12345", "-")
    # 篡改 discord_id → 签名不符 → 拒绝
    payload, sig = cookie.rsplit(".", 1)
    forged = payload.replace("12345", "99999") + "." + sig
    assert mr._parse_member(FakeReq({mr.MEMBER_COOKIE: forged})) is None
    # 过期
    old = mr._sign_member(req, "12345", int(time.time()) - 1)
    assert mr._parse_member(FakeReq({mr.MEMBER_COOKIE: old})) is None
    # 没有 Cookie
    assert mr._parse_member(FakeReq()) is None
    # 换密钥（相当于另一台服务器）→ 不认
    assert mr._parse_member(FakeReq({mr.MEMBER_COOKIE: cookie}, secret="y" * 40)) is None
