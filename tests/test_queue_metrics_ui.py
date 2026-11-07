"""Offline markup and contract checks; JS behavior lives in the Node VM probe."""
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app/static"


def test_queue_metrics_javascript_lifecycle_and_timezone_regressions():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the offline queue metrics UI probe")
    result = subprocess.run(
        [node, "--test", "integration/check_queue_metrics_ui.cjs"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = {}
        self.assets = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            assert attrs["id"] not in self.ids
            self.ids[attrs["id"]] = (tag, attrs)
        if tag in ("script", "link"):
            self.assets.append(attrs.get("src", attrs.get("href", "")))


def test_queue_metrics_overview_panel_and_accessibility():
    html = (STATIC / "index.html").read_text()
    parsed = Markup()
    parsed.feed(html)
    assert "queueMetrics" in parsed.ids
    for element in ("queueMetricsStatus", "queueMetricsSummary", "queueMetricsMeta",
                    "queueMetricsAssessment", "queueMetricsHourly", "queueMetricsCurrent", "queueMetricsHealth"):
        assert element in parsed.ids
    assert parsed.ids["queueMetricsStatus"][1]["role"] == "status"
    assert parsed.ids["queueMetricsWindow"][1]["aria-label"]
    assert 'value="24"' in html and 'value="168"' in html
    assert html.index('id="chart"') < html.index('id="queueMetrics"') < html.index('id="pool"')
    for phrase in ("排队与扩容观测", "不含生成耗时", "GPU", "不代表用户数", "不保证生成成功",
                   "部署后", "不回填", "不会自动", "6 小时", "50", "取消", "过期"):
        assert phrase in html


def test_queue_metrics_assets_rotate_cache_and_keep_theme_responsive():
    html = (STATIC / "index.html").read_text()
    parsed = Markup()
    parsed.feed(html)
    for asset in ("gate.css", "gate.js"):
        urls = [url for url in parsed.assets if "/" + asset + "?" in url]
        assert len(urls) == 1
        assert "queue-metrics" in urls[0]
    css = (STATIC / "assets/gate.css").read_text()
    assert ".gate-queue-metrics" in css
    assert ".gate-queue-summary" in css
    assert "var(--" in css[css.index(".gate-queue-metrics"):]
    assert "@media(max-width:640px)" in css[css.index(".gate-queue-metrics"):]
    assert "max-height:" in css[css.index(".gate-queue-metrics"):]
