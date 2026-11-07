"""Discord-stage errors must never reveal the invite or upstream diagnostics."""
from urllib.parse import parse_qs,urlparse
import httpx
from test_registration_configuration import VERIFICATION_DEPLOYMENT
import pytest
from fastapi import FastAPI
from app.database import Database
from app.registration_routes import router as callback_router


@pytest.mark.asyncio
@pytest.mark.parametrize('stage',['token','user','member'])
@pytest.mark.parametrize('failure',['403','429','timeout','malformed'])
async def test_each_discord_stage_fails_closed(tmp_path,stage,failure):
    from app.guild_verification import GuildVerificationService,router
    db=Database(str(tmp_path/'gate.db'))
    await db.connect()
    def discord(request):
        current='token' if request.url.path.endswith('/token') else 'member' if request.url.path.endswith('/member') else 'user'
        if current==stage:
            if failure=='timeout':
                raise httpx.ReadTimeout('private-upstream-diagnostic',request=request)
            if failure=='malformed':
                return httpx.Response(200,content=b'private-upstream-invalid-json')
            return httpx.Response(int(failure),json={'detail':'private-upstream-diagnostic'})
        return httpx.Response(200,json={'token': {'access_token':'private-token'},'user':{'id':'777'},'member':{'roles':['100000000000000003']}}[current])
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(discord)) as upstream:
            service=GuildVerificationService(db,upstream, deployment=VERIFICATION_DEPLOYMENT,client_id='id',client_secret='private-secret',redirect_uri='https://gate.example.com/self-register/callback')
            await service.initialize()
            app=FastAPI()
            app.state.guild_verification=service
            app.include_router(router)
            app.include_router(callback_router)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://gate.example.com') as client:
                start=await client.get('/self-register/verify')
                state=parse_qs(urlparse(start.headers['location']).query)['state'][0]
                result=await client.get('/self-register/callback',params={'state':state,'code':'private-code'})
                assert result.status_code==403
                assert 'discord.gg' not in result.text
                assert 'private-' not in result.text
                assert result.headers['cache-control']=='no-store'
    finally:
        await db.close()
