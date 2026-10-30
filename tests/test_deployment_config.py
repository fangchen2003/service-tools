"""Fail-closed deployment configuration contracts, with no network traffic."""
import json
import shutil
import subprocess
import unittest
from unittest.mock import Mock, patch

import httpx

from app.deployment import DeploymentConfig, safe_https_url
from app.registration import configured_service
from app.workbench_auth import configured_service as configured_auth
from test_domain_migration import ENV


# Static expectations also exercise the contract when Node is unavailable.
ORIGIN_CASES = (
    ("https://gate.example.com:443", "https://gate.example.com"),
    ("https://Gate.Example.COM:443", "https://gate.example.com"),
    ("https://Gate.Example.COM:08443/studio/", "https://gate.example.com:8443"),
    ("https://bücher.example:443", "https://xn--bcher-kva.example"),
    ("https://faß.example:8443", "https://xn--fa-hia.example:8443"),
    ("https://ＢÜＣＨＥＲ.example", "https://xn--bcher-kva.example"),
    ("https://[2001:0DB8:0:0::1]:443", "https://[2001:db8::1]"),
    ("https://[2001:db8::1]:8443/studio/", "https://[2001:db8::1]:8443"),
    ("https://[::ffff:192.0.2.1]", "https://[::ffff:c000:201]"),
    ("https://127.0.0.1:443", "https://127.0.0.1"),
    ("https://127.1", "https://127.0.0.1"),
    ("https://0x7f.0.0.1", "https://127.0.0.1"),
    ("https://0177.0.0.1", "https://127.0.0.1"),
    ("https://2130706433", "https://127.0.0.1"),
)


INVALID_BROWSER_URLS = (
    "https://gate.example.com|evil.example",
    "https://gate.example.com^evil.example",
    "https://gate.example.com<evil.example",
    "https://gate.example.com>evil.example",
    "https://gate.example.com[evil.example",
    "https://gate.example.com]evil.example",
    "https://gate.example.com:bad",
    "https://gate.example.com:65536",
    "https://gate.example.com:-1",
    "https://gate.example.com:４４３",
    "https://[::1]evil.example",
    "https://[::1]:443:8443",
    "https://[v1.example]",
    "https://[fe80::1%25eth0]",
    "https://256.0.0.1",
    "https://127.0.0.999",
    "https://1.2.3.4.5",
    "https://4294967296",
    "https://example.123",
    "https://127.0.0.09",
    "https://xn--.example",
    "https://\u200d.example",
    "https://gate.example.com\x00evil.example",
)

REJECTED_DEPLOYMENT_URLS = INVALID_BROWSER_URLS + (
    "https://@gate.example.com", "https://:@gate.example.com",
    "https://user@gate.example.com", "https://gate.example.com:",
    "https://gate.example.com:0", "https://[::1]:",
    "https://gate.example.com%7cevil.example",
    "https://gate.example.com?", "https://gate.example.com#",
    "https://gate.example.com\\evil", "https://gate.example.com\t",
)


