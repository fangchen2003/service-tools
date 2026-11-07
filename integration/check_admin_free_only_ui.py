"""Offline Chromium admin regression: real DOM/assets, intercepted fixture APIs only.
Run with a Python environment containing Playwright + Chromium, from repository root.
No credentials, real server, upstream calls, or production database are used.
"""
import argparse
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright
from check_queue_metrics_ui import fixture as queue_metrics_fixture
from check_key_status_ui import check_key_status_ui


def check(root: Path):
    static = root / "app/static"
    config = dict(allow_img2img=True, safe_clamp=False, max_pixels=1048576, max_steps=20,
                  default_image_model_scope="all", default_daily_images=100, default_daily_v5=50,
                  default_daily_anlas=73, default_monthly_anlas=2345, default_allow_anlas=False,
                  default_daily_text_tokens=150000, default_rpm=10, default_expires_days=30,
                  default_exclude_global_v5=True, default_allow_img2img=True,
                  key_inactivity_delete_days=0, global_concurrency=2, key_concurrency=2,
                  max_text_output_tokens=2048, max_input_chars=36000)
    settings = dict(global_monthly_anlas=9876, global_daily_v5=150, v5_auto_medium=False,
                    v5_medium_threshold=20, v5_medium_multiplier=0.6, v5_alert_threshold=20)
    key = dict(id=7, name="offline legacy flags", token="nai-fixture-only-not-a-secret", enabled=True,
               daily_images=100, daily_anlas=73, daily_v5=50, monthly_anlas=2345,
               daily_text_tokens=150000, rpm=10, allow_anlas=True, allow_img2img=True,
               exclude_global_v5=True, image_model_scope="all", is_admin=False,
               expires_at=None, last_used_at=None,
               used=dict(images=2, anlas=6, v5=1, text_tokens=0, legacy_free_images=1))
    writes, errors, failures = [], [], []
    account = dict(token_id="fixture", position=1, token="masked-fixture", admin_enabled=True,
                   usable=True, busy=False, disabled=False, blocked_for=0,
                   v5_daily_limit=150, image_concurrency=1, allow_anlas=True)

    def snapshot():
        return dict(values=config, sources={k: "panel" for k in config}, csrf_token="fixture-csrf",
                    image_policy=dict(free_max_steps_by_tier=dict(v5=config["max_steps"], legacy=config["max_steps"])))

    def route_request(route):
        request = route.request
        url = urlparse(request.url)
        assert url.hostname == "gate-ui.invalid", request.url
        path = url.path
        if path.startswith("/admin/assets/"):
            file = static / "assets" / path.rsplit("/", 1)[-1]
            assert file.is_file(), path
            content_type = "text/javascript" if file.suffix == ".js" else "text/css" if file.suffix == ".css" else "font/woff2"
            route.fulfill(body=file.read_bytes(), content_type=content_type)
            return
        if path == "/admin":
            route.fulfill(body=(static / "index.html").read_bytes(), content_type="text/html")
            return
        body = request.post_data_json if request.post_data else {}
        if request.method not in ("GET", "HEAD"):
            writes.append((path, body))
        if path == "/admin/api/configuration":
            if request.method == "PUT":
                config.update(body)
            result = snapshot()
        elif path == "/admin/api/settings":
            if request.method == "PUT":
                settings.update(body)
            result = settings
        elif path == "/admin/api/keys/7":
            key.update(body)
            result = {"ok": True}
        elif path == "/admin/api/keys":
            result = {"keys": [key]} if request.method == "GET" else {"key": dict(key, token="nai-new-offline-fixture")}
        elif path == "/admin/api/announcement":
            result = {"html": ""}
        elif path == "/admin/api/runtime-limits":
            result = dict(queue_timeout=90, key_image_min_interval=15, image_min_interval=15, image_429_cooldown_seconds=60)
        elif path == "/admin/api/upstream-tokens":
            result = {"accounts": [account], "source": "panel"}
        elif path == "/admin/api/upstream-tokens/fixture/allow-anlas":
            account.update(body)
            result = {"ok": True}
        elif path == "/admin/api/allowance":
            result = {"accounts": [], "threshold": 20}
        elif path == "/admin/api/me":
            result = {"ok": True}
        elif path == "/admin/api/queue-metrics":
            result = queue_metrics_fixture(int(parse_qs(url.query).get("hours", ["24"])[0]))
        elif path == "/admin/api/reconciliation":
            result = dict(csrf_token="fixture-csrf", running=False, retry_after=0, history=[])
        elif path == "/admin/api/overview":
            result = dict(today=dict(images=2, anlas=6, v5=1, requests=3, text_tokens=0),
                          month=dict(anlas=6), week=[
                              dict(day=f"2026-10-{25+i}", images=0 if i == 1 else 14,
                                   v45_images=0 if i == 1 else 5, v5_images=0 if i == 1 else 8,
                                   anlas=0 if i == 1 else 12.5) for i in range(7)
                          ], pool=[account], pool_configured=True,
                          v5_limit=150, keys_active=1, keys_total=1, anlas_budget=9876)
        else:
            raise AssertionError("Unexpected offline UI request: " + request.method + " " + path)
        route.fulfill(json=result)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("requestfailed", lambda request: failures.append(request.url))
        page.route("**/*", route_request)
        page.goto("http://gate-ui.invalid/admin#overview")
        bars = page.locator(".gate-chart-bars > div")
        bars.first.wait_for()
        assert bars.count() == 7
        for width in (1280, 640, 390, 320):
            page.set_viewport_size({"width": width, "height": 900})
            for index in range(7):
                bar = bars.nth(index)
                bar.hover()
                tooltip = bar.locator(".gate-chart-tooltip")
                assert tooltip.is_visible()
                content = " ".join(tooltip.inner_text().split())
                if index == 1:
                    assert "V4.5 0 张" in content and "V5 0 张" in content
                else:
                    assert "V4.5 5 张" in content and "V5 8 张" in content
                    assert "Anlas 使用量 12.5" in content
                    assert "其他图片 1 张" in content
                bounds = tooltip.bounding_box()
                panel = page.locator(".gate-chart-section").bounding_box()
                assert bounds["x"] >= panel["x"] and bounds["x"] + bounds["width"] <= panel["x"] + panel["width"], (width, index, bounds)
                assert bounds["y"] >= panel["y"], (width, index, bounds)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.mouse.move(0, 0)
        bars.first.focus()
        assert bars.first.locator(".gate-chart-tooltip").is_visible()
        page.keyboard.press("Tab")
        assert bars.nth(1).locator(".gate-chart-tooltip").is_visible()
        page.set_viewport_size(dict(width=1280, height=900))
        page.goto("http://gate-ui.invalid/admin#settings")
        page.reload()
        page.wait_for_function("configurationSnapshot !== null")
        for selector in ("#s_budget", "#f_daily_anlas", "#f_monthly_anlas",
                         "#c_default_daily_anlas", "#c_default_monthly_anlas"):
            assert page.locator(selector).count() == 0, selector
        assert "High 最多 28 步" in page.locator("#settings-budget").inner_text()
        assert "/vibe" in page.locator("#settings-budget").inner_text()
        assert not page.locator("#s_medium_multiplier").is_disabled()  # PR7 Medium fix.
        assert page.locator("#s_medium_threshold").is_disabled()
        page.locator("#s_medium_multiplier").fill("0.45")
        page.get_by_role("button", name="保存设置", exact=True).filter(visible=True).click()
        assert settings["v5_medium_multiplier"] == 0.45
        assert settings["global_monthly_anlas"] == 9876
        page.evaluate("settingsPage('generation')")
        assert page.locator("#c_max_steps").input_value() == "20"
        assert page.locator("#c_max_steps").get_attribute("max") == "28"
        assert "High 和旧模型均取此值与 28 的较小值" in page.locator("#settings-generation").inner_text()
        page.evaluate("settingsPage('defaults')")
        page.locator("#c_default_rpm").fill("11")
        page.locator('[data-save-config="defaults"]').click()
        page.wait_for_function("configurationSnapshot.values.default_rpm === 11")
        assert (config["default_daily_anlas"], config["default_monthly_anlas"], config["default_allow_anlas"]) == (73, 2345, False)
        assert not page.locator("#c_default_allow_anlas").is_checked()
        # V5 defaults are dormant for legacy-only keys, not erased by hiding/saving.
        scope = page.locator("#c_default_image_model_scope")
        v5 = page.locator("#c_default_daily_v5")
        isolated = page.locator("#c_default_exclude_global_v5")
        assert v5.is_visible() and isolated.is_visible()
        v5.fill("61")
        scope.select_option("legacy")
        assert not v5.is_visible() and v5.is_disabled(), "legacy defaults must hide/disable V5 quota"
        assert not isolated.is_visible() and isolated.is_disabled(), "legacy defaults must hide/disable isolation"
        for width in (390, 320, 1280):
            page.set_viewport_size({"width": width, "height": 900})
            assert not v5.is_visible() and not isolated.is_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        scope.select_option("all")
        assert v5.input_value() == "61" and isolated.is_checked()
        scope.select_option("legacy")
        page.locator('[data-save-config="defaults"]').click()
        page.wait_for_function("configurationSnapshot.values.default_image_model_scope === 'legacy'")
        payload = [body for path, body in writes if path == "/admin/api/configuration"][-1]
        assert not {"default_daily_v5", "default_exclude_global_v5"} & payload.keys(), payload
        assert (config["default_daily_v5"], config["default_exclude_global_v5"]) == (50, True)
        page.reload()
        page.wait_for_function("configurationSnapshot !== null")
        page.evaluate("settingsPage('defaults')")
        assert not v5.is_visible() and v5.is_disabled()
        assert not isolated.is_visible() and isolated.is_disabled()
        scope.select_option("all")
        assert v5.is_visible() and not v5.is_disabled() and v5.input_value() == "50"
        assert isolated.is_visible() and not isolated.is_disabled() and isolated.is_checked()
        page.locator('[data-save-config="defaults"]').click()
        page.wait_for_function("configurationSnapshot.values.default_image_model_scope === 'all'")
        page.evaluate("tab('keys')")
        page.wait_for_function("keysCache.length === 1")
        check_key_status_ui(page)
        assert "允许 Anlas" in page.locator("#keysTable").inner_text()
        assert "6 Anlas" in page.locator("#keysTable").inner_text()  # Audit retained.
        page.get_by_role("button", name="编辑", exact=True).click()
        assert page.locator("#f_allow_anlas").is_checked()
        modal_scope = page.locator("#f_image_model_scope")
        modal_v5 = page.locator("#f_daily_v5")
        modal_isolated = page.locator("#f_exclude_global_v5")
        assert modal_v5.is_visible() and modal_isolated.is_visible()
        modal_v5.fill("62")
        modal_scope.select_option("legacy")
        assert not modal_v5.is_visible() and modal_v5.is_disabled(), "legacy key editor must hide V5 quota"
        assert not modal_isolated.is_visible() and modal_isolated.is_disabled()
        for width in (390, 320, 1280):
            page.set_viewport_size({"width": width, "height": 900})
            assert not modal_v5.is_visible() and not modal_isolated.is_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        modal_scope.select_option("all")
        assert modal_v5.input_value() == "62" and modal_isolated.is_checked()
        modal_scope.select_option("legacy")
        # A hidden required control must not block saving or be sent as an empty/zero quota.
        page.evaluate("document.querySelector('#f_daily_v5').value=''")
        page.locator("#f_allow_anlas").uncheck()
        page.locator("#f_name").fill("renamed fixture")
        page.locator("#modalSave").click()
        page.wait_for_function("!document.querySelector('#modal').open")
        assert (key["daily_anlas"], key["monthly_anlas"], key["allow_anlas"]) == (73, 2345, False)
        assert key["allow_img2img"] and key["exclude_global_v5"]
        payload = [body for path, body in writes if path == "/admin/api/keys/7"][-1]
        assert not {"daily_v5", "exclude_global_v5"} & payload.keys(), payload
        assert key["daily_v5"] == 50 and key["image_model_scope"] == "legacy"
        page.get_by_role("button", name="编辑", exact=True).click()
        assert not modal_v5.is_visible() and not modal_isolated.is_visible()
        modal_scope.select_option("all")
        assert modal_v5.is_visible() and modal_v5.input_value() == "50"
        assert modal_isolated.is_visible() and modal_isolated.is_checked()
        page.evaluate("closeModal()")
        config["default_image_model_scope"] = "legacy"
        page.evaluate("openCreate()")
        page.wait_for_function("document.querySelector('#modal').open")
        assert not page.locator("#f_allow_anlas").is_checked()
        assert not modal_v5.is_visible() and modal_v5.is_disabled()
        assert not modal_isolated.is_visible() and modal_isolated.is_disabled()
        modal_scope.select_option("all")
        assert modal_v5.is_visible() and modal_v5.input_value() == "50"
        assert modal_isolated.is_visible() and modal_isolated.is_checked()
        modal_scope.select_option("legacy")
        page.locator("#f_name").fill("new fixture")
        page.locator("#modalSave").click()
        page.wait_for_function("document.querySelector('#keyTokenDialog').open")
        payload = [body for path, body in writes if path == "/admin/api/keys"][-1]
        assert payload["image_model_scope"] == "legacy"
        assert not {"daily_v5", "exclude_global_v5"} & payload.keys(), payload
        page.evaluate("closeKeyToken(); settingsPage('upstream'); tab('set')")
        page.wait_for_function("document.querySelector('#account-anlas-fixture') !== null")
        page.locator("#account-anlas-fixture").uncheck()
        page.get_by_role("button", name="保存账号设置", exact=True).click()
        page.wait_for_function("upstreamAccounts[0].allow_anlas === false")
        assert not account["allow_anlas"]
        for path, body in writes:
            if path in ("/admin/api/keys", "/admin/api/keys/7"):
                assert not {"daily_anlas", "monthly_anlas"} & body.keys(), (path, body)
                assert body["allow_anlas"] is False
            if path == "/admin/api/configuration":
                assert not {"default_daily_anlas", "default_monthly_anlas"} & body.keys()
                if "default_allow_anlas" in body:
                    assert body["default_allow_anlas"] is False
            if path == "/admin/api/settings":
                assert "global_monthly_anlas" not in body
        assert not errors, errors
        assert not failures, failures
        browser.close()
    return dict(browser="Chromium", fixture_writes=len(writes), page_errors=errors,
                key_status="on/off/expired filters; current-key counts; search, renewal metadata and mobile verified",
                v5_scope_fields="defaults/create/edit: hidden+disabled for legacy, restored for all; dormant values preserved",
                failed_requests=failures, legacy_flags="preserved", upstream_allow_anlas="editable",
                chart_details="model images and Anlas; hover, keyboard, 320–1280px",
                ordinary_paid_permission="editable; new keys default off", numeric_anlas_editors="absent", medium_multiplier="editable with auto mode off")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    print(json.dumps(check(args.root), ensure_ascii=False))
