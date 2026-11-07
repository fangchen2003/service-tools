"""Offline Chromium UI check. All requests are intercepted; no server or credentials.
Run: python integration/check_queue_metrics_ui.py
Node VM lifecycle/race tests: node --test integration/check_queue_metrics_ui.cjs
"""
import json
import mimetypes
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app/static"


def fixture(hours: int) -> dict[str, Any]:
    now = int(time.time())
    summary = dict(enqueued=80, dispatched=75, finished=74, timed_out=5, cancelled=1,
                   expired=0, interrupted=0, active_keys=12, wait_p50_seconds=0,
                   wait_p95_seconds=45.25, wait_max_seconds=90, wait_avg_seconds=12,
                   timeout_rate=.0625, timeout_denominator=80, samples=7200,
                   observed_seconds=36000, coverage_ratio=.75, busy_ratio=.6, unavailable_ratio=0,
                   queue_max=9, queue_avg=2)
    return dict(generated_at=now, time_zone="Asia/Shanghai", window_hours=hours,
                since=now-hours*3600, started_at=now-36000, retention_days=30,
                sample_interval_seconds=5, health=dict(pending_events=0, dropped_events=0,
                last_error=None, last_sample_at=now), summary=summary,
                hourly=[dict(hour_start=now-i*3600, **summary) for i in range(hours)],
                current=dict(queued=3, ready=1, active=2, total=6, capacity=4),
                assessment=dict(level="roomy", label="余量较宽松", reason="等待较短，继续观察高峰。"))


