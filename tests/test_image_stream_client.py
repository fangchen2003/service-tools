"""Image stream lifecycle checks; no network or real credentials."""
import asyncio

import anyio
import httpx
import pytest

from app.nai import NaiClient, UpstreamError


class FakeDB:
    def __init__(self):
        self.v5, self.images = {}, {}

    async def get_upstream_v5_counter(self, token, day):
        return self.v5.get(token, 0)

    async def bump_upstream_v5_counter(self, token, day):
        await asyncio.sleep(0)
        self.v5[token] = self.v5.get(token, 0) + 1

    async def bump_upstream_image_counter(self, token, day, count):
        await asyncio.sleep(0)
        self.images[token] = self.images.get(token, 0) + count


class FakeResponse:
    def __init__(self, status=200, close=None):
        self.status_code, self.close = status, close
        self.headers = {'retry-after': '7'}
        self.closed = 0

    async def aclose(self):
        self.closed += 1
        if self.close:
            await self.close()


class FakeHTTP:
    def __init__(self, response, send=None):
        self.response, self.operation = response, send
        self.calls = []

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, *, stream):
        self.calls.append(request)
        assert stream is True
        if self.operation:
            await self.operation()
        return self.response


def client_for(status=200, *, close=None, send=None, tokens=None, allow=None, db=None):
    db = db or FakeDB()
    client = NaiClient(tokens or ['fixture-a'], 'https://offline.invalid', '', '', db=db,
                       day_fn=lambda: '2026-09-22', v5_daily_limits=[1, 1],
                       allow_anlas=allow or [True, True], image_min_interval=0)
    response = FakeResponse(status, close)
    client._client = FakeHTTP(response, send)
    return client, db, response


@pytest.mark.parametrize('status,completed', [(200, 0), (201, 0), (200, 1), (201, 1)])
def test_only_confirmed_final_images_settle(status, completed):
    async def run():
        client, db, response = client_for(status)
        async with client.image_stream('https://offline.invalid', {'stream': 'sse'}, v5_free=True) as handle:
            assert handle.response is response and handle.completed_images == 0
            assert client.pool[0].pending_v5 == 1 and client.pool[0].last_ok == 0
            handle.completed_images = completed
        token = client.pool[0]
        assert token.pending_v5 == 0 and response.closed == 1
        assert db.v5.get(token.token_id, 0) == completed
        assert db.images.get(token.token_id, 0) == completed
        assert (token.last_ok > 0) is bool(completed)
        request, = client._client.calls
        assert request.method == 'POST' and request.headers['accept'] == 'text/event-stream'
    asyncio.run(run())


@pytest.mark.parametrize('status', [400, 401, 402, 403, 422, 429, 500, 503])
def test_status_is_safe_no_retry_and_no_success(status):
    async def run():
        client, db, response = client_for(status)
        cooldowns = []
        async def cooldown(seconds):
            cooldowns.append(seconds)
        with pytest.raises(UpstreamError) as error:
            async with client.image_stream('https://offline.invalid', {}, v5_free=True, on_rate_limited=cooldown):
                pytest.fail('Non-success response must not reach parser')
        assert error.value.status == (502 if status == 401 else status)
        assert error.value.billing_uncertain is (status >= 500)
        assert 'fixture-a' not in str(error.value)
        assert len(client._client.calls) == 1 and response.closed == 1
        assert client.pool[0].pending_v5 == 0 and client.pool[0].last_ok == 0
        assert client.pool[0].disabled is (status == 401)
        assert cooldowns == ([7] if status == 429 else [])
        assert db.v5 == db.images == {}
    asyncio.run(run())


@pytest.mark.parametrize('error,uncertain', [
    (httpx.ConnectError, False), (httpx.ConnectTimeout, False), (httpx.PoolTimeout, False),
    (httpx.ReadError, True), (httpx.ReadTimeout, True), (httpx.WriteTimeout, True),
    (httpx.RemoteProtocolError, True),
])
def test_stream_transport_billing_uncertainty_does_not_retry(error, uncertain):
    async def run():
        async def fail():
            raise error('private transport detail')
        client, db, _ = client_for(send=fail)
        with pytest.raises(UpstreamError) as caught:
            async with client.image_stream('https://offline.invalid', {}, requires_anlas=True):
                pytest.fail('failed request yielded a stream')
        assert caught.value.billing_uncertain is uncertain
        assert 'private' not in str(caught.value)
        assert len(client._client.calls) == 1 and not db.images
    asyncio.run(run())


