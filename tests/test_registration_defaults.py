"""Discord issuance must use the same live default policy as admin-created Keys."""
import json
import time
from urllib.parse import parse_qs,urlparse
import httpx
from test_registration_configuration import DEPLOYMENT
import pytest
from app.config import Settings
from app.database import Database
from app.registration import RegistrationService


@pytest.mark.asyncio
async def test_public_guidance_matches_current_default_scope_and_limits(tmp_path):
    from app.state import GateState
    from app.public_stats import _stats_payload
    state=GateState(Settings(data_dir=tmp_path,nai_tokens=[],default_daily_images=17,default_daily_v5=8,default_image_model_scope='legacy'))
    await state.db.connect()
    try:
        payload=await _stats_payload(state)
        assert payload['personal_legacy_limit']==17 and payload['personal_v5_limit']==8
        assert payload['personal_image_model_scope']=='legacy'
    finally:
        await state.nai.close();await state.db.close()


@pytest.mark.asyncio
async def test_admin_configuration_and_registrar_share_live_settings(tmp_path,monkeypatch):
    from fastapi import FastAPI
    from app.admin import router
    from app.state import GateState
    from app.registration import configured_service
    from test_domain_migration import ENV
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    for k in ('DISCORD_CLIENT_ID','DISCORD_CLIENT_SECRET','DISCORD_BOT_TOKEN','REGISTRATION_BRIDGE_SECRET'):
        monkeypatch.setenv(k,'fixture')
    st=GateState(Settings(data_dir=tmp_path,nai_tokens=[],admin_password='fixture',secret_key='fixture'))
    await st.db.connect()
    try:
        async with httpx.AsyncClient() as upstream:
            registrar=configured_service(st.db,upstream,settings=st.settings)
            assert registrar.settings is st.settings
            app=FastAPI();app.state.gate=st;app.include_router(router)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test') as c:
                assert (await c.post('/admin/api/login',json={'password':'fixture'})).status_code==200
                csrf=(await c.get('/admin/api/configuration')).json()['csrf_token']
                r=await c.put('/admin/api/configuration',headers={'X-NAI-Admin-CSRF':csrf},json={'default_daily_v5':7,'default_image_model_scope':'all','default_expires_days':0})
                assert r.status_code==200
                from app.key_defaults import new_key_defaults
                defaults=new_key_defaults(registrar.settings)
                created=(await c.post('/admin/api/keys',json={})).json()['key']
                for field in ('daily_images','daily_v5','daily_anlas','monthly_anlas','daily_text_tokens','rpm','allow_anlas','allow_img2img','exclude_global_v5','image_model_scope','expires_at'):
                    assert created[field]==defaults[field]
                assert defaults['daily_v5']==7 and defaults['image_model_scope']=='all'
    finally:
        await st.nai.close();await st.db.close()


@pytest.mark.asyncio
async def test_registration_hot_defaults_recovery_and_active_resend(tmp_path):
    db=Database(str(tmp_path/'gate.db'));await db.connect()
    settings=Settings(default_daily_images=17,default_daily_v5=8,default_daily_anlas=3,
        default_monthly_anlas=9,default_daily_text_tokens=321,default_rpm=7,
        default_expires_days=2,default_image_model_scope='legacy',default_allow_anlas=True,
        default_allow_img2img=True,default_exclude_global_v5=True)
    messages=[];user={'id':'777'}
    def discord(request):
        if request.url.path.endswith('/token'):return httpx.Response(200,json={'access_token':'fixture'})
        if request.url.path.endswith('/member'):return httpx.Response(200,json={'roles':['100000000000000003']})
        if request.url.path=='/api/users/@me':return httpx.Response(200,json=user)
        if request.url.path.endswith('/channels'):return httpx.Response(200,json={'id':'dm'})
        if request.url.path.endswith('/messages'):
            messages.append(json.loads(request.content)['content']);return httpx.Response(200,json={})
        raise AssertionError(request.url.path)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(discord)) as http:
            service=RegistrationService(db,http, deployment=DEPLOYMENT,client_id='id',client_secret='secret',bot_token='bot',bridge_secret='bridge',redirect_uri='https://test/callback',settings=settings)
            async def issue(after_begin=None):
                url=await service.begin(user['id'],'100000000000000001')
                if after_begin is not None: after_begin()
                await service.finish('code',parse_qs(urlparse(url).query)['state'][0])
                binding=await db.get_discord_registration(user['id'])
                return await db.get_key(binding['key_id'])
            start=time.time();first=await issue()
            fields=['daily_images','daily_v5','daily_anlas','monthly_anlas','daily_text_tokens','rpm','allow_anlas','allow_img2img','exclude_global_v5','image_model_scope']
            assert [first[k] for k in fields]==[17,8,3,9,321,7,1,1,1,'legacy']
            assert start+2*86400<=first['expires_at']<=time.time()+2*86400
            assert first['is_admin']==0
            assert '仅 V4.5 及以下' in messages[-1] and '17 张' in messages[-1]
            assert 'V5 High 30' not in messages[-1] and '默认未开通 Anlas' not in messages[-1]
            assert '已开通 Anlas' in messages[-1]

            # Same service instance sees settings changed after construction.
            settings.default_image_model_scope='all';settings.default_daily_v5=4
            settings.default_daily_images=22;settings.default_allow_anlas=False
            settings.default_allow_img2img=False;settings.default_exclude_global_v5=False
            settings.default_expires_days=0
            user['id']='778';second=await issue(lambda: setattr(settings,'default_daily_images',23))
            assert [second[k] for k in ['daily_images','daily_v5','allow_anlas','allow_img2img','exclude_global_v5','image_model_scope','expires_at']]==[23,4,0,0,0,'all',None]
            assert 'V5 High 4' in messages[-1] and '23 张' in messages[-1]
            # An active /find re-sends its actual policy, not current defaults.
            settings.default_daily_images=99;settings.default_daily_v5=98
            await service.find('778','100000000000000001')
            assert 'V5 High 4' in messages[-1] and '23 张' in messages[-1]
            assert (await db.get_key(second['id']))['daily_v5']==4
            await db.update_key(second['id'],{'enabled':False,'expires_at':time.time()-5})
            await service.find('778','100000000000000001')
            inactive=await db.get_key(second['id'])
            assert inactive['enabled']==0 and inactive['expires_at']<time.time()
            # A deleted Key's new row follows current issuance defaults, without
            # rotating the original token or creating a new registration count.
            await db.delete_key(second['id']);settings.default_image_model_scope='legacy'
            assert await service.find('778','100000000000000001')=='restored'
            restored=await db.get_discord_registration('778');row=await db.get_key(restored['key_id'])
            assert row['daily_images']==99 and row['image_model_scope']=='legacy'
            assert row['token']==second['token']
            assert (await db._db.execute_fetchall('SELECT COUNT(*) FROM discord_registrations'))[0][0]==2
    finally:await db.close()