class DeploymentConfigTests(unittest.TestCase):
    def test_invalid_authorities_are_rejected_without_repair(self):
        for url in REJECTED_DEPLOYMENT_URLS:
            with self.subTest(url=url):
                self.assertFalse(safe_https_url(url))
                config = DeploymentConfig(site_url=url, workbench_url=url,
                                          redirect_uri=ENV["DISCORD_REDIRECT_URI"])
                self.assertFalse(config.auth_ready)
                self.assertEqual(config.allowed_origins, frozenset())

    def test_invalid_authorities_disable_both_services_for_every_url_setting(self):
        for field in ("SITE_URL", "WORKBENCH_URL", "DISCORD_REDIRECT_URI"):
            for url in REJECTED_DEPLOYMENT_URLS:
                if "\x00" in url:  # The OS cannot store NUL in an environment value.
                    continue
                with self.subTest(field=field, url=url), patch.dict("os.environ", {**ENV, field: url}, clear=True):
                    self.assertIsNone(configured_service(None, Mock(spec=httpx.AsyncClient)))
                    self.assertIsNone(configured_auth(None, Mock(spec=httpx.AsyncClient)))

    @unittest.skipUnless(shutil.which("node"), "Node WHATWG URL oracle unavailable")
    def test_invalid_browser_authorities_are_rejected_by_node(self):
        result = subprocess.run(
            ["node", "-e", "console.log(JSON.stringify(JSON.parse(process.argv[1]).map(u => {"
             "try {new URL(u); return false} catch {return true}})))", json.dumps(INVALID_BROWSER_URLS)],
            check=True, capture_output=True, text=True, timeout=10)
        self.assertEqual(json.loads(result.stdout), [True] * len(INVALID_BROWSER_URLS))

    def test_valid_https_ports_and_path_escapes_remain_supported(self):
        for url, origin in (
            ("https://gate.example.com:1/a%20b", "https://gate.example.com:1"),
            ("https://gate.example.com:65535", "https://gate.example.com:65535"),
            ("https://Gate.Example.COM.:00443", "https://gate.example.com."),
        ):
            with self.subTest(url=url):
                self.assertTrue(safe_https_url(url))
                config = DeploymentConfig(site_url=url, redirect_uri=ENV["DISCORD_REDIRECT_URI"])
                self.assertTrue(config.auth_ready)
                self.assertEqual(config.allowed_origins, frozenset({origin}))

    def test_allowed_origins_use_browser_serialization_without_rewriting_urls(self):
        for url, origin in ORIGIN_CASES:
            with self.subTest(url=url):
                config = DeploymentConfig(site_url=url, workbench_url=url,
                                          redirect_uri=ENV["DISCORD_REDIRECT_URI"])
                self.assertTrue(config.auth_ready)
                self.assertEqual(config.allowed_origins, frozenset({origin}))
                self.assertEqual(config.site_url, url.rstrip("/") + "/")
                self.assertEqual(config.workbench_url, url.rstrip("/") + "/")

    @unittest.skipUnless(shutil.which("node"), "Node WHATWG URL oracle unavailable")
    def test_origin_examples_match_node_whatwg_url(self):
        result = subprocess.run(
            ["node", "-e", "console.log(JSON.stringify(JSON.parse(process.argv[1]).map(u => new URL(u).origin)))",
             json.dumps([url for url, _ in ORIGIN_CASES])],
            check=True, capture_output=True, text=True, timeout=10)
        self.assertEqual(json.loads(result.stdout), [origin for _, origin in ORIGIN_CASES])

    def test_guild_and_role_ids_are_not_coerced_or_defaulted(self):
        for field in ("REGISTRATION_COMMAND_GUILD", "REGISTRATION_MEMBERSHIP_GUILD", "REGISTRATION_MEMBERSHIP_ROLE"):
            for value in ("", " ", "0", "100000000000000001 ", "100000000000000001x", "１００００００００００００００００１"):
                with self.subTest(field=field, value=value), patch.dict("os.environ", {**ENV, field: value}, clear=True):
                    self.assertIsNone(configured_service(None, Mock(spec=httpx.AsyncClient)))

    def test_every_required_setting_must_be_present(self):
        for field in ENV:
            with self.subTest(field=field), patch.dict("os.environ", {k: v for k, v in ENV.items() if k != field}, clear=True):
                self.assertIsNone(configured_service(None, Mock(spec=httpx.AsyncClient)))
        for field in ("SITE_URL", "DISCORD_REDIRECT_URI", "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET"):
            with self.subTest(auth_field=field), patch.dict("os.environ", {k: v for k, v in ENV.items() if k != field}, clear=True):
                self.assertIsNone(configured_auth(None, Mock(spec=httpx.AsyncClient)))

    def test_unsafe_urls_disable_services_instead_of_becoming_redirects(self):
        unsafe = ("http://gate.example.com", "//evil.example.com", "javascript:alert(1)",
                  "https://user:secret@gate.example.com", "https://gate.example.com/#fragment",
                  "https://gate.example.com/?return_to=https://evil.example.com", "https:///missing-host",
                  "https://gate.example.com:bad", "https://gate.example.com\\@evil.example.com",
                  "https://gate.example.com\r\nX-Injected: 1", " https://gate.example.com")
        for field in ("SITE_URL", "WORKBENCH_URL", "DISCORD_REDIRECT_URI"):
            for value in unsafe:
                with self.subTest(field=field, value=value), patch.dict("os.environ", {**ENV, field: value}, clear=True):
                    self.assertIsNone(configured_service(None, Mock(spec=httpx.AsyncClient)))
                    self.assertIsNone(configured_auth(None, Mock(spec=httpx.AsyncClient)))
