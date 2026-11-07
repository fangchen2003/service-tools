"""Public role verification: no key issuance or real Discord traffic."""
import asyncio
import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
from test_registration_configuration import VERIFICATION_DEPLOYMENT
import pytest

from app.database import Database
from app.registration_routes import router as registration_router


@pytest.mark.asyncio
async def test_verified_browser_receives_invite_without_creating_key(tmp_path):
    # Import inside test: RED fails at the missing feature, not collection.
    from app.guild_verification import GuildVerificationService, router, COOKIE
    from fastapi import FastAPI

    db = Database(str(tmp_path / 'gate.sqlite'))
    await db.connect()
    calls = []
    roles = ['100000000000000003']
    mode = {'status': 200}

    def discord(request):
        calls.append(request)
        if mode['status'] != 200:
            return httpx.Response(mode['status'], json={'error': 'private-upstream-detail'})
        if request.url.path == '/api/oauth2/token':
            return httpx.Response(200, json={'access_token': 'private-user-token'})
        if request.url.path == '/api/users/@me':
            return httpx.Response(200, json={'id': '777'})
        if request.url.path == '/api/users/@me/guilds/100000000000000002/member':
            return httpx.Response(200, json={'roles': roles})
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(discord)) as upstream:
        service = GuildVerificationService(db, upstream, deployment=VERIFICATION_DEPLOYMENT, client_id='client', client_secret='private-secret',
                                          redirect_uri='https://gate.example.com/self-register/callback')
        await service.initialize()
        app = FastAPI()
        app.state.guild_verification = service
        app.state.registrar = None
        app.include_router(router)
        app.include_router(registration_router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://gate.example.com') as client:
            start = await client.get('/self-register/verify')
            assert start.status_code == 302
            query = parse_qs(urlparse(start.headers['location']).query)
            assert query['scope'] == ['identify guilds.members.read']
            assert query['redirect_uri'] == ['https://gate.example.com/self-register/callback']
            assert query['code_challenge_method'] == ['S256']
            cookie = start.headers['set-cookie']
            assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=lax' in cookie
            assert COOKIE in client.cookies
            state = query['state'][0]
            result = await client.get('/self-register/callback', params={'state':state, 'code':'private-code'})
            assert result.status_code == 200
            assert 'https://discord.gg/example-invite' in result.text
            assert '验证通过' in result.text
            assert '/register' in result.text
            assert 'nai-' not in result.text
            assert result.headers['cache-control'] == 'no-store'
            assert result.headers['referrer-policy'] == 'no-referrer'
            assert "frame-ancestors 'none'" in result.headers['content-security-policy']
            assert COOKIE not in client.cookies
            assert len(calls) == 3
            token_payload = parse_qs(calls[0].content.decode())
            assert 'code_verifier' in token_payload
            verifier = token_payload['code_verifier'][0]
            assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode() == query['code_challenge'][0]
            replay = await client.get('/self-register/callback', params={'state':state, 'code':'private-code'})
            assert replay.status_code == 403
            assert 'example-invite' not in replay.text
            assert len(calls) == 3
            for table in ['api_keys', 'discord_registrations']:
                assert (await db._db.execute_fetchall(f'SELECT count(*) FROM {table}'))[0][0] == 0

            # Missing role and API errors never disclose the invitation.
            for status, role_list in [(200, []), (403, roles[:]), (429, roles[:])]:
                roles[:] = role_list
                mode['status'] = status
                start = await client.get('/self-register/verify')
                state = parse_qs(urlparse(start.headers['location']).query)['state'][0]
                denied = await client.get('/self-register/callback', params={'state':state, 'code':'private-code'})
                assert denied.status_code == 403
                assert 'discord.gg' not in denied.text
                assert 'private-' not in denied.text
    await db.close()


@pytest.mark.asyncio
async def test_state_browser_binding_expiry_error_and_concurrency(tmp_path):
    from app.guild_verification import GuildVerificationService, router, COOKIE
    from fastapi import FastAPI
    db = Database(str(tmp_path / 'gate.sqlite'))
    await db.connect()
    calls = []
    def discord(req):
        calls.append(req)
        if req.url.path == '/api/oauth2/token':
            return httpx.Response(200, json={'access_token':'token'})
        if req.url.path == '/api/users/@me':
            return httpx.Response(200, json={'id':'777'})
        return httpx.Response(200, json={'roles':['100000000000000003']})
    async with httpx.AsyncClient(transport=httpx.MockTransport(discord)) as upstream:
        service = GuildVerificationService(db, upstream, deployment=VERIFICATION_DEPLOYMENT, client_id='client', client_secret='secret', redirect_uri='https://gate.example.com/self-register/callback')
        await service.initialize()
        app = FastAPI()
        app.state.guild_verification = service
        app.include_router(router)
        app.include_router(registration_router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://gate.example.com') as client:
            start = await client.get('/self-register/verify')
            state = parse_qs(urlparse(start.headers['location']).query)['state'][0]
            nonce = client.cookies.get(COOKIE)
            client.cookies.clear()
            for headers in [{}, {'Cookie': COOKIE+'=wrong'}, {'Cookie': COOKIE+'='+'a'*43}]:
                bad = await client.get('/self-register/callback', params={'state':state, 'code':'code'}, headers=headers)
                assert bad.status_code == 403
                assert not calls
            app.state.guild_verification = GuildVerificationService(db, upstream, deployment=VERIFICATION_DEPLOYMENT, client_id='client', client_secret='secret', redirect_uri='https://gate.example.com/self-register/callback')
            results = await asyncio.gather(*(client.get('/self-register/callback', params={'state':state,'code':'code'}, headers={'Cookie':COOKIE+'='+nonce}) for _ in range(3)))
            assert sorted(r.status_code for r in results) == [200,403,403]
            assert len(calls) == 3
            start = await client.get('/self-register/verify')
            state = parse_qs(urlparse(start.headers['location']).query)['state'][0]
            await db._db.execute('UPDATE guild_verification_states SET expires_at=0')
            await db._db.commit()
            expired = await client.get('/self-register/callback', params={'state':state,'code':'code'})
            assert expired.status_code == 403
            start = await client.get('/self-register/verify')
            state = parse_qs(urlparse(start.headers['location']).query)['state'][0]
            cancel = await client.get('/self-register/callback', params={'state':state,'error':'access_denied'})
            assert cancel.status_code == 403
            assert 'discord.gg' not in cancel.text
            assert len(calls) == 3
    await db.close()


@pytest.mark.asyncio
async def test_unconfigured_start_fails_closed():
    from app.guild_verification import router
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://gate.example.com') as client:
        response = await client.get('/self-register/verify')
        assert response.status_code == 503
        assert response.headers['cache-control'] == 'no-store'
        assert 'discord.gg' not in response.text
