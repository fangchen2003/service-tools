"""Real queue timestamps and isolated SQLite telemetry; no external upstreams."""
import asyncio
import json
from types import SimpleNamespace
import uuid

import pytest

from app.image_queue import ImageQueue, QueueError


def test_queue_records_wait_until_dispatch_not_until_slot_claim():
    now = [10.0]
    events = []
    queue = ImageQueue(lambda: 1, lambda: 1, lambda key: False,
                       clock=lambda: now[0], wall_clock=lambda: 1000 + now[0],
                       observe=events.append)
    ticket = queue.create(7, str(uuid.uuid4()), "generate-image")
    now[0] = 13
    queue.claim(ticket.ticket_id, 7, ticket.request_id, ticket.operation)
    now[0] = 25
    queue.dispatched(ticket)
    now[0] = 32
    queue.finish(ticket)
    event = events[-1]
    assert event['created_at'] == 1010
    assert event['admission_wait_seconds'] == 3
    assert event['wait_seconds'] == 15
    assert event['run_seconds'] == 7
    assert event['total_seconds'] == 22
    assert event['outcome'] == 'finished'
    assert event['key_id'] == 7
    assert 'request_id' not in event and 'token' not in event
    queue.finish(ticket)
    assert events[-1] == event


@pytest.mark.asyncio
async def test_timeout_is_distinct_from_cancellation_and_has_no_fake_wait():
    events = []
    queue = ImageQueue(lambda: 0, lambda: 1, lambda key: False, observe=events.append)
    ticket = queue.create(7, str(uuid.uuid4()), 'generate-image')
    with pytest.raises(QueueError) as caught:
        await queue.wait_claim(ticket, .01)
    assert caught.value.code == 'queue_timeout'
    assert events[-1]['outcome'] == 'timed_out'
    assert events[-1]['wait_seconds'] is None
    assert events[-1]['total_seconds'] >= .01


def test_tags_are_excluded_and_observer_failure_cannot_break_generation():
    def forbidden(event):
        raise RuntimeError('fixture metrics failure')
    queue = ImageQueue(lambda: 1, lambda: 1, lambda key: False, observe=forbidden)
    ticket = queue.create(7, str(uuid.uuid4()), 'generate-image')
    queue.claim(ticket.ticket_id, 7, ticket.request_id, ticket.operation)
    queue.finish(ticket)
    events = []
    queue._observe = events.append
    tag = queue.try_tag(7)
    queue.finish(tag)
    assert events == []


@pytest.mark.asyncio
async def test_metrics_persist_exact_percentiles_empty_states_and_restart(tmp_path):
    from app.queue_metrics import QueueMetrics
    path = tmp_path / 'metrics.sqlite'
    metrics = QueueMetrics(path, 'Asia/Shanghai', clock=lambda: 100000)
    await metrics.start()
    empty = await metrics.report(24, now=100000)
    assert empty['summary']['wait_p95_seconds'] is None
    assert empty['summary']['busy_ratio'] is None
    assert empty['assessment']['level'] == 'insufficient'
    for i, wait in enumerate((0, 10, 20, 100)):
        metrics.observe(dict(ticket_id=str(i), key_id=i % 2, operation='generate-image',
                             created_at=99900, admitted_at=99900, dispatched_at=99900 + wait,
                             ended_at=99999, admission_wait_seconds=0, wait_seconds=wait,
                             run_seconds=1, total_seconds=wait + 1, outcome='finished',
                             enqueued_depth=i + 1))
    await metrics.sample(dict(ts=99990, span_seconds=5, queued=3, active=1, capacity=1, cooldown=0))
    await metrics.sample(dict(ts=99995, span_seconds=5, queued=0, active=0, capacity=1, cooldown=0))
    report = await metrics.report(24, now=100000)
    s = report['summary']
    assert s['enqueued'] == s['dispatched'] == 4
    assert s['wait_p50_seconds'] == 15
    assert s['wait_p95_seconds'] == pytest.approx(88)
    assert s['active_keys'] == 2 and s['queue_max'] == 4
    assert s['busy_ratio'] == .5 and s['observed_seconds'] == 10
    assert s['timeout_rate'] == 0
    assert 'key_id' not in json.dumps(report) and 'ticket_id' not in json.dumps(report)
    metrics.observe(dict(ticket_id='unsettled', key_id=3, operation='generate-image',
                         created_at=99999, admitted_at=None, dispatched_at=None, ended_at=None,
                         admission_wait_seconds=None, wait_seconds=None, run_seconds=None,
                         total_seconds=None, outcome=None, enqueued_depth=1))
    await metrics.close()
    second = QueueMetrics(path, 'Asia/Shanghai', clock=lambda: 100001)
    await second.start()
    report = await second.report(24, now=100001)
    assert report['summary']['interrupted'] == 1
    assert report['summary']['dispatched'] == 4
    assert report['started_at'] == 100000
    await second.close()


