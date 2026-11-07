"""Private, best-effort queue telemetry in its own SQLite database.

No payloads, prompts, token values or client request IDs are retained. Queue
callbacks only update a bounded memory buffer; quota/accounting never awaits it.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
import logging
import math
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import aiosqlite


FIELDS = ("ticket_id", "key_id", "operation", "created_at", "admitted_at",
          "dispatched_at", "ended_at", "admission_wait_seconds", "wait_seconds",
          "run_seconds", "total_seconds", "outcome", "enqueued_depth")
OPERATIONS = {"generate-image", "upscale", "augment-image", "encode-vibe"}
SAMPLE_SECONDS = 5
RETENTION_DAYS = 30
SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry_meta (key TEXT PRIMARY KEY, value REAL NOT NULL);
CREATE TABLE IF NOT EXISTS queue_tasks (
    ticket_id TEXT PRIMARY KEY, key_id INTEGER NOT NULL, operation TEXT NOT NULL,
    created_at REAL NOT NULL, admitted_at REAL, dispatched_at REAL, ended_at REAL,
    admission_wait_seconds REAL, wait_seconds REAL, run_seconds REAL,
    total_seconds REAL, outcome TEXT, enqueued_depth INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS queue_tasks_created ON queue_tasks(created_at);
CREATE TABLE IF NOT EXISTS queue_samples (
    ts REAL PRIMARY KEY, span_seconds REAL NOT NULL, queued INTEGER NOT NULL,
    active INTEGER NOT NULL, capacity INTEGER NOT NULL, cooldown INTEGER NOT NULL,
    busy_seconds REAL NOT NULL DEFAULT 0, unavailable_seconds REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS queue_samples_time ON queue_samples(ts);
"""
UPSERT = (f"INSERT INTO queue_tasks ({','.join(FIELDS)}) VALUES ({','.join('?' for _ in FIELDS)}) "
          "ON CONFLICT(ticket_id) DO UPDATE SET " +
          ','.join(f'{f}=excluded.{f}' for f in FIELDS if f != 'ticket_id'))


class MetricsUnavailable(Exception):
    pass


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lo, hi = math.floor(position), math.ceil(position)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo), 3)


def summarize(tasks, samples, window_seconds, now=None):
    waits = [r['wait_seconds'] for r in tasks if r['dispatched_at'] is not None
             and r['wait_seconds'] is not None]
    dispatched = sum(r['dispatched_at'] is not None for r in tasks)
    timed_out = sum(r['outcome'] == 'timed_out' for r in tasks)
    observed = sum(s['span_seconds'] for s in samples)
    busy = sum(s['busy_seconds'] for s in samples)
    unavailable = sum(s['unavailable_seconds'] for s in samples)
    depths = [s['queued'] for s in samples] + [r['enqueued_depth'] for r in tasks]
    pending = [r for r in tasks if r['ended_at'] is None and r['dispatched_at'] is None]
    at = time.time() if now is None else now
    return dict(
        enqueued=len(tasks), dispatched=dispatched,
        finished=sum(r['outcome'] == 'finished' for r in tasks),
        timed_out=timed_out,
        cancelled=sum(r['outcome'] in ('cancelled', 'disconnected') for r in tasks),
        expired=sum(r['outcome'] == 'expired' for r in tasks),
        interrupted=sum(r['outcome'] == 'interrupted' for r in tasks),
        active_keys=len({r['key_id'] for r in tasks}),
        pending=len(pending),
        oldest_pending_seconds=max(0, at - min(r['created_at'] for r in pending)) if pending else None,
        wait_p50_seconds=percentile(waits, .5), wait_p95_seconds=percentile(waits, .95),
        wait_max_seconds=round(max(waits), 3) if waits else None,
        wait_avg_seconds=round(sum(waits) / len(waits), 3) if waits else None,
        timeout_rate=timed_out / (dispatched + timed_out) if dispatched + timed_out else None,
        timeout_denominator=dispatched + timed_out,
        samples=len(samples), observed_seconds=round(observed, 3),
        coverage_ratio=min(1, observed / max(1, window_seconds)),
        busy_ratio=busy / observed if observed else None,
        unavailable_ratio=unavailable / observed if observed else None,
        queue_max=max(depths) if depths else None,
        queue_avg=sum(s['queued'] * s['span_seconds'] for s in samples) / observed if observed else None,
    )