def check():
    mode = "populated"
    metric_reads, errors, forbidden = [], [], []

    def route_request(route):
        request = route.request
        url = urlparse(request.url)
        assert url.hostname == "queue-ui.invalid", request.url
        assert request.method == "GET", request.method
        path = url.path
        if path.startswith("/admin/assets/"):
            file = STATIC / "assets" / path.rsplit("/", 1)[-1]
            assert file.is_file(), path
            route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file)[0] or "application/octet-stream")
            return
        if path == "/admin":
            route.fulfill(body=(STATIC / "index.html").read_bytes(), content_type="text/html")
            return
        if path == "/admin/api/queue-metrics":
            hours = int(parse_qs(url.query)["hours"][0])
            metric_reads.append(hours)
            if mode == "error":
                route.fulfill(status=503, json={"detail": "offline fixture failure"})
                return
            data = fixture(hours)
            if mode in ("timezone", "invalid_timezone"):
                data["time_zone"] = "America/New_York" if mode == "timezone" else "Invalid/Zone"
            if mode == "empty":
                for key in data["summary"]:
                    data["summary"][key] = None if key.startswith("wait_") or key in ("timeout_rate", "busy_ratio", "queue_max", "queue_avg") else 0
                data.update(started_at=None, hourly=[])
                data["health"]["last_sample_at"] = None
                data["assessment"] = dict(level="insufficient", label="收集中", reason="尚未积累高峰观测数据。")
            route.fulfill(json=data, headers={"Cache-Control": "no-store"})
            return
        if path == "/admin/api/me":
            data = {"ok": True}
        elif path == "/admin/api/allowance":
            data = {"accounts": [], "threshold": 20}
        elif path == "/admin/api/reconciliation":
            data = dict(csrf_token="fixture-only", running=False, retry_after=0, history=[])
        elif path == "/admin/api/settings":
            data = {"v5_auto_medium": False}
        elif path == "/admin/api/overview":
            data = dict(today=dict(images=4, anlas=0, v5=0, requests=4, text_tokens=0),
                        month=dict(anlas=0), keys_active=1, keys_total=1, week=[
                            dict(day=f"2026-11-0{i}", images=i, v45_images=i, v5_images=0, anlas=0)
                            for i in range(1, 8)], pool=[], pool_configured=False, v5_limit=50)
        elif path == "/admin/api/keys":
            data = {"keys": []}
        else:
            forbidden.append(request.url)
            route.abort()
            return
        route.fulfill(json=data)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"],
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"))
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", route_request)
        page.goto("http://queue-ui.invalid/admin#overview")
        page.wait_for_function("document.querySelector('#queueMetrics').dataset.state === 'ready'")
        assert "0 秒" in page.locator("#queueMetricsSummary").inner_text()
        assert "45.3 秒" in page.locator("#queueMetricsSummary").inner_text()
        assert "60.0%" in page.locator("#queueMetricsSummary").inner_text()
        assert page.locator(".gate-chart-bars > div").count() == 7
        for theme in ("light", "dark"):
            page.evaluate("theme => document.documentElement.dataset.theme=theme", theme)
            for width in (1280, 640, 390, 320):
                page.set_viewport_size({"width": width, "height": 900})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (theme, width)
                grid = page.locator("#queueMetricsSummary").evaluate("el => getComputedStyle(el).gridTemplateColumns.split(' ').length")
                assert grid == (4 if width > 640 else 2), (theme, width, grid)
                page.locator(".gate-queue-hours summary").click()
                assert page.locator(".gate-queue-hours .gate-table-scroll").evaluate("el => el.clientHeight <= 340")
                page.locator(".gate-queue-hours summary").click()
        page.select_option("#queueMetricsWindow", "168")
        page.wait_for_function("document.querySelector('#queueMetricsMeta').textContent.includes('7 天')")
        assert page.locator("#queueMetricsHourly tr").count() == 168
        mode = "error"
        page.locator("#refreshView").click()
        page.wait_for_function("document.querySelector('#queueMetrics').dataset.state === 'stale'")
        assert "上次记录" in page.locator("#queueMetricsStatus").inner_text()
        assert page.locator("#queueMetricsAssessment").get_attribute("data-level") != "roomy"
        assert "45.3 秒" in page.locator("#queueMetricsSummary").inner_text()
        assert "今日生成" in page.locator("#ovCards").inner_text()
        mode = "empty"
        page.locator("#refreshView").click()
        page.wait_for_function("document.querySelector('#queueMetrics').dataset.state === 'collecting'")
        assert "数据不足" in page.locator("#queueMetricsAssessment").inner_text()
        assert "0 秒" not in page.locator("#queueMetricsSummary").inner_text()
        assert "—" in page.locator("#queueMetricsSummary").inner_text()
        page.evaluate("syncQueueMetricsPolling()")
        assert page.locator("#queueMetrics").get_attribute("data-state") == "collecting"
        for hidden in (True, False):
            state = page.evaluate("""hidden => {
                Object.defineProperty(document, 'hidden', {configurable:true, value:hidden});
                document.dispatchEvent(new Event('visibilitychange'));
                return document.querySelector('#queueMetrics').dataset.state;
            }""", hidden)
            assert state == "collecting", (hidden, state)
        page.wait_for_function("queueMetricsRequest === null")
        page.evaluate("delete document.hidden")
        assert page.locator("#queueMetrics").get_attribute("data-state") == "collecting"
        expired_state = page.evaluate("""() => {
            const originalNow=Date.now;
            try {
                Date.now=()=> (queueMetricsSnapshot.generated_at+61)*1000;
                syncQueueMetricsPolling();
                return document.querySelector('#queueMetrics').dataset.state;
            } finally { Date.now=originalNow; }
        }""")
        assert expired_state == "stale"
        assert page.locator("#queueMetricsAssessment").get_attribute("data-level") != "roomy"
        for mode, zone in (("timezone", "America/New_York"), ("invalid_timezone", "Asia/Shanghai")):
            page.locator("#refreshView").click()
            page.wait_for_function("""zone => document.querySelector('#queueMetrics').dataset.state === 'ready'
                && document.querySelector('#queueMetricsMeta').textContent.includes('（'+zone+'）')""", arg=zone)
            assert page.evaluate("""zone => {
                const d=queueMetricsSnapshot;
                const fmt=n=>new Date(n*1000).toLocaleString('zh-CN',{timeZone:zone,hour12:false});
                const meta=document.querySelector('#queueMetricsMeta').textContent;
                return meta.includes('开始记录 '+fmt(d.started_at)) && meta.includes('更新 '+fmt(d.generated_at))
                    && document.querySelector('#queueMetricsHealth').textContent.includes('最近采样 '+fmt(d.health.last_sample_at))
                    && document.querySelector('#queueMetricsHourly td').textContent === fmt(d.hourly[0].hour_start);
            }""", zone), zone
        mode = "error"
        page.select_option("#queueMetricsWindow", "24")
        page.wait_for_function("document.querySelector('#queueMetrics').dataset.state === 'unavailable'")
        assert "暂不可用" in page.locator("#queueMetricsStatus").inner_text()
        assert "45.3 秒" not in page.locator("#queueMetricsSummary").inner_text()
        page.evaluate("tab('keys')")
        page.wait_for_function("currentTab === 'keys' && queueMetricsTimer === null")
        assert not errors, errors
        assert not forbidden, forbidden
        version = browser.version
        browser.close()
    return dict(browser=version, metric_reads=metric_reads, page_errors=errors,
                forbidden_requests=forbidden, themes=["light", "dark"], widths=[1280, 640, 390, 320],
                states=["populated", "stale", "empty", "empty_expired", "unavailable"], hourly_rows=168,
                empty_lifecycle=["polling_sync", "hidden", "visible", "response_expiry"],
                timezones=["Asia/Shanghai", "America/New_York", "invalid_fallback"])


if __name__ == "__main__":
    print(json.dumps(check(), ensure_ascii=False))
