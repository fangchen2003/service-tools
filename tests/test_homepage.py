from pathlib import Path
import asyncio
import httpx
from app import main

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_image_includes_current_homepage_assets():
    # The upstream build must be reproducible without a local patch-image base.
    dockerfile = (ROOT / 'Dockerfile').read_text()
    assert 'COPY app ./app' in dockerfile
    for asset in ('gate-atelier-v1.webp', 'home.html'):
        assert (ROOT / 'app/static' / asset).is_file()

def test_landing_contains_real_workbench_live_stats_and_usage_notes():
    html = (ROOT / 'app/static/home.html').read_text()
    assert html.count('href="/workbench/"') == 1
    assert '<a class="primary" href="/workbench/">' in html
    assert '/api/public/stats' in html
    assert 'v5_images_today' in html and 'shared_v5_used' in html
    assert 'shared_v5_limit' in html and 'medium_cost' in html
    assert '用户须知' in html and '非 NovelAI 官方网站' in html
    assert 'aria-live="polite"' in html
    assert '暂时无法更新' in html
    assert '<input' not in html
    assert 'prefers-reduced-motion' in html



def test_homepage_artwork_has_cache_version_and_updated_dimensions():
    import hashlib
    import re
    from urllib.parse import urlsplit, parse_qs
    from html import unescape
    html = (ROOT / 'app/static/home.html').read_text()
    image_match = re.search(r'<img class="art-image"[^>]+>', html)
    assert image_match is not None
    image = image_match.group()
    src_match = re.search(r'src="([^"]+)"', image)
    assert src_match is not None
    version = parse_qs(urlsplit(src_match.group(1)).query).get('v', [])
    expected = hashlib.sha256((ROOT / 'app/static/gate-atelier-v1.webp').read_bytes()).hexdigest()[:12]
    assert version == [expected]
    # The original hash URL was accidentally served with stale bytes; rotate
    # its cache generation without changing the approved artwork content.
    assert parse_qs(urlsplit(unescape(src_match.group(1))).query).get('rev') == ['2']
    assert 'width="1536" height="1024"' in image
    assert 'NOVELAI + GPT IMAGE' in html


def test_fresh_data_dir_serves_packaged_homepage_at_root(tmp_path, monkeypatch):
    from app.config import Settings

    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(main, "SETTINGS", settings)
    assert list(tmp_path.iterdir()) == []

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://offline.test") as client:
            response = await client.get("/")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/html")
            assert response.text == (ROOT / "app/static/home.html").read_text(encoding="utf-8")
            assert 'href="/workbench/"' in response.text
            assert "/api/public/stats" in response.text
    asyncio.run(run())
    assert list(tmp_path.iterdir()) == []  # No persisted override is created.


def test_persisted_announcement_overrides_packaged_homepage(tmp_path, monkeypatch):
    from app.config import Settings

    settings = Settings(data_dir=tmp_path)
    custom = "<!doctype html><html><body>自定义公告 fixture</body></html>"
    settings.announcement_path.write_text(custom, encoding="utf-8")
    monkeypatch.setattr(main, "SETTINGS", settings)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://offline.test") as client:
            response = await client.get("/")
            assert response.status_code == 200
            assert response.text == custom
    asyncio.run(run())
    assert settings.announcement_path.read_text(encoding="utf-8") == custom


def test_landing_art_route_is_available_without_key():
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://offline.test') as c:
            response = await c.get('/assets/gate-atelier-v1.webp')
            assert response.status_code == 200
            assert response.headers['content-type'] == 'image/webp'
            assert response.content.startswith(b'RIFF')
    asyncio.run(run())
