"""Image tool admission, billing and hostile image/result regressions."""
import asyncio
import base64
import io
import gzip
import zipfile

import httpx
import pytest
from PIL import Image

from app import main
from app.image_tools import prepare_tool, validate_result
from app.nai import UpstreamError
from test_generation_integration import state, post
from test_nai_integration import make_client


def png(width=1024, height=1024):
    with io.BytesIO() as out:
        Image.new("RGB", (width, height), "#334455").save(out, format="PNG")
        return out.getvalue()


def body(tool=None, width=1024, height=1024, **extra):
    return dict(image=base64.b64encode(png(width, height)).decode(), width=width,
                height=height, **({"req_type": tool} if tool else {}), **extra)


def zipped(count=1):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for i in range(count):
            archive.writestr(f"image_{i}.png", png())
    return out.getvalue()


@pytest.mark.parametrize("size,cost", [(1024,1),(1280,2),(1536,3),(1728,4)])
def test_upscale_cost_and_canonical_payload(size, cost):
    payload, actual = prepare_tool(body(width=size,height=size), "upscale")
    assert actual == cost
    assert set(payload) == {"image", "model", "declared_blur_sigma"}
    assert payload["model"] == "nai-diffusion-5-curated"


@pytest.mark.parametrize("tool,cost", [("lineart",0),("sketch",0),("emotion",0),
    ("colorize",0),("declutter",0),("declutter-keep-bubbles",0),("bg-removal",65)])
def test_director_small_sources_normalized_and_priced(tool, cost):
    payload, actual = prepare_tool(body(tool, 256, 256, prompt="happy;;blue eyes",defry=3), "augment-image")
    assert actual == cost and payload["width"] == payload["height"] == 1024
    if tool in ("colorize", "emotion"):
        assert payload["prompt"] == "happy;;blue eyes" and payload["defry"] == 3


@pytest.mark.parametrize("changes", [{"width":64},{"height":True},{"image":"oops"},
    {"image":base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()},
    {"model":"unknown"},{"scale":4},{"scale":True},{"declared_blur_sigma":float('nan')}])
def test_untrusted_input_rejected(changes):
    with pytest.raises(ValueError):
        prepare_tool({**body(),**changes}, "upscale")


@pytest.mark.parametrize("changes", [{"req_type":[]},{"req_type":"unknown"},
    {"defry":True},{"defry":6},{"defry":10**500},{"prompt":[]},{"prompt":"字"*3000}])
def test_director_parameters_rejected(changes):
    with pytest.raises(ValueError):
        prepare_tool({**body("colorize"),**changes}, "augment-image")


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "/nai"])
@pytest.mark.parametrize("operation,tool,cost,count", [("upscale",None,1,1),
    ("augment-image","lineart",0,1),("augment-image","bg-removal",65,3)])