def assess(summary, health, hourly=()):
    if health['last_error'] or health['dropped_events']:
        return dict(level='insufficient', label='观测不完整',
                    reason='记录器出现故障或丢失观测，暂不据此判断扩容。')
    if (summary['observed_seconds'] < 6 * 3600 or summary['dispatched'] < 50
            or summary['coverage_ratio'] < .5):
        return dict(level='insufficient', label='继续采集',
                    reason='至少采集 6 小时、50 笔上游提交，并覆盖所选时间窗的一半；请覆盖高峰后再判断。')
    wait = summary['wait_p95_seconds']
    rate = summary['timeout_rate'] or 0
    busy = summary['busy_ratio'] or 0
    unavailable = summary['unavailable_ratio'] or 0
    if wait is None:
        return dict(level='insufficient', label='等待样本不足', reason='尚无可计算的真实上游提交等待时间。')
    def backlog_high(row):
        return ((row.get('queue_avg') or 0) >= 3 or (row.get('oldest_pending_seconds') or 0) >= 90
                or (row.get('expired', 0) / max(1, row.get('enqueued', 0))) >= .1)
    if backlog_high(summary):
        return dict(level='tight', label='积压或任务失效偏多，暂缓新增',
                    reason='平均等待队列 ≥3、未派发任务等待 ≥90 秒或票据过期占比 ≥10%；已放行任务的低等待不能代表剩余容量。')
    peaks = [r for r in hourly if max(r['dispatched'], r.get('enqueued', 0)) >= 10 and r['observed_seconds'] >= 900
             and ((r['wait_p95_seconds'] or 0) >= 90 or (r['timeout_rate'] or 0) >= .05
                  or (r['busy_ratio'] or 0) >= .8 or (r['unavailable_ratio'] or 0) >= .1 or backlog_high(r))]
    if peaks:
        peak = max(peaks, key=lambda r: r['wait_p95_seconds'] or 0)
        return dict(level='tight', label='高峰拥堵，暂缓新增', peak_hour_start=peak['hour_start'],
                    reason='至少一个有效观测小时（≥10 笔提交、≥15 分钟采样）的等待、超时或通道占用触发拥堵阈值；不能只看全天平均。')
    if wait >= 90 or rate >= .05 or busy >= .8 or unavailable >= .1:
        return dict(level='tight', label='暂缓新增用户',
                    reason='P95 等待 ≥90 秒、排队超时率 ≥5%、通道占用 ≥80% 或不可用时间 ≥10%，建议先缓解拥堵。')
    if (wait >= 30 or rate >= .01 or busy >= .5 or unavailable > 0 or
            (summary.get('queue_avg') or 0) >= 1 or (summary.get('queue_max') or 0) >= 5 or
            (summary.get('oldest_pending_seconds') or 0) >= 30 or summary.get('pending', 0) >= 5):
        return dict(level='caution', label='谨慎、小批量观察',
                    reason='等待、超时、占用、不可用时段或未派发积压触发谨慎阈值，新增后需继续观察。')
    return dict(level='roomy', label='观测窗口内有余量',
                reason='所选窗口的等待、超时和通道占用较低；可考虑小批量新增，但需复查高峰小时及官方额度，不保证可新增人数。')


