"""Public OAuth start abuse/canonical callback regressions."""
import httpx
import pytest
from fastapi import FastAPI
from app.database import Database
from test_registration_configuration import VERIFICATION_DEPLOYMENT


@pytest.mark.asyncio
async def test_one_browser_replaces_pending_state_and_ip_is_rate_limited(tmp_path):
    from app.guild_verification import GuildVerificationService, router
    db = Database(str(tmp_path/'gate.db'))
    await db.connect()
    async with httpx.AsyncClient() as http:
        service = GuildVerificationService(db,http, deployment=VERIFICATION_DEPLOYMENT,client_id='id',client_secret='secret',redirect_uri='https://gate.example.com/self-register/callback')
        await service.initialize()
        app = FastAPI()
        app.state.guild_verification=service
        app.include_router(router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://gate.example.com') as client:
            assert (await client.get('/self-register/verify')).status_code==302
            assert (await client.get('/self-register/verify')).status_code==302
            count=(await db._db.execute_fetchall('SELECT count(*) FROM guild_verification_states'))[0][0]
            assert count==1
            for _ in range(12):
                response=await client.get('/self-register/verify')
            assert response.status_code==429
            assert response.headers['retry-after']=='60'
            assert 'discord.gg' not in response.text
        # TLS terminates at Nginx; Docker's ASGI scope may still say http.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.2', 12345)),base_url='http://gate.example.com') as proxy_client:
            proxied=await proxy_client.get('/self-register/verify')
            assert proxied.status_code==302
            assert proxied.headers['location'].startswith('https://discord.com/oauth2/authorize?')
    await db.close()


@pytest.mark.asyncio
async def test_callback_origin_sets_cookie_on_explicitly_configured_domain(tmp_path,monkeypatch):
    from app.guild_verification import configured_service, router
    from test_domain_migration import ENV
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('DISCORD_INVITE_URL', VERIFICATION_DEPLOYMENT.discord_invite_url)
    monkeypatch.setenv('DISCORD_CLIENT_ID','id')
    monkeypatch.setenv('DISCORD_CLIENT_SECRET','secret')
    monkeypatch.setenv('DISCORD_REDIRECT_URI', VERIFICATION_DEPLOYMENT.redirect_uri)
    db=Database(str(tmp_path/'gate.db'))
    await db.connect()
    async with httpx.AsyncClient() as http:
        service=configured_service(db,http)
        assert service.redirect_uri==VERIFICATION_DEPLOYMENT.redirect_uri
        await service.initialize()
        app=FastAPI()
        app.state.guild_verification=service
        app.include_router(router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://gate.example.com') as client:
            response=await client.get('/self-register/verify')
            assert response.status_code==302
            assert response.headers['location']=='https://callback.example.com/self-register/verify'
            assert 'set-cookie' not in response.headers
            assert (await db._db.execute_fetchall('SELECT count(*) FROM guild_verification_states'))[0][0]==0
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://callback.example.com') as client:
            response=await client.get('/self-register/verify')
            assert response.status_code==302
            assert response.headers['location'].startswith('https://discord.com/oauth2/authorize?')
            assert 'set-cookie' in response.headers
    await db.close()
