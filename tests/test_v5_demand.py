"""Real route/client path with fake HTTP: no polling, no unconfirmed paid switch."""
import asyncio
import io
import json
import zipfile
from types import SimpleNamespace

import httpx
import pytest

from app import main
from app.allowance import AllowanceUnavailable
from app.nai import NaiClient
from app.policy import estimate_image_cost
from test_generation_integration import FakeDB, FakeState, image_body, post
from test_image_stream_routes import event
from test_nai_integration import PNG as IMAGE_PNG


class DB(FakeDB):
    def __init__(self):
        super().__init__()
        self.v5 = {}
        self.images = {}
        self.settings = {}

    async def get_setting(self, name, default=None):
        return self.settings.get(name, default)

    async def set_setting(self, name, value):
        self.settings[name] = value

    async def get_upstream_v5_counter(self, token_id, day):
        return self.v5.get(token_id, 0)

    async def bump_upstream_v5_counter(self, token_id, day):
        self.v5[token_id] = self.v5.get(token_id, 0) + 1

    async def bump_upstream_image_counter(self, token_id, day, count):
        self.images[token_id] = self.images.get(token_id, 0) + count


def usage(percent=80, negative=False):
    return dict(active=True, tier=3, usage=dict(percent=percent, isNegative=negative))


@pytest.fixture
def env(monkeypatch):
    st = FakeState()
    st.db = DB()
    st.nai = NaiClient(['private-fixture-token'], 'https://official.invalid', '', '',
        db=st.db, day_fn=st.day, v5_daily_limits=[100], allow_anlas=[True], image_min_interval=0)
    e = SimpleNamespace(st=st, queries=[], generations=[], subscription=usage(), status=200,
                        generation_status=200, wait=None, frames=None)
    async def handler(request):
        if request.method == 'GET':
            e.queries.append(request)
            if e.wait is not None:
                await e.wait.wait()
            return httpx.Response(e.status, json=e.subscription,
                                  headers={'location':'https://untrusted.invalid'})
        e.generations.append(request)
        is_stream = request.url.path.endswith('-stream')
        count = json.loads(request.content)['parameters'].get('n_samples', 1)
        content = IMAGE_PNG
        if is_stream:
            content = e.frames if e.frames is not None else b''.join(event(index=i) for i in range(count))
        elif count > 1:
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w') as archive:
                for i in range(count): archive.writestr(f'image_{i}.png', IMAGE_PNG)
            content = output.getvalue()
        return httpx.Response(e.generation_status, content=content,
            headers={'content-type':'text/event-stream' if is_stream else 'application/octet-stream'})
    st.nai._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(main, 'STATE', st)
    return e


