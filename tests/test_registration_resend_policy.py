"""A re-send must use the current row for both token and policy."""
import json,time
import httpx
from test_registration_configuration import DEPLOYMENT
import pytest
from app.config import Settings
from app.database import Database
from app.key_defaults import new_key_defaults
from app.registration import RegistrationService


@pytest.mark.asyncio
async def test_find_reads_current_token_after_concurrent_admin_rotation(tmp_path,monkeypatch):
    db=Database(str(tmp_path/'gate.db'));await db.connect()
    old='nai-old-fixture-token-000000000000';new='nai-new-fixture-token-000000000000';messages=[]
    try:
        row=await db.create_key({'name':'fixture','token':old,**new_key_defaults(Settings())})
        await db._db.execute('INSERT INTO discord_registrations(discord_id,key_id,token,created_at) VALUES(?,?,?,?)',('777',row['id'],old,time.time()));await db._db.commit()
        original=db.restore_discord_key
        async def rotating_restore(*args,**kwargs):
            await db.rotate_key_token(row['id'],new)
            return await original(*args,**kwargs)
        monkeypatch.setattr(db,'restore_discord_key',rotating_restore)
        def discord(req):
            if req.url.path.endswith('/channels'):return httpx.Response(200,json={'id':'dm'})
            messages.append(json.loads(req.content)['content']);return httpx.Response(200,json={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(discord)) as http:
            service=RegistrationService(db,http, deployment=DEPLOYMENT,client_id='id',client_secret='secret',bot_token='bot',bridge_secret='bridge',redirect_uri='https://test/callback')
            assert await service.find('777','100000000000000001')=='active'
            assert new in messages[-1] and old not in messages[-1]
    finally:await db.close()
