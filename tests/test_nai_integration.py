"""Token accounting regression checks; all HTTP and storage are local fakes."""
import asyncio
import base64
import io
import zipfile
from contextlib import asynccontextmanager

import anyio
import httpx
import pytest

from app.nai import NaiClient, UpstreamError


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/'
    'iZk9HQAAAABJRU5ErkJggg=='
)


def image_zip(count=1):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for index in range(count):
            archive.writestr(f'image_{index}.png', PNG)
    return output.getvalue()


class FakeDB:
    def __init__(self):
        self.v5 = {}
        self.images = {}

    async def get_upstream_v5_counter(self, token_id, day):
        return self.v5.get(token_id, 0)

    async def bump_upstream_v5_counter(self, token_id, day):
        await asyncio.sleep(0)
        self.v5[token_id] = self.v5.get(token_id, 0) + 1

    async def bump_upstream_image_counter(self, token_id, day, count):
        await asyncio.sleep(0)
        self.images[token_id] = self.images.get(token_id, 0) + count


class FakeHTTP:
    def __init__(self, status=200, operation=None, content=PNG):
        self.status = status
        self.operation = operation
        self.content = content
        self.calls = []

    async def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.operation:
            return await self.operation()
        return httpx.Response(self.status, headers={'retry-after': '5'}, content=self.content)

    @asynccontextmanager
    async def stream(self, *args, **kwargs):
        response = await self.request(*args, **kwargs)
        try:
            yield response
        finally:
            await response.aclose()


def make_client(db=None, http=None, *, tokens=None, allow=None):
    db = db or FakeDB()
    client = NaiClient(tokens or ['fake-a'], 'https://offline.invalid', 'https://offline.invalid',
                       'https://offline.invalid', db=db, day_fn=lambda: '2026-09-22',
                       v5_daily_limits=[1, 1], allow_anlas=allow or [True, True],
                       image_min_interval=0)
    client._client = http or FakeHTTP()
    return client, db


@pytest.mark.parametrize('status', [200, 201])
def test_success_records_once_and_releases_pending(status):
    async def run():
        client, db = make_client(http=FakeHTTP(status))
        response = await client.request('POST', 'https://offline.invalid', v5_free=True,
                                        image_count=1, image_lane=True)
        token = client.pool[0]
        assert response.status_code == status
        assert token.pending_v5 == 0
        assert db.v5[token.token_id] == 1 and db.images[token.token_id] == 1
        assert await client.pick_token(v5_free=True) is None
    asyncio.run(run())


@pytest.mark.parametrize('status', [400, 401, 429, 500, 503])
def test_http_failures_do_not_bill_or_retry_images(status):
    async def run():
        http = FakeHTTP(status)
        client, db = make_client(http=http)
        try:
            response = await client.request('POST', 'https://offline.invalid', v5_free=True,
                                            image_count=1, image_lane=True)
            assert response.status_code == status and status not in (401, 429)
        except UpstreamError:
            assert status in (401, 429)
        assert len(http.calls) == 1
        assert client.pool[0].pending_v5 == 0
        assert db.v5 == {} and db.images == {}
        assert client.pool[0].disabled is (status == 401)
    asyncio.run(run())


def test_paid_requests_keep_per_token_anlas_protection():
    async def run():
        http = FakeHTTP(201, content=image_zip(2))
        client, db = make_client(http=http, tokens=['fake-paid', 'fake-free'], allow=[True, False])
        await client.request('POST', 'https://offline.invalid', requires_anlas=True,
                             image_count=2, image_lane=True)
        assert http.calls[0][1]['headers']['Authorization'] == 'Bearer fake-paid'
        assert db.images == {client.pool[0].token_id: 2} and db.v5 == {}
        forbidden, _ = make_client(http=FakeHTTP(), allow=[False])
        with pytest.raises(UpstreamError):
            await forbidden.request('POST', 'https://offline.invalid', requires_anlas=True,
                                    image_lane=True)
        assert forbidden._client.calls == []
    asyncio.run(run())


@pytest.mark.parametrize('in_pacing', [False, True])
def test_asyncio_cancellation_releases_reservation(in_pacing):
    async def run():
        entered = asyncio.Event()
        async def blocked():
            entered.set()
            await asyncio.Event().wait()
        http = FakeHTTP(operation=blocked)
        client, db = make_client(http=http)
        if in_pacing:
            async def wait_slot(_token):
                await blocked()
            client.wait_for_token_image_slot = wait_slot
        task = asyncio.create_task(client.request('POST', 'https://offline.invalid',
                                                  v5_free=True, image_count=1, image_lane=True))
        await entered.wait()
        assert client.pool[0].pending_v5 == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.pool[0].pending_v5 == 0 and db.v5 == {} and db.images == {}
        token = await client.pick_token(v5_free=True)
        assert token is not None
        await client.finish_v5_reservation(token, succeeded=False, v5_free=True)
        assert len(http.calls) == (0 if in_pacing else 1)
    asyncio.run(run())


