"""Public deployments have no implicit guilds, callbacks or invitations."""
from dataclasses import replace

import pytest

from app.deployment import DeploymentConfig


DEPLOYMENT = DeploymentConfig(
    command_guild="100000000000000001",
    membership_guild="100000000000000002",
    membership_role="100000000000000003",
    site_url="https://gate.example.com/",
    redirect_uri="https://callback.example.com/self-register/callback",
)
VERIFICATION_DEPLOYMENT = replace(DEPLOYMENT, discord_invite_url="https://discord.gg/example-invite")


def test_verification_factory_requires_only_explicit_browser_configuration(monkeypatch):
    import os
    from unittest.mock import Mock
    import httpx
    from app.guild_verification import configured_service

    names = ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "REGISTRATION_MEMBERSHIP_GUILD",
             "REGISTRATION_MEMBERSHIP_ROLE", "DISCORD_REDIRECT_URI", "DISCORD_INVITE_URL")
    for name in tuple(os.environ):
        if name.startswith(("DISCORD_", "REGISTRATION_")) or name in ("SITE_URL", "WORKBENCH_URL"):
            monkeypatch.delenv(name)
    values = ("fixture-client", "fixture-secret", DEPLOYMENT.membership_guild,
              DEPLOYMENT.membership_role, DEPLOYMENT.redirect_uri, VERIFICATION_DEPLOYMENT.discord_invite_url)
    http = Mock(spec=httpx.AsyncClient)
    assert configured_service(None, http) is None
    for name, value in zip(names, values):
        monkeypatch.setenv(name, value)
    service = configured_service(None, http)
    assert service is not None
    assert service.deployment == replace(VERIFICATION_DEPLOYMENT, command_guild="", site_url="", workbench_url="")
    for name, value in zip(names, values):
        monkeypatch.delenv(name)
        assert configured_service(None, http) is None
        monkeypatch.setenv(name, value)


@pytest.mark.asyncio
async def test_manually_created_unconfigured_verifier_rejects_before_db_or_http():
    from unittest.mock import Mock
    from app.guild_verification import GuildVerificationService, VerificationError, COOKIE

    service = GuildVerificationService(None, Mock(), client_id="fixture", client_secret="fixture",
                                       deployment=DeploymentConfig())
    assert COOKIE == "__Host-nai-gate-guild-verify"
    with pytest.raises(VerificationError, match="配置"):
        await service.begin()
    with pytest.raises(VerificationError, match="配置"):
        await service.finish("gv." + "a" * 43, "b" * 43, "code", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"membership_guild": ""}, {"membership_role": ""}, {"discord_invite_url": ""},
    {"discord_invite_url": "https://evil.example/invite"}, {"redirect_uri": ""},
    {"redirect_uri": "https://callback.example.com:bad/self-register/callback"},
])
async def test_unconfigured_browser_route_never_redirects_or_stores_state(overrides):
    from unittest.mock import Mock
    import httpx
    from fastapi import FastAPI
    from app.guild_verification import GuildVerificationService, router

    app = FastAPI()
    app.state.guild_verification = GuildVerificationService(
        None, Mock(), client_id="fixture", client_secret="fixture",
        deployment=replace(VERIFICATION_DEPLOYMENT, **overrides))
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gate.example.com") as client:
        response = await client.get("/self-register/verify")
    assert response.status_code == 503
    assert "location" not in response.headers and "set-cookie" not in response.headers
    assert "discord.gg" not in response.text and "discord.com/invite" not in response.text


def test_guild_verification_requires_explicit_invite_and_membership_configuration():
    assert not DeploymentConfig().guild_verification_ready
    assert not DEPLOYMENT.guild_verification_ready
    configured = replace(DEPLOYMENT, discord_invite_url="https://discord.gg/example-invite")
    assert configured.guild_verification_ready
    # Browser-only verification does not need bot credentials or command/site URLs.
    assert replace(configured, command_guild="", site_url="", workbench_url="").guild_verification_ready
    for field in ("membership_guild", "membership_role", "redirect_uri", "discord_invite_url"):
        assert not replace(configured, **{field: ""}).guild_verification_ready


@pytest.mark.parametrize("invite", [
    "", "http://discord.gg/example", "https://discord.gg.evil.example/example",
    "https://discord.gg@evil.example/example", "https://discord.gg/",
    "https://discord.com/example", "https://discord.com/invite/",
    "https://discord.gg/example?tracking=1", "https://discord.gg/example#fragment",
    "https://discord.gg/example/extra", "https://discord.gg:444/example",
    "https://discord.gg\\@evil.example/example", "https://discord.gg/example\n",
    "https://discord.com/invite/%22onclick=evil", "https://user@discord.gg/example",
])
def test_invalid_invites_disable_browser_verification(invite):
    assert not replace(DEPLOYMENT, discord_invite_url=invite).guild_verification_ready


@pytest.mark.parametrize("invite", ["https://discord.gg/example-invite_1", "https://discord.com/invite/example-invite_1"])
def test_supported_discord_invite_urls_are_configured_per_deployment(monkeypatch, invite):
    monkeypatch.setenv("DISCORD_INVITE_URL", invite)
    config = DeploymentConfig.from_env()
    assert config.discord_invite_url == invite
    assert replace(DEPLOYMENT, discord_invite_url=invite).guild_verification_ready
