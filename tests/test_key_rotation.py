"""Rotate only credentials; preserve usage, policy and admitted work."""
import asyncio
import sqlite3
import time

import httpx
import pytest
import pytest_asyncio

from app import main
from app.config import Settings
from app.database import Database
from test_generation_integration import FakeState, image_body


@pytest_asyncio.fixture
async def env(monkeypatch):
    state = FakeState()
    state.settings = Settings(admin_password='fixture', secret_key='fixture-session',
                              admin_cookie_secure=False, key_image_min_interval=0)
    state.db = Database(':memory:')
    await state.db.connect()

    async def login_allowed(_):
        return True

    state.hit_login = login_allowed
    monkeypatch.setattr(main, 'STATE', state)
    monkeypatch.setattr(main.app.state, 'gate', state, raising=False)
    key = await state.db.create_key(dict(name="fixture 'quoted'", token='fixture-old',
        daily_images=17, daily_anlas=20, daily_v5=9, monthly_anlas=100,
        daily_text_tokens=2000, rpm=7, allow_anlas=True, allow_img2img=True,
        exclude_global_v5=True, image_model_scope='all', expires_at=time.time()+86400))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                base_url='http://fixture') as client:
        try:
            yield state, client, key
        finally:
            await state.db.close()


async def login(client):
    assert (await client.post('/admin/api/login', json={'password': 'fixture'})).status_code == 200


def auth(token):
    return {'Authorization': 'Bearer '+token}


async def snapshot(db):
    return {table: [tuple(r) for r in await (await db._db.execute(f'SELECT * FROM {table}')).fetchall()]
            for table in ('counters', 'usage_log', 'daily_quota_offsets', 'deleted_key_usage_flags')}


@pytest.mark.asyncio
async def test_lifetime_image_count_is_visible_to_admin_and_key_owner(env):
    state, client, key = env
    day = state.day()
    await state.db.bump_counters(key['id'], day, images=3)
    await state.db.add_log(key['id'], key['name'], 'image', 'model', 'ok', images=2)
    await state.db.add_log(key['id'], key['name'], 'image_stream', 'model', 'ok', images=1)
    await state.db.add_log(key['id'], key['name'], 'image', 'model', 'error', images=5)
    await state.db.add_log(key['id'], key['name'], 'augment-image', 'model', 'ok', images=1)
    await login(client)

    keys = (await client.get('/admin/api/keys')).json()['keys']
    assert keys[0]['used']['images'] == 3
    assert keys[0]['used']['generated_images_total'] == 3
    me = (await client.get('/v1/me', headers=auth(key['token']))).json()
    assert me['generated_images_total'] == 3
    assert me['today']['images'] == 3


@pytest.mark.asyncio
async def test_admin_only_missing_and_get_cannot_rotate(env):
    state, client, key = env
    path = f"/admin/api/keys/{key['id']}/regenerate"
    for headers in ({}, auth(key['token']), {'Cookie': 'nai_gate_admin=9999999999.forged'}):
        response = await client.post(path, headers=headers)
        assert response.status_code == 401 and 'token' not in response.json()
    assert (await state.db.get_key(key['id']))['token'] == key['token']
    await login(client)
    assert (await client.get(path)).status_code == 405
    assert (await client.post('/admin/api/keys/999999/regenerate')).status_code == 404
    assert (await state.db.get_key(key['id']))['token'] == key['token']


