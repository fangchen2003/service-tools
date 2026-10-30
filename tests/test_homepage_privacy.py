"""Offline regressions for the packaged homepage's deployment-neutral identity."""
from pathlib import Path
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import runpy
import shutil
import subprocess
import sys
import threading
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / "app/static/home.html"


def test_homepage_uses_generic_brand_without_site_identity():
    html = HOME.read_text(encoding="utf-8")
    assert "<title>NAI Gate — 让灵感成画</title>" in html
    assert 'aria-label="NAI Gate 首页"' in html
    assert re.search(r'<span class="brand-name">NAI Gate', html)
    assert '<span class="foot-brand">NAI Gate</span>' in html
    assert '<meta name="description" content="NAI Gate。' in html


# Run the actual complete inline script in Node's VM. Only browser platform
# boundaries are simulated; no rewritten copy/display implementation or network.
NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
(async () => {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const nodes = Object.fromEntries(input.ids.map(id => [id, {
    textContent: '', hidden: false, style: {}, dataset: {}, attrs: {}, events: {},
    setAttribute(name, value) { this.attrs[name] = value; },
    addEventListener(name, callback) { this.events[name] = callback; }
  }]));
  const copies = [], requests = [];
  const now = Date.now()/1000;
  const context = {
    window: {location: new URL(input.origin)},
    document: {hidden: false, getElementById: id => nodes[id], addEventListener() {}},
    navigator: {clipboard: {async writeText(value) {
      if (input.rejectClipboard) throw new Error('permission denied');
      copies.push(value);
    }}},
    fetch: async (url) => {
      requests.push(url);
      if (url !== '/api/public/stats') throw new Error('unexpected request');
      return {ok: true, async json() { return {
        v5_images_today: 12, shared_v5_used: 3, shared_v5_limit: 10,
        medium_cost: 1, personal_v5_limit: 5, personal_legacy_limit: 8,
        refreshed_at: now, reset_at: now + 3600, day: 'fixture', timezone: 'UTC'
      }; }};
    },
    AbortSignal, Intl, Date,
    setTimeout() {}, clearTimeout() {}, setInterval() {}
  };
  vm.runInNewContext(input.script, context);
  await new Promise(resolve => setImmediate(resolve));
  await nodes['copy-api'].events.click();
  process.stdout.write(JSON.stringify({
    displayed: nodes['api-origin']?.textContent,
    aria: nodes['copy-api'].attrs['aria-label'],
    copies, toast: nodes.toast.textContent, requests,
    images: nodes.images.textContent, state: nodes['live-state'].dataset.state
  }));
})().catch(error => { console.error(error); process.exit(1); });
"""


@pytest.mark.parametrize("origin", [
    "https://offline.test", "http://localhost:43817", "https://sub.offline.test:9443",
])
@pytest.mark.parametrize("reject_clipboard", [False, True])
def test_api_display_copy_and_failure_use_current_origin(origin, reject_clipboard):
    node = shutil.which("node")
    assert node, "Node is required to execute the homepage privacy regression"
    html = HOME.read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    result = subprocess.run(
        [node, "-e", NODE_HARNESS],
        input=json.dumps({
            "ids": re.findall(r'id="([^"]+)"', html),
            "script": "\n".join(scripts), "origin": origin,
            "rejectClipboard": reject_clipboard,
        }), text=True, capture_output=True, check=True,
    )
    actual = json.loads(result.stdout)
    assert actual.get("displayed") == origin
    assert actual["aria"] == f"复制 API 地址 {origin}"
    assert actual["copies"] == ([] if reject_clipboard else [origin])
    assert actual["toast"] == (
        f"无法自动复制，请手动复制 {origin}" if reject_clipboard else "API 地址已复制"
    )
    assert actual["requests"] == ["/api/public/stats"]
    assert actual["images"] == "12" and actual["state"] == "ok"


def test_api_markup_is_safe_without_javascript():
    html = HOME.read_text(encoding="utf-8")
    assert '<code id="api-origin">当前站点地址</code>' in html
    assert 'aria-label="复制当前站点 API 地址"' in html
    # The SVG namespace is an identifier, not a deployment destination.
    assert re.findall(r"https?://[^\s'\"<>]+", html) == ["http://www.w3.org/2000/svg"]


def test_artifact_cli_requires_explicit_origin_without_network(monkeypatch, capsys):
    script = ROOT / "integration/check_homepage_artifact.py"
    requests = []

    def forbidden_network(*args, **kwargs):
        requests.append(args)
        raise AssertionError("An omitted --origin must never attempt a request")

    monkeypatch.setattr(sys, "argv", [str(script)])
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_network)
    # Ensure this catches a network-capable default even if the old default
    # artwork path no longer exists after a rename.
    monkeypatch.setattr(Path, "read_bytes", lambda self: b"fixture")
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(script), run_name="__main__")
    assert exit_info.value.code == 2
    assert "--origin" in capsys.readouterr().err
    assert requests == []


def test_artifact_cli_checks_local_served_bytes_with_renamed_default():
    requests = []
    artwork = ROOT / "app/static/gate-atelier-v1.webp"
    image_path = "/assets/gate-atelier-v1.webp?v=756417852335&rev=2"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == "/":
                content, media_type = HOME.read_bytes(), "text/html"
            elif self.path == image_path:
                content, media_type = artwork.read_bytes(), "image/webp"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", media_type)
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            result = subprocess.run(
                [sys.executable, str(ROOT / "integration/check_homepage_artifact.py"),
                 "--origin", origin],
                text=True, capture_output=True, timeout=30,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "origin": origin, "image_url": origin + image_path,
        "image_sha256": "756417852335f26466188fa1a26c723bbe29e0d4be3162f75653460b337037de",
        "image_bytes": artwork.stat().st_size, "workbench_entries": 1,
    }
    assert requests == ["/", image_path]