def body():
    return {**image_body(), 'model':'nai-diffusion-5'}


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('percent,negative', [(80,False),(0,False),(0,True),(1,True)])
async def test_confirmed_exhaustion_only_and_same_token(env, streaming, percent, negative):
    env.subscription=usage(percent, negative)
    r = await post('/ai/generate-image' + ('-stream' if streaming else ''), body())
    assert r.status_code == 200
    charge, = env.st.db.charges
    estimate=estimate_image_cost(body(), v5_allowance_available=not negative)
    assert charge[1]['anlas'] == estimate['anlas'] and charge[1]['v5'] == estimate['v5']
    assert len(env.queries)==len(env.generations)==1
    assert env.queries[0].headers['authorization']==env.generations[0].headers['authorization']
    assert env.st.nai.pool[0].pending_v5==0
    assert sum(env.st.db.v5.values())==(0 if negative else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('failure', ['status','unknown','redirect','invalid'])
async def test_unknown_pauses_without_generation_or_charge(env, streaming, failure):
    if failure=='status':env.status=429
    elif failure=='unknown':env.subscription={}
    elif failure=='redirect':env.status=302
    else:env.subscription=usage(True,False)
    r=await post('/ai/generate-image'+('-stream' if streaming else ''),body())
    assert r.status_code==503
    assert not env.generations and not env.st.db.charges
    assert env.st.nai.pool[0].pending_v5==0
    await post('/ai/generate-image',body())
    assert len(env.queries)==1  # backoff, including redirects despite client's global follow_redirects=True
    assert 'private-fixture-token' not in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize('permission', ['token','key','budget'])
@pytest.mark.parametrize('streaming',[False,True])
async def test_paid_permissions_and_budget_are_rechecked_before_send(env, permission, streaming):
    env.subscription=usage(0,True)
    if permission=='token':env.st.nai.pool[0].allow_anlas=False
    elif permission=='key':env.st.db.keys['fixture-1']['allow_anlas']=False
    else:env.st.db.keys['fixture-1']['daily_anlas']=1
    r=await post('/ai/generate-image'+('-stream' if streaming else ''),body())
    assert r.status_code in (402,503)
    assert not env.generations and not env.st.db.charges
    assert env.st.nai.pool[0].pending_v5==0


@pytest.mark.asyncio
async def test_cache_idle_snapshot_and_recovery_never_query_in_background(env):
    cache=env.st.nai.allowance
    for _ in range(3):await cache.snapshot(env.st.nai.pool)
    assert env.queries==[]
    await post('/ai/generate-image',body())
    await post('/ai/generate-image',body())
    assert len(env.queries)==1
    token_id=env.st.nai.pool[0].token_id
    cache._rows[token_id]['at']-=301
    env.subscription=usage(0,True)
    await post('/ai/generate-image',body())
    assert env.st.db.charges[-1][1]['anlas']>0
    env.subscription=usage(0,False)
    await post('/ai/generate-image',body())
    assert len(env.queries)==3  # never reuse prior depleted result to bill another image
    assert env.st.db.charges[-1][1]['anlas']==0
    snapshot=await cache.snapshot(env.st.nai.pool)
    assert snapshot['accounts'][0]['low'] and 'private-fixture-token' not in json.dumps(snapshot)
    env.st.db.settings['v5_alert_threshold']=10
    assert (await cache.snapshot(env.st.nai.pool))['threshold']==10


@pytest.mark.asyncio
async def test_failed_generation_does_not_charge_or_consume_reservation(env):
    env.subscription=usage(0,True)
    env.generation_status=500
    assert (await post('/ai/generate-image',body())).status_code==500
    assert not env.st.db.charges and not env.st.db.v5
    assert env.st.nai.pool[0].pending_v5==0


@pytest.mark.asyncio
async def test_legacy_and_already_paid_v5_make_no_subscription_query(env):
    await post('/ai/generate-image',image_body())
    await post('/ai/generate-image',{**body(),'parameters':{**body()['parameters'],'steps':29}})
    assert not env.queries and len(env.generations)==2


@pytest.mark.asyncio
async def test_paid_v5_inpainting_preserves_strength_without_cost_discount(env):
    masked = {**body(), 'model': 'nai-diffusion-5-full-inpainting', 'action': 'infill',
              'parameters': {**body()['parameters'], 'width': 512, 'height': 512,
              'steps': 29, 'image': 'fixture', 'mask': 'fixture', 'strength': .6,
              'img2img': {'strength': .6, 'color_correct': True}}}
    response = await post('/ai/generate-image', masked)
    assert response.status_code == 200
    charge, = env.st.db.charges
    assert (charge[1]['anlas'], charge[1]['v5']) == (9, 0)
    assert not env.queries and len(env.generations) == 1
    assert json.loads(env.generations[0].content)['parameters']['img2img'] == masked['parameters']['img2img']


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('exhausted', [False, True])
async def test_img2img_batch_settles_mixed_or_exhausted_cost(env, streaming, exhausted):
    env.subscription = usage(0 if exhausted else 80, exhausted)
    batch = {**body(), 'parameters': {**body()['parameters'], 'width': 512, 'height': 512,
             'steps': 20, 'n_samples': 2, 'image': 'fixture', 'strength': .6}}
    response = await post('/ai/generate-image' + ('-stream' if streaming else ''), batch)
    assert response.status_code == 200
    charge, = env.st.db.charges
    assert (charge[1]['anlas'], charge[1]['v5']) == ((8, 0) if exhausted else (4, 1))
    assert charge[1]['images'] == 2
    assert len(env.queries) == len(env.generations) == 1
    assert env.queries[0].headers['authorization'] == env.generations[0].headers['authorization']
    assert env.st.nai.pool[0].pending_v5 == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('restriction', ['token', 'key', 'budget', 'exhausted_budget'])
async def test_mixed_batch_keeps_paid_permissions_and_rechecks_budget(env, restriction):
    env.st.settings.safe_clamp = False
    batch = {**body(), 'parameters': {**body()['parameters'], 'width': 512, 'height': 512,
             'steps': 20, 'n_samples': 2, 'image': 'fixture', 'strength': .6}}
    if restriction == 'token': env.st.nai.pool[0].allow_anlas = False
    elif restriction == 'key': env.st.db.keys['fixture-1']['allow_anlas'] = False
    else:
        env.st.db.keys['fixture-1']['daily_anlas'] = 4 if restriction == 'exhausted_budget' else 3
        if restriction == 'exhausted_budget': env.subscription = usage(0, True)
    response = await post('/ai/generate-image', batch)
    assert response.status_code in (402, 503)
    assert not env.generations and not env.st.db.charges
    assert env.st.nai.pool[0].pending_v5 == 0


@pytest.mark.asyncio
async def test_partial_stream_retains_confirmed_exhausted_price(env):
    env.subscription = usage(0, True)
    env.frames = event(index=1)
    batch = {**body(), 'parameters': {**body()['parameters'], 'width': 512, 'height': 512,
             'steps': 20, 'n_samples': 3, 'image': 'fixture', 'strength': 1}}
    response = await post('/ai/generate-image-stream', batch)
    assert b'"event_type": "error"' in response.content
    charge, = env.st.db.charges
    assert (charge[1]['images'], charge[1]['anlas'], charge[1]['v5']) == (1, 6, 0)
    assert len(env.queries) == 1


@pytest.mark.asyncio
async def test_cancel_during_lookup_releases_reservation(env):
    env.wait=asyncio.Event()
    async def resolve(_):pass
    task=asyncio.create_task(env.st.nai.request('POST','https://official.invalid/ai/generate-image',body(),
        v5_free=True,image_count=1,image_lane=True,resolve_v5_cost=resolve))
    for _ in range(100):
        if env.queries:break
        await asyncio.sleep(0)
    assert len(env.queries)==1 and env.st.nai.pool[0].pending_v5==1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert env.st.nai.pool[0].pending_v5==0 and not env.generations


@pytest.mark.asyncio
async def test_admin_status_is_readonly_and_settings_validate_before_writing(env):
    from app.admin import router
    from fastapi import FastAPI
    from app.config import Settings
    app=FastAPI();app.include_router(router)
    async def allowed(_):return True
    env.st.settings=Settings(admin_password='fixture',secret_key='fixture',admin_cookie_secure=False)
    env.st.hit_login=allowed;app.state.gate=env.st
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture.invalid') as c:
        assert (await c.get('/admin/api/allowance')).status_code==401
        await c.post('/admin/api/login',json={'password':'fixture'})
        assert (await c.get('/admin/api/allowance')).status_code==200
        for value in [0,101,1.5,True,None,'20']:
            assert (await c.put('/admin/api/settings',json={'v5_alert_threshold':value})).status_code==422
            assert not env.st.db.settings
        assert (await c.put('/admin/api/settings',json={'v5_alert_threshold':30})).status_code==200
        assert (await c.get('/admin/api/settings')).json()['v5_alert_threshold']==30
    assert not env.queries


@pytest.mark.asyncio
async def test_saved_threshold_survives_sqlite_round_trip_and_partial_update(env, tmp_path):
    from app.admin import router
    from app.allowance import AllowanceCache
    from app.config import Settings
    from app.database import Database
    from fastapi import FastAPI

    db = Database(str(tmp_path / 'settings.db'))
    await db.connect()
    env.st.db = db
    cache = env.st.nai.allowance = AllowanceCache(db)
    env.st.settings = Settings(admin_password='fixture', secret_key='fixture', admin_cookie_secure=False)
    async def allowed(_): return True
    env.st.hit_login = allowed
    app = FastAPI()
    app.include_router(router)
    app.state.gate = env.st
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture.invalid') as c:
            await c.post('/admin/api/login', json={'password': 'fixture'})
            assert (await c.put('/admin/api/settings', json={'v5_alert_threshold': 30})).status_code == 200
            await db.close()
            await db.connect()
            settings = (await c.get('/admin/api/settings')).json()
            assert type(settings['v5_alert_threshold']) is int and settings['v5_alert_threshold'] == 30
            assert await cache.threshold() == 30
            cache._rows[env.st.nai.pool[0].token_id] = {'percent': 25, 'is_negative': False}
            snapshot = (await c.get('/admin/api/allowance')).json()
            assert snapshot['threshold'] == 30 and snapshot['accounts'][0]['low']
            response = await c.put('/admin/api/settings', json={'global_monthly_anlas': 100, 'global_daily_v5': 50})
            assert response.status_code == 200 and response.json()['v5_alert_threshold'] == 30
            assert (await c.put('/admin/api/settings', json={'v5_alert_threshold': '30'})).status_code == 422
            assert await cache.threshold() == 30
    finally:
        await db.close()
    assert not env.queries


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['large','malformed','timeout'])
async def test_read_failures_pause_and_close_without_secret_echo(env, failure):
    responses=[]
    async def handler(request):
        if failure=='timeout':raise httpx.ReadTimeout('private-fixture-token',request=request)
        response=httpx.Response(200,content=b'x'*65537 if failure=='large' else b'invalid-json')
        responses.append(response)
        return response
    await env.st.nai._client.aclose()
    env.st.nai._client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    response=await post('/ai/generate-image',body())
    assert response.status_code==503 and 'private-fixture-token' not in response.text
    assert not env.st.db.charges and env.st.nai.pool[0].pending_v5==0
    assert all(r.is_closed for r in responses)


@pytest.mark.asyncio
async def test_query_occurs_after_pacing_and_for_selected_pool_account(env):
    from app.nai import TokenState
    env.st.nai.pool.append(TokenState('second-private-token',1,100,True))
    pacing=asyncio.Event();release=asyncio.Event()
    async def wait(_):pacing.set();await release.wait()
    env.st.nai.wait_for_token_image_slot=wait
    task=asyncio.create_task(post('/ai/generate-image',body()))
    await pacing.wait()
    assert env.queries==[]
    release.set();assert (await task).status_code==200
    assert env.queries[0].headers['authorization']=='Bearer second-private-token'
    assert env.queries[0].headers['authorization']==env.generations[0].headers['authorization']