def test_paid_token_permission_and_multiple_completed_images():
    async def run():
        client, db, _ = client_for(tokens=['paid', 'free'], allow=[True, False])
        async with client.image_stream('https://offline.invalid', {}, requires_anlas=True) as handle:
            handle.completed_images = 2
        assert client._client.calls[0].headers['authorization'] == 'Bearer paid'
        assert db.images == {client.pool[0].token_id: 2} and db.v5 == {}
        forbidden, _, _ = client_for(allow=[False])
        with pytest.raises(UpstreamError) as error:
            async with forbidden.image_stream('https://offline.invalid', {}, requires_anlas=True):
                pytest.fail('Disallowed token selected')
        assert error.value.status == 503 and forbidden._client.calls == []
    asyncio.run(run())


@pytest.mark.parametrize('stage', ['pacing', 'headers', 'body'])
def test_cancellation_releases_reservation_without_billing(stage):
    async def run():
        entered = asyncio.Event()
        dispatched = []
        async def blocked():
            entered.set()
            await asyncio.Event().wait()
        client, db, response = client_for(send=blocked if stage == 'headers' else None)
        if stage == 'pacing':
            async def pace(token):
                await blocked()
            client.wait_for_token_image_slot = pace
        async def consume():
            async with client.image_stream('https://offline.invalid', {}, v5_free=True,
                                           on_dispatch=lambda: dispatched.append(True)):
                await blocked()
        task = asyncio.create_task(consume())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.pool[0].pending_v5 == 0 and db.v5 == db.images == {}
        assert response.closed == (1 if stage == 'body' else 0)
        assert len(client._client.calls) == (0 if stage == 'pacing' else 1)
        assert dispatched == ([] if stage == 'pacing' else [True])
    asyncio.run(run())


@pytest.mark.parametrize('completed', [0, 1])
def test_close_error_still_releases_and_settles(completed):
    async def run():
        async def fail():
            raise RuntimeError('private upstream body fixture-a')
        client, db, response = client_for(close=fail)
        with pytest.raises(UpstreamError) as error:
            async with client.image_stream('https://offline.invalid', {}, v5_free=True) as handle:
                handle.completed_images = completed
        token = client.pool[0]
        assert token.pending_v5 == 0 and response.closed == 1
        assert db.v5.get(token.token_id, 0) == completed and db.images.get(token.token_id, 0) == completed
        assert 'private' not in str(error.value) and error.value.status == 502
    asyncio.run(run())


def test_repeated_cancellation_during_close_finishes_accounting():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def close():
            entered.set()
            await release.wait()
        client, db, _ = client_for(close=close)
        async def consume():
            async with client.image_stream('https://offline.invalid', {}, v5_free=True) as handle:
                handle.completed_images = 1
        task = asyncio.create_task(consume())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        token = client.pool[0]
        assert token.pending_v5 == 0 and db.v5[token.token_id] == db.images[token.token_id] == 1
    asyncio.run(run())


@pytest.mark.parametrize('completed', [0, 1])
def test_anyio_cancel_scope_and_transport_failure_are_clean(completed):
    async def run():
        client, db, response = client_for()
        with anyio.CancelScope() as scope:
            async with client.image_stream('https://offline.invalid', {}, v5_free=True) as handle:
                handle.completed_images = completed
                scope.cancel()
                await anyio.sleep(0)
        token = client.pool[0]
        assert response.closed == 1 and token.pending_v5 == 0
        assert db.images.get(token.token_id, 0) == db.v5.get(token.token_id, 0) == completed
        async def fail():
            raise httpx.ReadTimeout('private fixture-a')
        client, db, _ = client_for(send=fail)
        with pytest.raises(UpstreamError) as error:
            async with client.image_stream('https://offline.invalid', {}, v5_free=True):
                pytest.fail('Transport failed')
        assert error.value.status == 502 and 'private' not in str(error.value)
        assert len(client._client.calls) == 1 and client.pool[0].pending_v5 == 0
        assert db.v5 == db.images == {}
    asyncio.run(run())


def test_failed_v5_settlement_does_not_strand_reservation():
    async def run():
        class FailedDB(FakeDB):
            async def bump_upstream_v5_counter(self, token, day):
                raise RuntimeError('offline db unavailable')
        client, _, _ = client_for(db=FailedDB())
        with pytest.raises(RuntimeError, match='offline db unavailable'):
            async with client.image_stream('https://offline.invalid', {}, v5_free=True) as handle:
                handle.completed_images = 1
        assert client.pool[0].pending_v5 == 0
    asyncio.run(run())
