"""Byte limits apply before parsing, even for chunked or misleading requests."""
import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.datastructures import UploadFile

from app import main


def chunk_request(chunks, headers=(), *, content_type=b'application/json'):
    reads=[]
    async def receive():
        index=len(reads)
        assert index < len(chunks), 'read past the end'
        reads.append(index)
        return {'type':'http.request','body':chunks[index],'more_body':index+1<len(chunks)}
    return Request({'type':'http','method':'POST','path':'/fixture',
        'headers':[(b'content-type',content_type),*headers]},receive),reads


@pytest.mark.asyncio
@pytest.mark.parametrize('declared',[None,b'1'])
async def test_actual_json_limit_stops_before_reading_rest(declared):
    headers=[] if declared is None else [(b'content-length',declared)]
    request,reads=chunk_request([b'{"v":"',b'a'*60,b'a'*60,b'"}'],headers)
    with pytest.raises(main.GateError) as error:
        await main.read_json(request,limit_mb=100/(1024*1024))
    assert error.value.status==413
    assert len(reads)==3


@pytest.mark.asyncio
async def test_exact_json_byte_boundary_accepts_unicode():
    raw=json.dumps({'v':'中文'},ensure_ascii=False).encode()
    request,_=chunk_request([raw[:10],raw[10:]])
    assert await main.read_json(request,limit_mb=len(raw)/(1024*1024))=={'v':'中文'}


@pytest.mark.asyncio
@pytest.mark.parametrize('declared,status',[(b'101',413),(b'-1',400),(b'NaN',400),
                                         (b'1.5',400),(b'x'*5000,400),(b'9'*5000,413)])
async def test_bad_or_oversized_length_rejects_without_reading(declared,status):
    request,reads=chunk_request([b'{}'],[(b'content-length',declared)])
    with pytest.raises(main.GateError) as error:
        await main.read_json(request,limit_mb=100/(1024*1024))
    assert error.value.status==status and reads==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('raw',[b'invalid',b'[]',b'null',b'123'])
async def test_invalid_or_nonobject_json_remains_400(raw):
    request,_=chunk_request([raw])
    with pytest.raises(main.GateError) as error:await main.read_json(request)
    assert error.value.status==400


def multipart(*, file=False, extra=False):
    filename='; filename="request.json"' if file else ''
    raw=(f'--fixture\r\nContent-Disposition: form-data; name="request"{filename}\r\n'
         'Content-Type: application/json\r\n\r\n{"input":"fixture"}\r\n').encode()
    if extra:
        raw+=b'--fixture\r\nContent-Disposition: form-data; name="image"; filename="image.png"\r\n\r\nfixture\r\n'
    return raw+b'--fixture--\r\n'


@pytest.mark.asyncio
@pytest.mark.parametrize('file',[False,True])
async def test_multipart_text_and_file_fields_are_compatible(file,monkeypatch):
    raw=multipart(file=file);closed=[];original=UploadFile.close
    async def close(upload):
        await original(upload);closed.append(upload.file.closed)
    monkeypatch.setattr(UploadFile,'close',close)
    request,_=chunk_request([raw[:35],raw[35:]],content_type=b'multipart/form-data; boundary=fixture')
    assert await main.read_image_payload(request,limit_mb=len(raw)/(1024*1024))=={'input':'fixture'}
    assert closed==([True] if file else [])


@pytest.mark.asyncio
async def test_multipart_oversize_rejected_before_parser(monkeypatch):
    async def forbidden(*args,**kwargs):pytest.fail('parser must not run for oversized body')
    monkeypatch.setattr(Request,'form',forbidden)
    request,reads=chunk_request([b'x'*60,b'x'*60,b'tail'],content_type=b'multipart/form-data; boundary=fixture')
    with pytest.raises(main.GateError) as error:
        await main.read_image_payload(request,limit_mb=100/(1024*1024))
    assert error.value.status==413 and len(reads)==2


@pytest.mark.asyncio
async def test_rejected_multipart_closes_all_files(monkeypatch):
    raw=multipart(file=True,extra=True);closed=[];original=UploadFile.close
    async def close(upload):
        await original(upload);closed.append(upload.file.closed)
    monkeypatch.setattr(UploadFile,'close',close)
    request,_=chunk_request([raw],content_type=b'multipart/form-data; boundary=fixture')
    with pytest.raises(main.GateError) as error:await main.read_image_payload(request)
    assert error.value.status==400 and closed==[True,True]


@pytest.mark.asyncio
async def test_login_chunked_body_capped_without_cookie():
    from app.admin import router
    from app.config import Settings
    from types import SimpleNamespace
    async def hit(_):return True
    app=FastAPI();app.include_router(router)
    app.state.gate=SimpleNamespace(hit_login=hit,settings=Settings(admin_password='fixture',secret_key='fixture'))
    async def chunks():
        yield b'{"password":"'
        for _ in range(17):yield b'a'*65536
        yield b'"}'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture.invalid') as client:
        response=await client.post('/admin/api/login',content=chunks(),headers={'content-type':'application/json'})
    assert response.status_code==413 and 'set-cookie' not in response.headers


@pytest.mark.asyncio
async def test_cancellation_during_body_read_is_not_swallowed():
    async def receive():raise asyncio.CancelledError()
    request=Request({'type':'http','headers':[]},receive)
    with pytest.raises(asyncio.CancelledError):await main.read_json(request)


@pytest.mark.asyncio
@pytest.mark.parametrize('multipart_file',[False,True])
async def test_large_image_payload_preserves_prompts_characters_and_references(multipart_file):
    # Byte-limit regression, not a claim to reproduce the official tokenizer.
    payload={'input':'detailed background, '*1500,'model':'nai-diffusion-5',
             'parameters':{'negative_prompt':'undesired detail, '*1500,
                'v4_prompt':{'caption':{'base_caption':'scene, '*1500,
                    'char_captions':[{'char_caption':f'character {i}, '+('blue clothes, '*100)} for i in range(22)]}},
                'reference_image_multiple':['A'*(600*1024),'B'*(600*1024)]}}
    raw=json.dumps(payload).encode()
    content_type=b'application/json'
    if multipart_file:
        raw=(b'--fixture\r\nContent-Disposition: form-data; name="request"; filename="request.json"\r\n'
             b'Content-Type: application/json\r\n\r\n'+raw+b'\r\n--fixture--\r\n')
        content_type=b'multipart/form-data; boundary=fixture'
    assert 1024*1024<len(raw)<25*1024*1024
    request,_=chunk_request([raw[i:i+65536] for i in range(0,len(raw),65536)],content_type=content_type)
    assert await main.read_image_payload(request)==payload