async def test_routes_permissions_and_success(state, prefix, operation, tool, cost, count):
    data = body(tool)
    url = prefix+"/ai/"+operation
    assert (await post(url,data,token="wrong")).status_code == 401
    state.db.keys['fixture-1']['allow_img2img'] = False
    assert (await post(url,data)).status_code == 403
    state.db.keys['fixture-1']['allow_img2img'] = True
    state.nai.content = zipped(count)
    response = await post(url,data)
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert len(state.db.charges) == 1
    assert all(row['unconfirmed_anlas'] == 0 for _, row in state.db.logs)
    assert state.db.charges[0][1]['anlas'] == cost
    assert state.db.charges[0][1]['v5'] == 0 and state.db.charges[0][1]['images'] == count
    options = state.nai.calls[0][3]
    assert options['image_lane'] and options['requires_anlas'] == (cost > 0)
    assert options['max_response_bytes'] == 64*1024*1024
    assert not options['v5_free']


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ['free-key','daily','monthly','global','permission'])
async def test_preflight_rejects_without_dispatch(state, failure):
    key=state.db.keys['fixture-1']
    if failure=='free-key': key['allow_anlas']=False
    elif failure=='daily': key['daily_anlas']=64
    elif failure=='monthly': key['monthly_anlas']=64
    elif failure=='global': state.settings.global_monthly_anlas=64
    else: state.settings.allow_img2img=False
    response=await post('/ai/augment-image',body('bg-removal'))
    assert response.status_code in (402,403)
    assert not state.nai.calls and not state.db.charges


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ['429','500','network','timeout','empty','json','truncated','partial'])
async def test_failure_never_charges_or_retries(state, failure):
    if failure=='429':state.nai.error=UpstreamError(429,'limited')
    elif failure=='500':state.nai.status=500
    elif failure=='network':state.nai.error=httpx.ReadError('private upstream')
    elif failure=='timeout':state.nai.error=TimeoutError('private upstream')
    else:state.nai.content={'empty':b'','json':b'{"secret":"private upstream"}',
        'truncated':png()[:40], 'partial':zipped(1)}[failure]
    # FakeNai's error callback expects UpstreamError.status.
    if failure in ('network','timeout'):
        async def fail(*args,**kwargs):
            state.nai.calls.append(1)
            raise state.nai.error
        state.nai.request=fail
    response=await post('/ai/augment-image',body('bg-removal'))
    await asyncio.sleep(0)
    assert response.status_code in (429,502) and 'private upstream' not in response.text
    assert not state.db.charges and len(state.nai.calls)==1
    if failure=='429':assert state.cooldowns


@pytest.mark.asyncio
async def test_budget_serializes_competing_tools_and_settlement(state):
    state.db.keys['fixture-1']['daily_anlas']=1
    state.nai.content=png()
    state.db.accounting_release=asyncio.Event()
    first=asyncio.create_task(post('/ai/upscale',body()))
    await state.db.accounting_entered.wait()
    second=asyncio.create_task(post('/ai/upscale',body()))
    await asyncio.sleep(.03)
    assert not second.done() and len(state.nai.calls)==1
    state.db.accounting_release.set()
    assert (await first).status_code==200
    assert (await second).status_code==402
    assert len(state.nai.calls)==len(state.db.charges)==1


@pytest.mark.asyncio
async def test_disconnect_after_dispatch_settles_once(state):
    state.nai.content=png()
    state.nai.release=asyncio.Event()
    task=asyncio.create_task(post('/ai/upscale',body()))
    await state.nai.entered.wait()
    task.cancel()
    await asyncio.sleep(.02)
    assert state.image_budget_lock.locked()
    state.nai.release.set()
    with pytest.raises(asyncio.CancelledError):await task
    assert len(state.db.charges)==1 and not state.image_budget_lock.locked()


@pytest.mark.asyncio
async def test_real_client_enforces_paid_token_and_response_limit():
    client,_=make_client(allow=[False])
    with pytest.raises(UpstreamError):
        await client.request('POST','https://fixture.invalid',requires_anlas=True,image_lane=True,max_response_bytes=16)
    assert not client._client.calls
    calls=[]
    async def handler(request):
        calls.append(request)
        return httpx.Response(200,content=b'x'*17)
    client,_=make_client()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client._client=http
        with pytest.raises(UpstreamError) as error:
            await client.request('POST','https://fixture.invalid',image_lane=True,max_response_bytes=16)
        assert error.value.billing_uncertain
    assert len(calls)==1


@pytest.mark.asyncio
async def test_compressed_upstream_is_decoded_only_once():
    image=png()
    async def handler(request):
        return httpx.Response(200,headers={'content-encoding':'gzip','content-type':'image/png'},
                              content=gzip.compress(image))
    client,_=make_client()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client._client=http
        result=await client.request('POST','https://fixture.invalid',image_lane=True,
                                    max_response_bytes=64*1024*1024)
    assert result.content==image and 'content-encoding' not in result.headers