@pytest.mark.asyncio
async def test_rotation_preserves_all_state_and_rejects_old_token(env):
    state, client, key = env
    db, day = state.db, state.day()
    await db.bump_counters(key['id'], day, images=3, anlas=9, v5=2, text_tokens=40)
    await db.add_log(key['id'], key['name'], 'image', 'fixture', 'ok', images=3, anlas=9)
    await db.reset_daily_image_quota(key['id'], day)
    await db._db.execute("ALTER TABLE api_keys ADD COLUMN custom_note TEXT DEFAULT 'preserved'")
    await db._db.commit()
    before_key = dict(await db.get_key(key['id']))
    before_data = await snapshot(db)
    semaphore = state.key_sem(key['id'], 2)
    state._tag_next_at[key['id']] = 12345
    await login(client)
    response = await client.post(f"/admin/api/keys/{key['id']}/regenerate")
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    token = response.json()['token']
    assert token.startswith('nai-') and len(token) == 36 and token != key['token']
    after_key = dict(await db.get_key(key['id']))
    assert after_key == {**before_key, 'token': token}
    assert await snapshot(db) == before_data
    assert state.key_sem(key['id'], 2) is semaphore and state._tag_next_at[key['id']] == 12345
    assert (await client.get('/user/information', headers=auth(key['token']))).status_code == 401
    assert (await client.get('/user/information', headers=auth(token))).status_code == 200
    assert (await client.post('/ai/generate-image', json=image_body(), headers=auth(key['token']))).status_code == 401
    assert not state.nai.calls
    assert await snapshot(db) == before_data


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('enabled', False), ('expires_at', 1)])
async def test_rotation_does_not_reenable_disabled_or_expired_key(env, field, value):
    state, client, key = env
    await state.db.update_key(key['id'], {field: value})
    await login(client)
    response = await client.post(f"/admin/api/keys/{key['id']}/regenerate")
    assert response.status_code == 200
    assert (await client.get('/user/information', headers=auth(response.json()['token']))).status_code == 403
    assert (await state.db.get_key(key['id']))[field] == value


@pytest.mark.asyncio
async def test_inflight_generation_settles_to_same_key_after_rotation(env):
    state, client, key = env
    await login(client)
    state.nai.release = asyncio.Event()
    task = asyncio.create_task(client.post('/ai/generate-image', json=image_body(), headers=auth(key['token'])))
    try:
        await asyncio.wait_for(state.nai.entered.wait(), 2)
        assert state.global_active == 1
        response = await client.post(f"/admin/api/keys/{key['id']}/regenerate")
        assert response.status_code == 200
        token = response.json()['token']
        assert (await client.get('/user/information', headers=auth(key['token']))).status_code == 401
        assert state.global_active == 1
        state.nai.release.set()
        assert (await asyncio.wait_for(task, 2)).status_code == 200
        counter = await state.db.get_counter(key['id'], state.day())
        assert counter['images'] == counter['requests'] == 1
        assert counter['anlas'] == 0 and state.global_active == 0
        assert await state.db.count_logs() == 1
        assert (await client.get('/user/information', headers=auth(token))).status_code == 200
    finally:
        state.nai.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_repeated_rotation_only_latest_token_works_and_deleted_is_404(env):
    state, client, key = env
    await login(client)
    path = f"/admin/api/keys/{key['id']}/regenerate"
    tokens = [key['token']]
    for _ in range(2):
        response = await client.post(path)
        assert response.status_code == 200
        tokens.append(response.json()['token'])
    assert len(set(tokens)) == 3
    for token in tokens[:-1]:
        assert await state.db.get_key_by_token(token) is None
    assert (await state.db.get_key_by_token(tokens[-1]))['id'] == key['id']
    await state.db.delete_key(key['id'])
    assert (await client.post(path)).status_code == 404


@pytest.mark.asyncio
async def test_collision_does_not_destroy_existing_credentials(env):
    state, _, key = env
    other = await state.db.create_key(dict(name='other', token='other-token', daily_images=1,
        monthly_anlas=1, daily_text_tokens=1, rpm=1))
    with pytest.raises(sqlite3.IntegrityError):
        await state.db.rotate_key_token(key['id'], other['token'])
    assert (await state.db.get_key(key['id']))['token'] == key['token']
    assert (await state.db.get_key(other['id']))['token'] == other['token']
    await state.db._db.rollback()


@pytest.mark.asyncio
async def test_noop_scope_patch_keeps_algorithm_and_pin_goes_manual(env):
    # 2026-10-10：提交一个没变的「模型 = 全部」就把 Key 转成手动、V5 冻结在 18；只有值真的变了才转手动
    state, client, key = env
    await login(client)
    r = await client.patch(f"/admin/api/keys/{key['id']}", json={'image_model_scope': 'all', 'notify': False})
    assert r.status_code == 200
    assert (await state.db.get_key(key['id']))['quota_auto'] == 1
    r = await client.patch(f"/admin/api/keys/{key['id']}", json={'v5_pinned': 30, 'notify': False})
    row = await state.db.get_key(key['id'])
    assert r.status_code == 200 and row['quota_auto'] == -1 and row['v5_pinned'] == 30 and row['image_model_scope'] == 'all'
    r = await client.patch(f"/admin/api/keys/{key['id']}", json={'quota_mode': 'auto', 'notify': False})
    row = await state.db.get_key(key['id'])
    assert row['quota_auto'] == 1 and row['v5_pinned'] is None            # 交回算法：清掉手动基础值