@pytest.mark.asyncio
async def test_metrics_failure_is_visible_and_cannot_overwrite_newer_event(tmp_path):
    from app.queue_metrics import QueueMetrics
    metrics = QueueMetrics(tmp_path / 'metrics.sqlite', 'Asia/Shanghai')
    metrics.max_pending = 1
    event = dict(ticket_id='one', key_id=1, operation='generate-image', created_at=1,
                 admitted_at=None, dispatched_at=None, ended_at=None,
                 admission_wait_seconds=None, wait_seconds=None, run_seconds=None,
                 total_seconds=None, outcome=None, enqueued_depth=1)
    metrics.observe(event)
    metrics.observe({**event, 'ticket_id': 'two'})
    assert metrics.health()['dropped_events'] == 1
    await metrics.start()
    await metrics.close()


@pytest.mark.asyncio
async def test_cancelled_flush_restores_pending_rows_for_shutdown(tmp_path, monkeypatch):
    from app.queue_metrics import QueueMetrics
    metrics = QueueMetrics(tmp_path / 'metrics.sqlite', 'Asia/Shanghai')
    await metrics.start()
    event = dict(ticket_id='one', key_id=1, operation='generate-image', created_at=metrics.clock(),
                 admitted_at=None, dispatched_at=None, ended_at=None,
                 admission_wait_seconds=None, wait_seconds=None, run_seconds=None,
                 total_seconds=None, outcome=None, enqueued_depth=1)
    metrics.observe(event)
    entered = asyncio.Event()
    execute = metrics._db.executemany
    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(metrics._db, 'executemany', blocked)
    task = asyncio.create_task(metrics.flush())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(metrics._db, 'executemany', execute)
    try:
        assert metrics.health()['pending_events'] == 1
        report = await metrics.report()
        assert report['summary']['enqueued'] == 1
    finally:
        await metrics.close()


def test_capacity_assessment_does_not_hide_a_bad_peak_behind_daily_average():
    from app.queue_metrics import assess
    summary = dict(observed_seconds=43200, dispatched=1000, coverage_ratio=.5,
                   wait_p95_seconds=5, timeout_rate=0, busy_ratio=.1, unavailable_ratio=0,
                   queue_max=2)
    health = dict(last_error=None, dropped_events=0)
    peak = dict(summary, hour_start=100000, observed_seconds=3600, dispatched=20,
                wait_p95_seconds=120, busy_ratio=.9)
    result = assess(summary, health, hourly=[peak])
    assert result['level'] == 'tight'
    assert result['peak_hour_start'] == 100000
    assert assess(summary, health)['level'] == 'roomy'


def test_backlog_and_expired_unserved_tickets_cannot_look_roomy():
    from app.queue_metrics import assess
    summary = dict(observed_seconds=86400, dispatched=50, coverage_ratio=1,
                   wait_p95_seconds=0, timeout_rate=0, busy_ratio=0, unavailable_ratio=0,
                   queue_max=25, queue_avg=20, enqueued=550, expired=500,
                   pending=0, oldest_pending_seconds=None)
    health = dict(last_error=None, dropped_events=0)
    assert assess(summary, health)['level'] == 'tight'
    summary.update(queue_max=1, queue_avg=0, expired=0, enqueued=51,
                   pending=1, oldest_pending_seconds=100)
    assert assess(summary, health)['level'] != 'roomy'


def test_slot_integration_captures_bursts_between_sampling_ticks(tmp_path):
    from app.queue_metrics import QueueMetrics
    metrics = QueueMetrics(tmp_path / 'metrics.sqlite', 'Asia/Shanghai')
    metrics.observe_slots(0, 1, now=0)
    metrics.observe_slots(1, 1, now=.1)
    metrics.observe_slots(0, 1, now=4.1)
    sample = metrics.take_slot_sample(0, 1, now=5)
    assert sample['span_seconds'] == 5
    assert sample['busy_seconds'] == pytest.approx(4)
    assert sample['unavailable_seconds'] == 0


@pytest.mark.asyncio
async def test_retention_removes_abandoned_old_records_too(tmp_path):
    from app.queue_metrics import QueueMetrics
    now = 4000000
    metrics = QueueMetrics(tmp_path / 'metrics.sqlite', 'Asia/Shanghai', clock=lambda: now)
    await metrics.start()
    metrics.observe(dict(ticket_id='old', key_id=1, operation='generate-image',
                         created_at=now - 31 * 86400, admitted_at=None, dispatched_at=None,
                         ended_at=None, admission_wait_seconds=None, wait_seconds=None,
                         run_seconds=None, total_seconds=None, outcome=None, enqueued_depth=1))
    await metrics.flush()
    cursor = await metrics._db.execute('SELECT count(*) FROM queue_tasks')
    assert (await cursor.fetchone())[0] == 0
    await metrics.close()