class QueueMetrics:
    def __init__(self, path: Path, timezone: str, *, clock=time.time):
        self.path, self.timezone, self.clock = Path(path), timezone, clock
        self._db = None
        self._io_lock = asyncio.Lock()
        self._pending = {}
        self._samples = {}
        self.max_pending = 10000
        self._dropped = 0
        self._last_error = None
        self._last_sample = None
        self._started_at = None
        self._recovered = False
        self._last_prune = 0.0
        self._slot_at = self._slot_start = None
        self._slot_active = self._slot_capacity = 0
        self._busy_area = self._unavailable_area = 0.0

    def observe_slots(self, active, capacity, *, now=None):
        now = time.monotonic() if now is None else now
        if self._slot_at is not None:
            elapsed = max(0, now - self._slot_at)
            if self._slot_capacity > 0:
                self._busy_area += min(1, self._slot_active / self._slot_capacity) * elapsed
            else:
                self._unavailable_area += elapsed
        else:
            self._slot_start = now
        self._slot_at, self._slot_active, self._slot_capacity = now, max(0, active), max(0, capacity)

    def take_slot_sample(self, active, capacity, *, now=None):
        self.observe_slots(active, capacity, now=now)
        assert self._slot_at is not None and self._slot_start is not None
        elapsed = max(0, self._slot_at - self._slot_start)
        # Do not turn process stalls into invented historical coverage.
        span = min(SAMPLE_SECONDS * 2, elapsed)
        scale = span / elapsed if elapsed else 0
        sample = dict(span_seconds=span, busy_seconds=self._busy_area * scale,
                      unavailable_seconds=self._unavailable_area * scale)
        self._slot_start = self._slot_at
        self._busy_area = self._unavailable_area = 0.0
        return sample

    def health(self):
        return dict(pending_events=len(self._pending), dropped_events=self._dropped,
                    last_error=self._last_error, last_sample_at=self._last_sample)

    def observe(self, event):
        if event.get('operation') not in OPERATIONS:
            return
        identity = event['ticket_id']
        if identity not in self._pending and len(self._pending) >= self.max_pending:
            self._dropped += 1
            return
        self._pending[identity] = {f: event.get(f) for f in FIELDS}

    async def start(self):
        if self._db is not None:
            return
        async with self._io_lock:
            if self._db is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = await aiosqlite.connect(str(self.path), timeout=3)
            try:
                db.row_factory = aiosqlite.Row
                await db.execute('PRAGMA journal_mode=WAL')
                await db.execute('PRAGMA busy_timeout=3000')
                await db.executescript(SCHEMA)
                cursor = await db.execute('PRAGMA table_info(queue_samples)')
                columns = {row[1] for row in await cursor.fetchall()}
                for column in ('busy_seconds', 'unavailable_seconds'):
                    if column not in columns:
                        await db.execute(f'ALTER TABLE queue_samples ADD COLUMN {column} REAL NOT NULL DEFAULT 0')
                await db.execute("INSERT OR IGNORE INTO telemetry_meta VALUES ('started_at', ?)", (self.clock(),))
                if not self._recovered:
                    # A previous process cannot settle its outstanding tasks after a restart.
                    # Never invent a dispatch or waiting duration for those records.
                    await db.execute("UPDATE queue_tasks SET outcome='interrupted', ended_at=? "
                                     "WHERE ended_at IS NULL AND outcome IS NULL", (self.clock(),))
                await db.commit()
                cursor = await db.execute("SELECT value FROM telemetry_meta WHERE key='started_at'")
                self._started_at = (await cursor.fetchone())[0]
                cursor = await db.execute('SELECT max(ts) FROM queue_samples')
                self._last_sample = (await cursor.fetchone())[0]
            except BaseException:
                await db.close()
                raise
            self._db = db
            self._recovered = True
            self._last_error = None

    async def flush(self):
        if self._db is None:
            self._last_error = '观测数据库尚未就绪'
            return False
        async with self._io_lock:
            pending, samples = self._pending, self._samples
            self._pending, self._samples = {}, {}
            try:
                if pending:
                    await self._db.executemany(UPSERT, [tuple(e[f] for f in FIELDS) for e in pending.values()])
                if samples:
                    await self._db.executemany('INSERT OR REPLACE INTO queue_samples VALUES (?,?,?,?,?,?,?,?)',
                        [(s['ts'], s['span_seconds'], s['queued'], s['active'], s['capacity'], s['cooldown'],
                          s['busy_seconds'], s['unavailable_seconds'])
                         for s in samples.values()])
                now = self.clock()
                if now - self._last_prune >= 3600:
                    cutoff = now - RETENTION_DAYS * 86400
                    await self._db.execute('DELETE FROM queue_tasks WHERE created_at<?', (cutoff,))
                    await self._db.execute('DELETE FROM queue_samples WHERE ts<?', (cutoff,))
                await self._db.commit()
                if now - self._last_prune >= 3600:
                    self._last_prune = now
                self._last_error = None
                return True
            except BaseException as exc:
                try:
                    await self._db.rollback()
                except Exception:
                    pass
                for key, event in pending.items():
                    if key in self._pending:
                        continue  # A newer lifecycle update always wins.
                    if len(self._pending) < self.max_pending:
                        self._pending[key] = event
                    else:
                        self._dropped += 1
                for key, sample in samples.items():
                    if len(self._samples) < self.max_pending:
                        self._samples.setdefault(key, sample)
                    else:
                        self._dropped += 1
                if isinstance(exc, asyncio.CancelledError):
                    raise
                self._last_error = '观测数据库写入失败'
                logging.getLogger(__name__).warning('queue telemetry write failed: %s', type(exc).__name__)
                return False

    async def sample(self, sample):
        if len(self._samples) >= self.max_pending:
            self._dropped += 1
            return
        sample = dict(sample)
        sample.setdefault('busy_seconds', min(1, sample['active'] / sample['capacity']) * sample['span_seconds']
                          if sample['capacity'] > 0 else 0)
        sample.setdefault('unavailable_seconds', sample['span_seconds'] if sample['capacity'] <= 0 else 0)
        self._samples[sample['ts']] = sample
        self._last_sample = sample['ts']
        await self.flush()

    async def run(self, snapshot):
        previous = time.monotonic()
        while True:
            try:
                if self._db is None:
                    await self.start()
                current = time.monotonic()
                sample = snapshot()
                sample['ts'] = self.clock()
                sample.setdefault('span_seconds', min(SAMPLE_SECONDS * 2, max(0, current - previous)))
                previous = current
                await self.sample(sample)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = '观测采集暂不可用'
                logging.getLogger(__name__).warning('queue telemetry sampling failed: %s', type(exc).__name__)
            await asyncio.sleep(SAMPLE_SECONDS)

    async def report(self, hours=24, *, now=None):
        if hours not in (24, 168):
            raise ValueError('仅支持近 24 小时或 7 天')
        if not await self.flush():
            raise MetricsUnavailable('观测记录暂不可用，生图服务不受影响')
        now = self.clock() if now is None else now
        since = now - hours * 3600
        async with self._io_lock:
            try:
                cursor = await self._db.execute('SELECT * FROM queue_tasks WHERE created_at>=? AND created_at<=?', (since, now))
                tasks = [dict(r) for r in await cursor.fetchall()]
                cursor = await self._db.execute('SELECT * FROM queue_samples WHERE ts>=? AND ts<=? ORDER BY ts', (since, now))
                samples = [dict(r) for r in await cursor.fetchall()]
            except Exception as exc:
                self._last_error = '观测数据库读取失败'
                raise MetricsUnavailable('观测记录暂不可用，生图服务不受影响') from exc
        # Clip the first trailing sample to the selected time window.
        for sample in samples:
            old_span = sample['span_seconds']
            sample['span_seconds'] = max(0, min(old_span, sample['ts'] - since))
            scale = sample['span_seconds'] / old_span if old_span else 0
            sample['busy_seconds'] *= scale
            sample['unavailable_seconds'] *= scale
        summary = summarize(tasks, samples, hours * 3600, now)
        zone = ZoneInfo(self.timezone)
        def hour_start(ts):
            return int(datetime.fromtimestamp(ts, zone).replace(minute=0, second=0, microsecond=0).timestamp())
        hourly_tasks, hourly_samples = defaultdict(list), defaultdict(list)
        for task in tasks:
            hourly_tasks[hour_start(task['created_at'])].append(task)
        for sample in samples:
            hourly_samples[hour_start(sample['ts'])].append(sample)
        hourly = []
        hour = hour_start(since)
        while hour <= now:
            duration = max(1, min(hour + 3600, now) - max(hour, since))
            hourly.append(dict(hour_start=hour, **summarize(hourly_tasks[hour], hourly_samples[hour], duration, now)))
            hour += 3600
        health = self.health()
        if self._last_sample is None or now - self._last_sample > SAMPLE_SECONDS * 3:
            health['last_error'] = health['last_error'] or '最近采样中断，统计可能不完整'
        return dict(generated_at=now, time_zone=self.timezone, window_hours=hours, since=since,
                    started_at=self._started_at, retention_days=RETENTION_DAYS,
                    sample_interval_seconds=SAMPLE_SECONDS, health=health, summary=summary,
                    hourly=hourly, assessment=assess(summary, health, hourly))

    async def close(self):
        if self._db is None:
            return
        await self.flush()
        async with self._io_lock:
            await self._db.close()
            self._db = None