def test_anyio_scope_cancellation_completes_cleanup():
    async def run():
        entered = asyncio.Event()
        async def blocked():
            entered.set()
            await asyncio.Event().wait()
        client, db = make_client(http=FakeHTTP(operation=blocked))
        async def request():
            await client.request('POST', 'https://offline.invalid', v5_free=True,
                                 image_count=1, image_lane=True)
        async with anyio.create_task_group() as group:
            group.start_soon(request)
            await entered.wait()
            group.cancel_scope.cancel()
        assert client.pool[0].pending_v5 == 0 and db.v5 == {} and db.images == {}
    asyncio.run(run())


def test_cancellation_during_successful_accounting_finishes_once():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        class SlowDB(FakeDB):
            async def bump_upstream_v5_counter(self, token_id, day):
                entered.set()
                await release.wait()
                await super().bump_upstream_v5_counter(token_id, day)
        client, db = make_client(db=SlowDB(), http=FakeHTTP(201))
        task = asyncio.create_task(client.request('POST', 'https://offline.invalid',
                                                  v5_free=True, image_count=1, image_lane=True))
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        token = client.pool[0]
        assert token.pending_v5 == 0
        assert db.v5[token.token_id] == 1 and db.images[token.token_id] == 1
    asyncio.run(run())


def test_unknown_transport_result_never_retries_or_counts_as_success():
    async def run():
        async def fail():
            raise httpx.ReadTimeout('offline ambiguous outcome')
        http = FakeHTTP(operation=fail)
        client, db = make_client(http=http)
        with pytest.raises(UpstreamError) as error:
            await client.request('POST', 'https://offline.invalid', v5_free=True,
                                 image_count=1, image_lane=True)
        assert error.value.billing_uncertain
        assert len(http.calls) == 1 and client.pool[0].pending_v5 == 0
        assert db.v5 == {} and db.images == {}
    asyncio.run(run())


def test_pending_reservation_blocks_concurrent_overallocation():
    async def run():
        client, db = make_client()
        choices = await asyncio.gather(client.pick_token(v5_free=True), client.pick_token(v5_free=True))
        assert sum(token is not None for token in choices) == 1
        chosen = next(token for token in choices if token is not None)
        await client.finish_v5_reservation(chosen, succeeded=False, v5_free=True)
        assert chosen.pending_v5 == 0 and db.v5 == {}
        assert await client.pick_token(v5_free=True) is chosen
        await client.finish_v5_reservation(chosen, succeeded=True, v5_free=True)
        assert await client.pick_token(v5_free=True) is None
    asyncio.run(run())


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 201])
@pytest.mark.parametrize('content,count', [
    (b'', 1), (b'{"error":"private upstream message"}', 1),
    (PNG[:24], 1), (image_zip(1), 2), (PNG, 2),
])
async def test_invalid_image_success_releases_reservation_without_accounting(status, content, count):
    http = FakeHTTP(status, content=content)
    client, db = make_client(http=http)
    with pytest.raises(UpstreamError) as error:
        await client.request('POST', 'https://offline.invalid', image_count=count,
                             v5_free=True, image_lane=True)
    assert error.value.status == 502 and 'private' not in error.value.message
    assert error.value.billing_uncertain
    assert len(http.calls) == 1 and client.pool[0].pending_v5 == 0
    assert client.pool[0].last_ok == 0 and db.v5 == db.images == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('content,count', [(PNG, 1), (image_zip(1), 1), (image_zip(2), 2)])
async def test_valid_image_formats_preserve_response_and_correct_count(content, count):
    client, db = make_client(http=FakeHTTP(201, content=content))
    response = await client.request('POST', 'https://offline.invalid',
                                    image_count=count, image_lane=True)
    assert response.content == content and response.status_code == 201
    assert db.images == {client.pool[0].token_id: count}


