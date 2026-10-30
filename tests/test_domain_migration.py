"""Deployment switches use fixtures, never real Discord or generation calls."""
import unittest
from unittest.mock import Mock, patch

import httpx

from app.registration import configured_service
from app.workbench_auth import configured_service as configured_auth

ENV = {
    "DISCORD_CLIENT_ID": "fixture-client",
    "DISCORD_CLIENT_SECRET": "fixture-secret",
    "DISCORD_BOT_TOKEN": "fixture-token",
    "REGISTRATION_BRIDGE_SECRET": "fixture-bridge",
    "REGISTRATION_COMMAND_GUILD": "100000000000000001",
    "REGISTRATION_MEMBERSHIP_GUILD": "100000000000000002",
    "REGISTRATION_MEMBERSHIP_ROLE": "100000000000000003",
    "SITE_URL": "https://gate.example.com",
    "DISCORD_REDIRECT_URI": "https://callback.example.com/self-register/callback",
}


class DomainMigrationTests(unittest.TestCase):
    def test_site_delivery_and_oauth_callback_are_independent_per_deployment(self):
        for site in ("https://gate.example.com", "https://second.example.com"):
            with self.subTest(site=site), patch.dict("os.environ", {**ENV, "SITE_URL": site}, clear=True):
                service = configured_service(None, Mock(spec=httpx.AsyncClient))
            self.assertIsNotNone(service)
            self.assertEqual(service.redirect_uri, ENV["DISCORD_REDIRECT_URI"])
            message = service._key_message("fixture-not-a-real-key")
            self.assertIn("网址：" + site + "/", message)
            self.assertIn(site + "/workbench/", message)

    def test_missing_deployment_configuration_disables_both_services(self):
        credentials = {k: v for k, v in ENV.items() if k.startswith("DISCORD_") or k == "REGISTRATION_BRIDGE_SECRET"}
        credentials.pop("DISCORD_REDIRECT_URI")
        with patch.dict("os.environ", credentials, clear=True):
            self.assertIsNone(configured_service(None, Mock(spec=httpx.AsyncClient)))
            self.assertIsNone(configured_auth(None, Mock(spec=httpx.AsyncClient)))
