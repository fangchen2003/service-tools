"""Browser-normalized callback origins must not self-redirect."""
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from app.database import Database
from app.guild_verification import COOKIE, GuildVerificationService, router
from test_registration_configuration import VERIFICATION_DEPLOYMENT


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,canonical", [
    ("bücher.example", "xn--bcher-kva.example"),
    ("[2001:0db8:0:0::1]", "[2001:db8::1]"),
    ("2130706433", "127.0.0.1"),
])
async def test_canonical_callback_origin_starts_oauth_without_redirect_loop(tmp_path, configured, canonical):
    callback = f"https://{configured}/self-register/callback"
    deployment = replace(VERIFICATION_DEPLOYMENT, redirect_uri=callback)
    assert deployment.guild_verification_ready
    db = Database(str(tmp_path / "gate.sqlite"))
    await db.connect()
    token_payloads = []

    def discord(request):
        if request.url.path.endswith("/token"):
            token_payloads.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"access_token": "fixture-token"})
        if request.url.path.endswith("/member"):
            return httpx.Response(200, json={"roles": [deployment.membership_role]})
        return httpx.Response(200, json={"id": "777"})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(discord)) as upstream:
            service = GuildVerificationService(db, upstream, deployment=deployment,
                                              client_id="fixture", client_secret="fixture")
            await service.initialize()
            app = FastAPI()
            app.state.guild_verification = service
            app.include_router(router)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                         base_url=f"https://{canonical}") as client:
                response = await client.get("/self-register/verify")
                assert response.status_code == 302
                assert urlsplit(response.headers["location"]).hostname == "discord.com"
                assert COOKIE in response.headers["set-cookie"]
                query = parse_qs(urlsplit(response.headers["location"]).query)
                assert query["redirect_uri"] == [callback]
                await service.finish(query["state"][0], client.cookies[COOKIE], "fixture-code", "")
                assert token_payloads[0]["redirect_uri"] == [callback]
                alias = await client.get("/self-register/verify", headers={"host": "alias.example"})
                assert alias.headers["location"] == f"https://{canonical}/self-register/verify"
                assert "set-cookie" not in alias.headers
    finally:
        await db.close()