@pytest.mark.asyncio
async def test_invalid_image_success_never_reaches_user_ledger(monkeypatch):
    from app import main
    from test_generation_integration import FakeState, image_body, post

    state = FakeState()
    state.nai, upstream_db = make_client(http=FakeHTTP(200, content=b'{"error":"invalid image"}'))
    monkeypatch.setattr(main, 'STATE', state)
    response = await post('/ai/generate-image', image_body(precise=1))
    assert response.status_code == 502
    assert not state.db.charges and not upstream_db.images
    assert not state.image_budget_lock.locked() and state.global_active == 0
    assert state.db.logs[-1][1]['unconfirmed_anlas'] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,uncertain', [
    (httpx.ConnectError, False), (httpx.ConnectTimeout, False), (httpx.PoolTimeout, False),
    (httpx.ReadError, True), (httpx.ReadTimeout, True), (httpx.WriteError, True),
    (httpx.WriteTimeout, True), (httpx.RemoteProtocolError, True), (httpx.DecodingError, True),
])
async def test_transport_failure_preserves_dispatch_uncertainty(failure, uncertain):
    async def fail():
        raise failure('private upstream token')
    http = FakeHTTP(operation=fail)
    client, db = make_client(http=http)
    with pytest.raises(UpstreamError) as error:
        await client.request('POST', 'https://offline.invalid', image_count=1,
                             v5_free=True, image_lane=True)
    assert error.value.billing_uncertain is uncertain
    assert 'private' not in error.value.message
    assert len(http.calls) == 1 and not db.images and not db.v5
    assert not client.pool[0].pending_v5


@pytest.mark.asyncio
async def test_non_image_transport_error_keeps_original_contract():
    async def fail():
        raise httpx.ReadTimeout('offline text error')
    client, _ = make_client(http=FakeHTTP(operation=fail))
    with pytest.raises(httpx.ReadTimeout):
        await client.request('POST', 'https://offline.invalid')


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,cost', [
    ('generate-image', 2), ('upscale', 1), ('augment-image', 65), ('encode-vibe', 2),
])
@pytest.mark.parametrize('outcome,uncertain', [
    ('connect', False), ('rejected', False), ('limited', False),
    ('read', True), ('server_error', True), ('invalid_result', True),
    ('rejected_body', False), ('limited_body', False), ('server_body', True),
])
async def test_nonstream_anomaly_reaches_ledger_without_charging_or_retrying(
        monkeypatch, operation, cost, outcome, uncertain):
    """Run real dispatch, validation and ASGI paths with an offline upstream."""
    from app import main
    from test_generation_integration import FakeState, image_body, encoding_body, post
    from test_image_tools import body as tool_body

    payload = (image_body(width=256, height=256, steps=29) if operation == 'generate-image'
               else encoding_body() if operation == 'encode-vibe'
               else tool_body('bg-removal' if operation == 'augment-image' else None))
    calls = []

    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'partial'
            raise httpx.ReadError('private upstream token')

    async def handler(request):
        calls.append(request.url.path)
        if outcome in ('connect', 'read'):
            failure = httpx.ConnectError if outcome == 'connect' else httpx.ReadError
            raise failure('private upstream token')
        if outcome.endswith('_body'):
            return httpx.Response({'rejected_body': 400, 'limited_body': 429, 'server_body': 503}[outcome],
                                  stream=BrokenBody())
        status = {'rejected': 400, 'limited': 429, 'server_error': 503,
                  'invalid_result': 200}[outcome]
        return httpx.Response(status, headers={'content-type': 'application/json'},
                              content=b'{"error":"private upstream token"}')

    state = FakeState()
    state.nai, upstream_db = make_client()
    monkeypatch.setattr(main, 'STATE', state)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        state.nai._client = http
        response = await post('/ai/' + operation, payload)
    assert response.status_code >= 400
    # One operation only: no generation retry or official balance query.
    assert calls == ['/ai/' + operation]
    assert len(state.db.logs) == 1
    pending = [row for _, row in state.db.logs if row['unconfirmed_anlas'] > 0]
    assert len(pending) == int(uncertain)
    if pending:
        assert pending[0]['unconfirmed_anlas'] == cost
    assert all('private' not in row['detail'] for _, row in state.db.logs)
    assert not state.db.charges and not upstream_db.images and not upstream_db.v5
    assert not state.image_budget_lock.locked() and state.global_active == 0
    assert not state.nai.pool[0].pending_v5


@pytest.mark.asyncio
@pytest.mark.parametrize('status,uncertain', [(200, True), (400, False), (429, False), (503, True)])
async def test_bounded_response_uses_known_headers_when_body_breaks(status, uncertain):
    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'partial'
            raise httpx.ReadError('private response body')

    async def handler(request):
        return httpx.Response(status, stream=BrokenBody())

    client, db = make_client()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client._client = http
        with pytest.raises(UpstreamError) as error:
            await client.request('POST', 'https://offline.invalid', image_lane=True,
                                 max_response_bytes=1024)
    assert error.value.billing_uncertain is uncertain
    assert not db.images and not db.v5
