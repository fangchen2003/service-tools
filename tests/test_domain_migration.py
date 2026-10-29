"""Domain switch never requires paid generation or real Discord requests."""
import unittest
from unittest.mock import Mock, patch

import httpx

from app.registration import RegistrationService, configured_service

ENV = {
    "DISCORD_CLIENT_ID": "fixture-client",
    "DISCORD_CLIENT_SECRET": "fixture-secret",
    "DISCORD_BOT_TOKEN": "fixture-token",
    "REGISTRATION_BRIDGE_SECRET": "fixture-bridge",
}


class DomainMigrationTests(unittest.TestCase):
    def test_new_site_delivery_keeps_registered_legacy_oauth_callback(self):
        with patch.dict("os.environ", ENV, clear=True):
            service = configured_service(None, Mock(spec=httpx.AsyncClient))
        assert service is not None
        message = RegistrationService._key_message("fixture-not-a-real-key")
        self.assertIn("网址：https://gate.example.com/", message)
        self.assertNotIn("网址：https://gate.example.com/", message)
        self.assertIn("https://gate.example.com/workbench/", message)
        self.assertEqual(service.redirect_uri,
                         "https://gate.example.com/self-register/callback")

    def test_registered_new_callback_can_be_selected_explicitly(self):
        new_callback = "https://gate.example.com/self-register/callback"
        with patch.dict("os.environ", {**ENV, "DISCORD_REDIRECT_URI": new_callback}, clear=True):
            service = configured_service(None, Mock(spec=httpx.AsyncClient))
        assert service is not None
        self.assertEqual(service.redirect_uri, new_callback)
