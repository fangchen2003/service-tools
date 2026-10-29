"""Read-only check of actual served hero bytes, not just HTML/version strings."""
import argparse
import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, parse_qs
from urllib.request import urlopen

class HeroParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hero = None
        self.entries = 0
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'img' and 'art-image' in (a.get('class') or '').split():
            self.hero = a
        if tag == 'a' and a.get('href') == '/workbench/':
            self.entries += 1

def check(origin, expected_file):
    expected = Path(expected_file).read_bytes()
    digest = hashlib.sha256(expected).hexdigest()
    with urlopen(origin.rstrip('/')+'/', timeout=20) as response:
        html = response.read().decode()
    parser = HeroParser(); parser.feed(html)
    assert parser.hero is not None, 'Missing hero'
    assert parser.entries == 1, 'Workbench entry count changed'
    image = parser.hero
    image_url = urljoin(origin, image['src'])
    assert urlsplit(image_url).netloc == urlsplit(origin).netloc
    with urlopen(image_url, timeout=20) as response:
        actual = response.read()
        assert response.headers.get_content_type() == 'image/webp'
    actual_digest = hashlib.sha256(actual).hexdigest()
    assert actual == expected, f'Stale served artwork: expected {digest}, got {actual_digest}'
    assert parse_qs(urlsplit(image_url).query)['v'] == [digest[:12]]
    assert (image['width'], image['height']) == ('1536', '1024')
    return {'origin':origin,'image_url':image_url,'image_sha256':digest,'image_bytes':len(actual),'workbench_entries':parser.entries}

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--origin', default='https://gate.example.com')
    parser.add_argument('--expected', default=str(Path(__file__).resolve().parents[1]/'app/static/gate-atelier-v1.webp'))
    args = parser.parse_args()
    print(json.dumps(check(args.origin, args.expected), ensure_ascii=False, indent=2))