@pytest.mark.asyncio
async def test_quota_two_modes_auto_and_manual(env):
    # 2026-10-11 站长：额度只有「算法」和「手动」两种。手动 = 填多少就是多少；算法 = 全部交给算法，忽略填的数字
    state, client, key = env
    await login(client)
    manual = {'quota': 'manual', 'notify': False, 'daily_images': 40, 'daily_v5': 80, 'daily_anlas': 150,
              'monthly_anlas': 1500, 'allow_anlas': True, 'exclude_global_v5': True, 'image_model_scope': 'all',
              'features': ['image', 'vibe', 'upscale']}
    r = await client.patch(f"/admin/api/keys/{key['id']}", json=manual)
    row = await state.db.get_key(key['id'])
    assert r.status_code == 200
    assert (row['quota_auto'], row['anlas_auto'], row['v5_pinned']) == (-1, -1, None)        # 没有「基础值」中间状态
    assert (row['daily_images'], row['daily_v5'], row['daily_anlas'], row['exclude_global_v5']) == (40, 80, 150.0, 1)
    assert row['features'] == 'image,upscale,vibe'
    r = await client.patch(f"/admin/api/keys/{key['id']}", json={'quota': 'auto', 'notify': False,
                                                                  'daily_images': 999, 'exclude_global_v5': True})
    row = await state.db.get_key(key['id'])
    assert r.status_code == 200
    assert (row['quota_auto'], row['anlas_auto'], row['v5_pinned'], row['features'], row['exclude_global_v5']) == (1, 0, None, None, 0)
    assert row['daily_images'] != 999                                                        # 算法模式忽略填的数字
    assert (await client.patch(f"/admin/api/keys/{key['id']}", json={'quota': 'half'})).status_code == 422
    listed = {k['id']: k for k in (await client.get('/admin/api/keys')).json()['keys']}
    assert listed[key['id']]['quota_auto'] is True


@pytest.mark.asyncio
async def test_member_tags_manual_and_auto(env):
    # 后台标签：手动（带备注）+ 自动（近 24 小时因成员自己能改的原因被拒）；删 Key 时标签一起清掉
    state, client, key = env
    await login(client)
    await state.db.add_log(key['id'], key['name'], 'image', 'm', 'rejected',
                           detail='403 Vibe 编码会消耗 Anlas（每次编码参考图约 2 Anlas）')
    r = await client.put(f"/admin/api/keys/{key['id']}/tags", json={'tag': '疑似小号', 'note': '和 #100 同前缀'})
    assert r.status_code == 200
    assert (await client.put(f"/admin/api/keys/{key['id']}/tags", json={'tag': '  '})).status_code == 422
    from app.admin import member_tags
    manual, auto = await member_tags(state.db, time.time() - 86400)
    assert [t['tag'] for t in manual[key['id']]] == ['疑似小号'] and manual[key['id']][0]['note'] == '和 #100 同前缀'
    assert auto[key['id']] == [{'tag': '开着 Vibe', 'count': 1}]
    actions = [a['action'] for a in await state.db.list_admin_actions()]
    assert '成员标签' in actions
    assert (await client.delete(f"/admin/api/keys/{key['id']}/tags/疑似小号")).status_code == 200
    assert (await client.delete(f"/admin/api/keys/{key['id']}/tags/疑似小号")).status_code == 404
    await client.put(f"/admin/api/keys/{key['id']}/tags", json={'tag': '站长熟人'})
    await state.db.delete_key(key['id'])
    assert not await state.db._db.execute_fetchall("SELECT 1 FROM key_tags WHERE key_id=?", (key['id'],))
