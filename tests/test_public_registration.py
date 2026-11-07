"""Anonymous enrollment notice is aggregate-only and matches issuance controls."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch
import httpx
import pytest
from fastapi import FastAPI
from app.database import Database
from app.public_stats import router


@pytest.mark.asyncio
async def test_public_registration_notice_tracks_controls_without_private_fields(tmp_path):
    db=Database(str(tmp_path/'gate.db'),tz='Asia/Shanghai')
    await db.connect()
    assert db._db is not None
    try:
        now=datetime.now(ZoneInfo(db.tz))
        for i,ts in enumerate([now.timestamp(),now.timestamp(),(now-timedelta(days=1)).timestamp()]):
            await db._db.execute('INSERT INTO discord_registrations(discord_id,key_id,token,created_at) VALUES(?,?,?,?)',(str(777+i),i+1,'private-key',ts))
        await db._db.commit()
        app=FastAPI();app.state.gate=SimpleNamespace(db=db);app.state.registrar=object();app.include_router(router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test') as c:
            queries=[]
            await db._db.set_trace_callback(queries.append)
            outbound=AsyncMock(side_effect=AssertionError('Public registration must not call upstream'))
            with patch.object(httpx.AsyncHTTPTransport,'handle_async_request',outbound):
                response=await c.get('/api/public/registration')
            outbound.assert_not_called()
            assert queries and all(query.lstrip().upper().startswith('SELECT') for query in queries)
            await db._db.set_trace_callback(None)
            assert response.status_code==200
            d=response.json()
            assert d['today_count']==2 and d['daily_limit']==0 and d['remaining'] is None
            assert d['can_register'] is True and d['status']=='open'
            assert d['day']==now.date().isoformat() and d['timezone']=='Asia/Shanghai'
            assert set(d)=={'day','timezone','today_count','daily_limit','remaining','enabled','configured','can_register','status','refreshed_at'}
            assert response.headers['cache-control']=='no-store'
            assert all(x not in response.text for x in ['private-key','discord_id','csrf','history','total_count'])
            for enabled,limit,expected,remaining in [(True,3,'open',1),(True,2,'full',0),(True,1,'full',0),(False,3,'paused',1)]:
                await db.set_settings_bulk({'registration_enabled':enabled,'registration_daily_limit':limit})
                d=(await c.get('/api/public/registration')).json()
                assert d['status']==expected and d['remaining']==remaining
                assert d['can_register']==(expected=='open')
            app.state.registrar=None
            d=(await c.get('/api/public/registration')).json()
            assert d['status']=='unavailable' and d['can_register'] is False
            assert (await c.post('/api/public/registration')).status_code==405
            await db._db.execute('DROP TABLE discord_registrations');await db._db.commit()
            failed=await c.get('/api/public/registration')
            assert failed.status_code==503 and failed.headers['cache-control']=='no-store'
            assert 'gate.db' not in failed.text
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_public_notice_uninitialized_fails_closed():
    app=FastAPI();app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test') as c:
        r=await c.get('/api/public/registration')
        assert r.status_code==503 and r.headers['cache-control']=='no-store'
