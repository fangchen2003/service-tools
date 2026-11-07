"""Registration markup contracts; offline Chromium behavior is in integration/check_registration_ui.cjs."""
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app/static"


class Markup(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.elements = []
        self.ids = {}
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs))
        if "id" in attrs:
            assert attrs["id"] not in self.ids
            self.ids[attrs["id"]] = (tag, attrs)


def test_home_verification_under_faq_without_exposing_invite():
    html = (STATIC / "home.html").read_text()
    parsed = Markup(html)
    links = [attrs for tag, attrs in parsed.elements if tag == "a"]
    assert len([a for a in links if a.get("href") == "/self-register/verify"]) == 1
    assert len([a for a in links if a.get("href") == "/workbench/"]) == 1
    assert "Discord 身份验证" in html
    assert "验证指定身份组后加入服务器，再使用" in html
    assert "领取 Key" in html
    assert "discord.gg" not in html and "discord.com/invite" not in html
    assert html.index("如何领取或找回 Key？") < html.index('href="/self-register/verify"') < html.index('<ol class="rules">')


def test_admin_registration_controls_are_safe_until_read_and_assets_rotate():
    html = (STATIC / "index.html").read_text()
    parsed = Markup(html)
    assert 'data-setting-page="registration"' in html
    assert parsed.ids["registrationStatus"][1]["role"] == "status"
    for element in ("registrationSave", "registrationEnabled", "registrationDailyLimit"):
        assert "disabled" in parsed.ids[element][1]
    number = parsed.ids["registrationDailyLimit"][1]
    assert (number["type"], number["min"], number["max"], number["step"]) == ("number", "0", "100000", "1")
    assert "0 = 不限" in html and "近 30 天成功注册" in html
    for tag, attrs in parsed.elements:
        url = attrs.get("src", attrs.get("href", ""))
        if "/gate.js?" in url:
            assert "20261107-queue-metrics-model-scope-registration" in url
        if "/gate.css?" in url:
            assert "queue-metrics-registration" in url
