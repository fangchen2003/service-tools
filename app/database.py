"""SQLite 持久化：虚拟 key、每日计数器、用量日志。"""

from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any, Optional
from uuid import uuid4

import aiosqlite

from .reasons import reason_label

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    token TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    daily_images INTEGER NOT NULL DEFAULT 100,
    daily_anlas REAL NOT NULL DEFAULT 0,
    daily_v5 INTEGER NOT NULL DEFAULT 0,
    monthly_anlas REAL NOT NULL DEFAULT 500,
    daily_text_tokens INTEGER NOT NULL DEFAULT 150000,
    rpm INTEGER NOT NULL DEFAULT 10,
    allow_anlas INTEGER NOT NULL DEFAULT 0,
    allow_img2img INTEGER NOT NULL DEFAULT 0,
    exclude_global_v5 INTEGER NOT NULL DEFAULT 0,
    image_model_scope TEXT NOT NULL DEFAULT 'legacy',
    is_admin INTEGER NOT NULL DEFAULT 0,
    is_test INTEGER NOT NULL DEFAULT 0,   -- 测试 Key：不计入成员统计、上游表现，不会被闲置回收
    anlas_auto INTEGER NOT NULL DEFAULT 0,
    quota_auto INTEGER NOT NULL DEFAULT 1,   -- 1 额度由动态额度算法管理（quota_algo.py）；-1 站长手动 -- 1 自动分配管理（只用于 V5 续杯）；0 交给算法；-1 站长手动（关闭或手动额度）
    expires_at REAL,
    created_at REAL NOT NULL,
    last_used_at REAL
);
CREATE TABLE IF NOT EXISTS site_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS anlas_reconciliations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discord_registrations (
    discord_id TEXT PRIMARY KEY,
    key_id INTEGER NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS discord_bans (
    discord_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_role_removals (
    discord_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    key_id INTEGER NOT NULL,
    day TEXT NOT NULL,            -- YYYY-MM-DD (按配置时区)
    images INTEGER NOT NULL DEFAULT 0,
    legacy_free_images INTEGER NOT NULL DEFAULT 0,
    anlas REAL NOT NULL DEFAULT 0,
    text_tokens INTEGER NOT NULL DEFAULT 0,
    requests INTEGER NOT NULL DEFAULT 0,
    v5 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key_id, day)
);
CREATE TABLE IF NOT EXISTS generation_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    key_id INTEGER,
    key_name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'image',
    model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    negative TEXT NOT NULL DEFAULT '',
    extra TEXT NOT NULL DEFAULT '',
    thumb BLOB,
    image BLOB,
    image_type TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON generation_audit(ts);
CREATE INDEX IF NOT EXISTS idx_audit_key ON generation_audit(key_id, ts);
CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    key_id INTEGER,
    key_name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,           -- image / text / voice / tags / chat
    model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,         -- ok / error / rejected
    images INTEGER NOT NULL DEFAULT 0,
    anlas REAL NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    unconfirmed_anlas REAL NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',      -- 拒绝原因码（app/reasons.py）：统计和自动驾驶按它数，不再 LIKE 中文 detail
    wait_ms INTEGER NOT NULL DEFAULT 0,   -- 从收到请求到发往上游（排队 + 冷却）
    dur_ms INTEGER NOT NULL DEFAULT 0,    -- 上游处理耗时；没发到上游为 0
    client TEXT NOT NULL DEFAULT '',      -- User-Agent 摘要，客户端自报，仅供参考
    up_status INTEGER NOT NULL DEFAULT 0, -- 上游最后返回的 HTTP 状态码；没收到响应为 0
    rid TEXT NOT NULL DEFAULT '',         -- 请求编号（响应头 X-Request-Id），成员报错时据此定位
    src TEXT NOT NULL DEFAULT '',         -- 来源网络打码标签（如 120.235.*.*），防分享溯源用，不存完整 IP
    ver TEXT NOT NULL DEFAULT ''          -- 网关版本（规则版本）：回测按参数版本分段，避免把不同规则下的数据混在一起
);
CREATE INDEX IF NOT EXISTS idx_log_ts ON usage_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_log_key ON usage_log (key_id, ts DESC);
CREATE TABLE IF NOT EXISTS upstream_token_counters (
    token_id TEXT NOT NULL,
    day TEXT NOT NULL,            -- YYYY-MM-DD (按配置时区)
    images INTEGER NOT NULL DEFAULT 0,
    v5 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_id, day)
);
CREATE TABLE IF NOT EXISTS key_tags (          -- 后台手动标签（只给站长看，成员看不到）
    key_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    by TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    PRIMARY KEY (key_id, tag)
);
CREATE TABLE IF NOT EXISTS share_state (       -- 防分享风险分（share_guard.py）
    key_id INTEGER PRIMARY KEY,
    score REAL NOT NULL DEFAULT 0,
    score_ts REAL NOT NULL DEFAULT 0,
    strikes INTEGER NOT NULL DEFAULT 0,
    paused_until REAL NOT NULL DEFAULT 0,
    warned_ts REAL NOT NULL DEFAULT 0,
    paused_ts REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS upstream_snapshots (   -- 每小时记一次上游真实状态，和我们自己的计数做独立核对
    ts REAL NOT NULL,
    v5_percent REAL,              -- 账号 V5 剩余 %（订阅接口）
    v5_rate REAL,                 -- 实测恢复 %/天（拿不到时为空）
    anlas REAL,                   -- 账号 Anlas 余额
    fresh INTEGER NOT NULL DEFAULT 0   -- 1 = 这次是主动刚读的（不是出图时留下的旧缓存），才能拿来算恢复速度
);
CREATE TABLE IF NOT EXISTS status_hourly (  -- 成员接口每小时各 HTTP 状态码的次数（app/status_stats.py）
    hour INTEGER NOT NULL,
    code INTEGER NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour, code)
);
CREATE TABLE IF NOT EXISTS feedback (      -- 成员用 /反馈 提交的问卷（Discord 弹窗表单，不私信任何人）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    discord_id TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    key_id INTEGER,
    answers TEXT NOT NULL                  -- JSON：{问题 id: 回答}
);
CREATE INDEX IF NOT EXISTS idx_feedback_ts ON feedback (ts);
CREATE TABLE IF NOT EXISTS coupons (       -- 成员的券（现在只有「重置券」：在首页自己点一下，重置当天的 V5 / Anlas 额度）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'reset',
    note TEXT NOT NULL DEFAULT '',       -- 发券理由，成员能看到（如「唱得真好听」）
    by TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);
CREATE INDEX IF NOT EXISTS idx_coupons_key ON coupons(key_id, used_at);
CREATE TABLE IF NOT EXISTS req_features (      -- 出图请求的特征（只存哈希），给防分享回测用；长期保留
    ts REAL NOT NULL,
    key_id INTEGER NOT NULL,
    src TEXT NOT NULL DEFAULT '',          -- 来源网络打码标签
    fp TEXT NOT NULL DEFAULT '',           -- 客户端指纹（User-Agent）的哈希
    os TEXT NOT NULL DEFAULT '',
    sig TEXT NOT NULL DEFAULT '',          -- 参数签名哈希（采样器 / 步数 / CFG / 负面词 …）
    toks TEXT NOT NULL DEFAULT '',         -- 提示词词条哈希（空格分隔，最多 80 个），不可还原
    busy INTEGER NOT NULL DEFAULT 0        -- 当时这把 Key 是否还有图在生成
);
CREATE INDEX IF NOT EXISTS idx_req_features ON req_features (key_id, ts);
CREATE TABLE IF NOT EXISTS share_evidence (    -- 防分享证据与处罚记录，后台溯源用
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    key_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    points REAL NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_share_evidence_key ON share_evidence (key_id, ts DESC);
CREATE TABLE IF NOT EXISTS error_events (
    sig TEXT PRIMARY KEY,          -- 错误特征：异常类型 + 出错位置（或来源 + 去掉数字的消息）
    source TEXT NOT NULL,          -- request / upstream / maintenance:xxx / web:landing / disconnect ...
    level TEXT NOT NULL DEFAULT 'error',
    title TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',   -- 最近一次的堆栈 / 上下文（只给站长看）
    count INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    last_rid TEXT NOT NULL DEFAULT '',
    last_key INTEGER,
    last_path TEXT NOT NULL DEFAULT '',
    resolved_at REAL               -- 站长标记已处理；之后再出现视为「复发」
);
CREATE TABLE IF NOT EXISTS waitlist (
    discord_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    joined_at REAL NOT NULL,
    invited_at REAL                -- 有名额时发出邀请的时间；24 小时内未领取则让给下一位
);
CREATE TABLE IF NOT EXISTS key_idle_reminders (
    key_id INTEGER PRIMARY KEY,
    activity REAL NOT NULL,       -- 提醒时 Key 的最后活动时间；之后再有活动会重新计时并允许再次提醒
    sent_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,          -- 后台(打码 IP) / Discord:<id> / 系统
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    ok INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_admin_actions_ts ON admin_actions (ts DESC);
CREATE TABLE IF NOT EXISTS key_sources (
    key_id INTEGER NOT NULL,
    net_hash TEXT NOT NULL,       -- 加盐哈希后的来源网段（IPv4 /24、IPv6 /48），不存完整 IP
    label TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key_id, net_hash)
);
CREATE INDEX IF NOT EXISTS idx_key_sources_seen ON key_sources (last_seen);
CREATE TABLE IF NOT EXISTS upstream_token_settings (
    token_id TEXT PRIMARY KEY,
    v5_daily_limit INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS upstream_token_enabled (
    token_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS upstream_token_image_concurrency (
    token_id TEXT PRIMARY KEY,
    concurrency INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS daily_quota_offsets (
    key_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    anlas REAL NOT NULL DEFAULT 0,
    v5 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key_id, day)
);
CREATE TABLE IF NOT EXISTS deleted_key_usage_flags (
    key_id INTEGER PRIMARY KEY,
    exclude_global_v5 INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS preserve_deleted_key_usage
BEFORE DELETE ON api_keys
BEGIN
    INSERT INTO deleted_key_usage_flags(key_id, exclude_global_v5)
        VALUES (OLD.id, OLD.exclude_global_v5)
        ON CONFLICT(key_id) DO NOTHING;
    DELETE FROM daily_quota_offsets WHERE key_id=OLD.id;
END;
CREATE TRIGGER IF NOT EXISTS prevent_deleted_key_quota_reset
BEFORE INSERT ON daily_quota_offsets
WHEN EXISTS (SELECT 1 FROM deleted_key_usage_flags WHERE key_id=NEW.key_id)
BEGIN
    SELECT RAISE(IGNORE);
END;
"""

# 统计成员用量 / 上游表现时排除测试 Key 的日志。删除测试 Key 前先删它的日志，否则这些日志会重新算进成员统计。
NOT_TEST = "COALESCE(key_id, 0) NOT IN (SELECT id FROM api_keys WHERE is_test=1)"

_INSERT_LOG = """INSERT INTO usage_log (ts, key_id, key_name, kind, model, status,
                                      images, anlas, tokens, detail, unconfirmed_anlas,
                                      wait_ms, dur_ms, client, up_status, rid, src, ver)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
_INSERT_REJECT = """INSERT INTO usage_log (ts, key_id, key_name, kind, model, status,
                                         images, anlas, tokens, detail, unconfirmed_anlas,
                                         wait_ms, dur_ms, client, up_status, rid, src, ver, reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
RULE_VERSION = ""                     # 启动时由 main.py 设为网关版本
_UPSERT_COUNTERS = """INSERT INTO counters
                     (key_id, day, images, anlas, text_tokens, requests, v5, legacy_free_images)
                     VALUES (?,?,?,?,?,?,?,?)
                     ON CONFLICT(key_id, day) DO UPDATE SET
                       images = images + excluded.images,
                       anlas = anlas + excluded.anlas,
                       text_tokens = text_tokens + excluded.text_tokens,
                       requests = requests + excluded.requests,
                       v5 = v5 + excluded.v5,
                       legacy_free_images = legacy_free_images + excluded.legacy_free_images"""


def _rollback_on_error(conn) -> None:
    """共享主连接上任何一条语句出错（最常见：database is locked）就回滚。

    Python sqlite3 写之前会自动 BEGIN；语句失败时这个事务不会自己结束。之后同一连接再读一次就拿到一个旧快照，
    别的连接一提交，这个连接上的每次写都立刻报 locked，直到重启（2026-10-10 19:20 部署快照时实际发生：
    touch_key 等锁超时 → 之后所有走主连接的写全部 500，快照结束后也不恢复）。"""
    import sqlite3
    orig = conn._execute

    async def _execute(fn, *args, **kwargs):
        try:
            return await orig(fn, *args, **kwargs)
        except sqlite3.Error:
            def _rb():
                c = conn._conn
                if c is not None and c.in_transaction:
                    c.rollback()
            try:
                await orig(_rb)
            except Exception:
                pass
            raise
    conn._execute = _execute


class Database:
    def __init__(self, path: str, tz: str = "Asia/Shanghai"):
        self.path = path
        self.tz = tz
        self._db: Optional[aiosqlite.Connection] = None
        self._record_lock = asyncio.Lock()
        # 独立事务也需访问同一个内存库；主连接关闭前保留该库。
        self._connection_path = (f"file:gate-{uuid4().hex}?mode=memory&cache=shared"
                                 if path == ":memory:" else path)

    def _open_connection(self):
        return aiosqlite.connect(self._connection_path, uri=self.path == ":memory:", timeout=15)   # 锁等待 15 秒（默认 5）

    async def connect(self) -> None:
        self._db = await self._open_connection()
        _rollback_on_error(self._db)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        # 轻量迁移：老库补列（新库建表已含该列，会抛 duplicate column，忽略即可）
        for ddl in (
            "ALTER TABLE api_keys ADD COLUMN daily_anlas REAL NOT NULL DEFAULT 0",
            "ALTER TABLE api_keys ADD COLUMN daily_v5 INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE counters ADD COLUMN v5 INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE api_keys ADD COLUMN exclude_global_v5 INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE api_keys ADD COLUMN image_model_scope TEXT NOT NULL DEFAULT 'legacy'",
            "ALTER TABLE api_keys ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE api_keys ADD COLUMN features TEXT",
            "ALTER TABLE discord_registrations ADD COLUMN role_granted INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE discord_registrations ADD COLUMN username TEXT",
            "ALTER TABLE discord_registrations ADD COLUMN display_name TEXT",
            "ALTER TABLE discord_registrations ADD COLUMN avatar TEXT",
            "ALTER TABLE upstream_token_counters ADD COLUMN images INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE usage_log ADD COLUMN wait_ms INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE usage_log ADD COLUMN dur_ms INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE usage_log ADD COLUMN client TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE usage_log ADD COLUMN up_status INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE api_keys ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE api_keys ADD COLUMN anlas_auto INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE usage_log ADD COLUMN rid TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE api_keys ADD COLUMN quota_auto INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE usage_log ADD COLUMN src TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE usage_log ADD COLUMN ver TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE generation_audit ADD COLUMN extra TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE generation_audit ADD COLUMN image BLOB",
            "ALTER TABLE generation_audit ADD COLUMN image_type TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE usage_log ADD COLUMN reason TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE upstream_snapshots ADD COLUMN fresh INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE discord_registrations ADD COLUMN member_checked_at REAL NOT NULL DEFAULT 0",
            # 站长手动定的 V5 每日张数（基础值）；实际 daily_v5 = max(它 × 节约倍数, 算法给普通成员的值)，见 quota_algo
            "ALTER TABLE api_keys ADD COLUMN v5_pinned INTEGER",
            # 按算力折算的张数（节约模式 14 步的图算 0.5 张）；账号每日上限按它判断。老数据回填见下
            "ALTER TABLE upstream_token_counters ADD COLUMN units REAL",
        ):
            try:
                await self._db.execute(ddl)
                await self._db.commit()
            except aiosqlite.OperationalError:
                pass  # 列已存在
        # units 列刚加上时为 NULL：老数据按 1 张 = 1 份回填（偏保守，不会让升级当天突然多出额度）
        await self._db.execute("UPDATE upstream_token_counters SET units=images WHERE units IS NULL")
        await self._db.commit()
        # 原因码上线前的拒绝只有中文 detail：一次性回填最近 2 天（自动驾驶 / AIMD 最长看 24 小时），之后只按 reason 数
        from .reasons import BACKFILL
        cols = {r["name"] for r in await (await self._db.execute("PRAGMA table_info(usage_log)")).fetchall()}
        if {"reason", "detail"} <= cols:          # 很老的库没有 detail 列，也就没有可回填的
            for code, pattern in BACKFILL:
                await self._db.execute("UPDATE usage_log SET reason=? WHERE reason='' AND status='rejected' "
                                       "AND ts>? AND detail LIKE ?", (code, time.time() - 2 * 86400, pattern))
            await self._db.commit()
        log_columns = await (await self._db.execute("PRAGMA table_info(usage_log)")).fetchall()
        if "unconfirmed_anlas" not in {row["name"] for row in log_columns}:
            # 旧日志缺少报价，待核对金额初始为 0。
            await self._db.execute(
                "ALTER TABLE usage_log ADD COLUMN unconfirmed_anlas REAL NOT NULL DEFAULT 0"
            )
            await self._db.commit()
        columns = await (await self._db.execute("PRAGMA table_info(counters)")).fetchall()
        if "legacy_free_images" not in {row["name"] for row in columns}:
            # One-time migration: preserve today's usage rather than granting a fresh
            # 100 images when the service is upgraded in the middle of a day.
            from datetime import datetime
            from zoneinfo import ZoneInfo

            await self._db.execute(
                "ALTER TABLE counters ADD COLUMN legacy_free_images INTEGER NOT NULL DEFAULT 0"
            )
            await self._db.execute(
                "UPDATE api_keys SET daily_images=100 WHERE daily_images=0 AND is_admin=0"
            )
            cur = await self._db.execute(
                """SELECT key_id, ts, images FROM usage_log
                   WHERE kind IN ('image', 'image_stream') AND status='ok'
                     AND anlas=0 AND images>0
                     AND model NOT LIKE 'nai-diffusion-5%'
                     AND model NOT LIKE 'nai-v5%'"""
            )
            counts: dict[tuple[int, str], int] = {}
            timezone = ZoneInfo(self.tz)
            for row in await cur.fetchall():
                day = datetime.fromtimestamp(row["ts"], timezone).strftime("%Y-%m-%d")
                identity = (row["key_id"], day)
                counts[identity] = counts.get(identity, 0) + int(row["images"])
            for (key_id, day), images in counts.items():
                await self._db.execute(
                    "UPDATE counters SET legacy_free_images=? WHERE key_id=? AND day=?",
                    (images, key_id, day),
                )
            await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def reconciliation_totals(self) -> dict:
        # Lifetime totals include deleted Keys and survive daily quota resets.
        row = await (await self._db.execute("""
            SELECT (SELECT COALESCE(SUM(anlas), 0) FROM counters) AS anlas,
                   COALESCE(SUM(unconfirmed_anlas), 0) AS unconfirmed_anlas,
                   COALESCE(SUM(unconfirmed_anlas > 0), 0) AS unconfirmed_requests,
                   COALESCE(MAX(id), 0) AS last_log_id FROM usage_log
        """)).fetchone()
        return dict(row)

    async def save_reconciliation(self, snapshot: dict) -> None:
        # Isolate snapshot commits and rollbacks from concurrent ledger writes.
        async with self._open_connection() as db:
            await db.execute("INSERT INTO anlas_reconciliations(snapshot) VALUES (?)",
                             (json.dumps(snapshot, ensure_ascii=False, allow_nan=False),))
            await db.commit()

    async def reconciliation_history(self, limit: int = 20) -> list[dict]:
        rows = await (await self._db.execute(
            "SELECT id, snapshot FROM anlas_reconciliations ORDER BY id DESC LIMIT ?",
            (min(20, max(1, limit)),))).fetchall()
        return [{**json.loads(row["snapshot"]), "id": row["id"]} for row in rows]

    # ---------- upstream token counters ----------
    async def get_upstream_counter(self, token_id: str, day: str) -> dict[str, int]:
        cur = await self._db.execute(
            "SELECT images, v5, units FROM upstream_token_counters WHERE token_id=? AND day=?",
            (token_id, day),
        )
        row = await cur.fetchone()
        if not row:
            return {"images": 0, "v5": 0, "units": 0.0}
        units = row["units"]
        return {"images": int(row["images"]), "v5": int(row["v5"]),
                "units": float(row["images"] if units is None else units)}

    async def migrate_upstream_token_ids(self, token_ids: list[str]) -> None:
        """Merge old position-based counters into stable hashed token identities."""
        for token_id in set(token_ids):
            suffix = token_id.removeprefix("token-")
            rows = await (await self._db.execute(
                "SELECT token_id, day, images, v5 FROM upstream_token_counters WHERE token_id LIKE ?",
                (f"token-%-{suffix}",),
            )).fetchall()
            for row in rows:
                await self._db.execute(
                    """INSERT INTO upstream_token_counters(token_id, day, images, v5)
                       VALUES(?,?,?,?) ON CONFLICT(token_id,day) DO UPDATE SET
                       images=images+excluded.images, v5=v5+excluded.v5""",
                    (token_id, row["day"], row["images"], row["v5"]),
                )
                await self._db.execute(
                    "DELETE FROM upstream_token_counters WHERE token_id=? AND day=?",
                    (row["token_id"], row["day"]),
                )
        await self._db.commit()

    async def move_upstream_token(self, old_id: str, new_id: str) -> None:
        """令牌被替换（同一账号重置了 Token）：把设置和计数从旧标识迁移到新标识。"""
        if old_id == new_id:
            return
        for table in ("upstream_token_settings", "upstream_token_enabled", "upstream_token_image_concurrency",
                      "upstream_token_counters"):
            await self._db.execute(f"DELETE FROM {table} WHERE token_id=?", (new_id,))
            await self._db.execute(f"UPDATE {table} SET token_id=? WHERE token_id=?", (new_id, old_id))
        await self._db.commit()

    async def get_upstream_token_limits(self) -> dict[str, int]:
        rows = await (await self._db.execute(
            "SELECT token_id, v5_daily_limit FROM upstream_token_settings"
        )).fetchall()
        return {row["token_id"]: int(row["v5_daily_limit"]) for row in rows}

    async def set_upstream_token_limit(self, token_id: str, limit: int) -> None:
        await self._db.execute(
            """INSERT INTO upstream_token_settings(token_id, v5_daily_limit) VALUES(?,?)
               ON CONFLICT(token_id) DO UPDATE SET v5_daily_limit=excluded.v5_daily_limit""",
            (token_id, limit),
        )
        await self._db.commit()

    async def get_upstream_token_enabled(self) -> dict[str, bool]:
        rows = await (await self._db.execute(
            "SELECT token_id, enabled FROM upstream_token_enabled"
        )).fetchall()
        return {row["token_id"]: bool(row["enabled"]) for row in rows}

    async def set_upstream_token_enabled(self, token_id: str, enabled: bool) -> None:
        await self._db.execute(
            """INSERT INTO upstream_token_enabled(token_id, enabled) VALUES(?,?)
               ON CONFLICT(token_id) DO UPDATE SET enabled=excluded.enabled""",
            (token_id, int(enabled)),
        )
        await self._db.commit()

    async def get_upstream_token_image_concurrency(self) -> dict[str, int]:
        rows = await (await self._db.execute(
            "SELECT token_id, concurrency FROM upstream_token_image_concurrency"
        )).fetchall()
        return {row["token_id"]: int(row["concurrency"]) for row in rows}

    async def set_upstream_token_image_concurrency(self, token_id: str, limit: int) -> None:
        await self._db.execute(
            """INSERT INTO upstream_token_image_concurrency(token_id, concurrency) VALUES(?,?)
               ON CONFLICT(token_id) DO UPDATE SET concurrency=excluded.concurrency""",
            (token_id, limit),
        )
        await self._db.commit()

    async def get_upstream_v5_counter(self, token_id: str, day: str) -> int:
        return (await self.get_upstream_counter(token_id, day))["v5"]

    async def bump_upstream_image_counter(self, token_id: str, day: str,
                                           images: int, weight: float = 1.0) -> None:
        if images < 1:
            return
        units = images * max(0.0, float(weight))
        await self._db.execute(
            """INSERT INTO upstream_token_counters(token_id, day, images, units) VALUES (?,?,?,?)
               ON CONFLICT(token_id, day) DO UPDATE SET images=images+excluded.images,
               units=COALESCE(units, images)+excluded.units""",
            (token_id, day, images, units),
        )
        await self._db.commit()

    async def bump_upstream_v5_counter(self, token_id: str, day: str) -> None:
        await self._db.execute(
            """INSERT INTO upstream_token_counters(token_id, day, v5) VALUES (?,?,1)
               ON CONFLICT(token_id, day) DO UPDATE SET v5=v5+1""",
            (token_id, day),
        )
        await self._db.commit()

    # ---------- keys ----------
    async def create_key(self, fields: dict[str, Any]) -> aiosqlite.Row:
        now = time.time()
        cur = await self._db.execute(
            """INSERT INTO api_keys
               (name, token, enabled, daily_images, daily_anlas, daily_v5, monthly_anlas,
               daily_text_tokens, rpm, allow_anlas, allow_img2img, exclude_global_v5, image_model_scope, is_admin, expires_at, created_at, features,
               is_test)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fields["name"],
                fields["token"],
                1 if fields.get("enabled", True) else 0,
                int(fields["daily_images"]),
                float(fields.get("daily_anlas", 0)),
                int(fields.get("daily_v5", 0)),
                float(fields["monthly_anlas"]),
                int(fields["daily_text_tokens"]),
                int(fields["rpm"]),
                1 if fields.get("allow_anlas") else 0,
                1 if fields.get("allow_img2img") else 0,
                1 if fields.get("exclude_global_v5") else 0,
                fields.get("image_model_scope", "legacy"),
                1 if fields.get("is_admin") else 0,
                fields.get("expires_at"),
                now,
                fields.get("features"),
                1 if fields.get("is_test") else 0,
            ),
        )
        await self._db.commit()
        return await self.get_key(cur.lastrowid)

    async def get_key(self, key_id: int) -> Optional[aiosqlite.Row]:
        cur = await self._db.execute("SELECT * FROM api_keys WHERE id=?", (key_id,))
        return await cur.fetchone()

    async def get_key_by_token(self, token: str) -> Optional[aiosqlite.Row]:
        cur = await self._db.execute("SELECT * FROM api_keys WHERE token=?", (token,))
        return await cur.fetchone()

    async def rotate_key_token(self, key_id: int, token: str) -> bool:
        # Keep the ID so admitted work, usage and rate limits retain ownership.
        cur = await self._db.execute(
            "UPDATE api_keys SET token=? WHERE id=?", (token, key_id)
        )
        await self._db.commit()
        return cur.rowcount == 1

    async def update_key(self, key_id: int, fields: dict[str, Any]) -> None:
        allowed = {
            "name", "enabled", "daily_images", "daily_anlas", "daily_v5", "monthly_anlas",
            "daily_text_tokens", "rpm", "allow_anlas", "allow_img2img", "exclude_global_v5", "image_model_scope", "expires_at", "features",
            "is_test", "v5_pinned",
        }
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append(f"{k}=?")
            if k in ("enabled", "allow_anlas", "allow_img2img", "exclude_global_v5", "is_test"):
                v = 1 if v else 0
            vals.append(v)
        if not sets:
            return
        vals.append(key_id)
        await self._db.execute(f"UPDATE api_keys SET {', '.join(sets)} WHERE id=?", vals)
        await self._db.commit()

    async def delete_key(self, key_id: int) -> None:
        # The trigger archives the V5 flag and removes offsets atomically.
        # Counters/logs also accept late settlement from already admitted work.
        await self._db.execute("DELETE FROM key_sources WHERE key_id=?", (key_id,))
        await self._db.execute("DELETE FROM key_idle_reminders WHERE key_id=?", (key_id,))
        await self._db.execute("DELETE FROM key_tags WHERE key_id=?", (key_id,))
        await self._db.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
        await self._db.commit()

    async def inactive_key_ids(self, cutoff: float) -> list[int]:
        """Return inactive keys without falsifying their last-used timestamps."""
        raw_grace = await self.get_setting("key_inactivity_grace_started_at", 0)
        try:
            grace_started_at = float(raw_grace)
        except (TypeError, ValueError):
            return []  # Malformed reset marker must never trigger mass deletion.
        if not math.isfinite(grace_started_at) or grace_started_at < 0:
            return []
        cur = await self._db.execute(
            """SELECT id FROM api_keys
               WHERE is_admin=0 AND is_test=0 AND enabled=1 AND MAX(COALESCE(last_used_at, created_at), ?) < ?
               ORDER BY id ASC""",
            (grace_started_at, cutoff),
        )
        return [int(row["id"]) for row in await cur.fetchall()]

    async def keys_due_for_idle_reminder(self, remind_before: float) -> list[dict[str, Any]]:
        """闲置回收前的提醒对象：Discord 自助领取、仍启用、最后活动早于 remind_before、本轮尚未提醒。"""
        raw_grace = await self.get_setting("key_inactivity_grace_started_at", 0)
        try:
            grace = float(raw_grace)
        except (TypeError, ValueError):
            return []
        if not math.isfinite(grace) or grace < 0:
            return []
        cur = await self._db.execute(
            """SELECT k.id AS key_id, k.name AS name, r.discord_id AS discord_id,
                      MAX(COALESCE(k.last_used_at, k.created_at), ?) AS activity,
                      EXISTS (SELECT 1 FROM usage_log u WHERE u.key_id=k.id AND u.status='ok') AS ever_used
               FROM api_keys k JOIN discord_registrations r ON r.key_id=k.id
               LEFT JOIN key_idle_reminders m ON m.key_id=k.id
               WHERE k.is_admin=0 AND k.enabled=1
                 AND MAX(COALESCE(k.last_used_at, k.created_at), ?) < ?
                 AND (m.key_id IS NULL OR m.activity <> MAX(COALESCE(k.last_used_at, k.created_at), ?))""",
            (grace, grace, remind_before, grace))
        return [dict(r) for r in await cur.fetchall()]

    async def mark_idle_reminded(self, key_id: int, activity: float) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO key_idle_reminders (key_id, activity, sent_at) VALUES (?,?,?)",
            (key_id, activity, time.time()))
        await self._db.commit()

    async def list_keys(self) -> list[aiosqlite.Row]:
        cur = await self._db.execute("SELECT * FROM api_keys ORDER BY id DESC")
        return list(await cur.fetchall())

    async def touch_key(self, key_id: int) -> None:
        await self._db.execute(
            "UPDATE api_keys SET last_used_at=? WHERE id=?", (time.time(), key_id)
        )
        await self._db.commit()

    # ---------- counters ----------
    async def bump_counters(
        self, key_id: int, day: str,
        images: int = 0, anlas: float = 0.0, text_tokens: int = 0, requests: int = 1,
        v5: int = 0, legacy_free_images: int = 0,
    ) -> None:
        await self._db.execute(
            _UPSERT_COUNTERS,
            (key_id, day, images, anlas, text_tokens, requests, v5, legacy_free_images),
        )
        await self._db.commit()

    async def get_counter(self, key_id: int, day: str) -> dict[str, Any]:
        cur = await self._db.execute(
            """SELECT c.*, COALESCE(o.anlas, 0) AS _quota_offset_anlas,
                      COALESCE(o.v5, 0) AS _quota_offset_v5
               FROM counters AS c
               LEFT JOIN daily_quota_offsets AS o ON o.key_id=c.key_id AND o.day=c.day
               WHERE c.key_id=? AND c.day=?""", (key_id, day)
        )
        row = await cur.fetchone()
        if row:
            result = dict(row)
            result["anlas"] = max(0, result["anlas"] - result.pop("_quota_offset_anlas"))
            result["v5"] = max(0, result["v5"] - result.pop("_quota_offset_v5"))
            return result
        return {"images": 0, "legacy_free_images": 0, "anlas": 0.0, "text_tokens": 0, "requests": 0, "v5": 0}

    # ---------- 券 ----------
    async def add_coupon(self, key_id: int, kind: str, days: float, note: str = "", by: str = "") -> dict[str, Any]:
        now = time.time()
        cur = await self._db.execute(
            "INSERT INTO coupons(key_id, kind, note, by, created_at, expires_at) VALUES (?,?,?,?,?,?)",
            (key_id, kind, note[:200], by[:40], now, now + days * 86400))
        await self._db.commit()
        return {"id": cur.lastrowid, "key_id": key_id, "kind": kind, "note": note[:200], "created_at": now,
                "expires_at": now + days * 86400, "used_at": None}

    async def list_coupons(self, key_id: int, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT id, kind, note, by, created_at, expires_at, used_at FROM coupons WHERE key_id=?"
        args: tuple = (key_id,)
        if active_only:
            sql += " AND used_at IS NULL AND expires_at>?"
            args = (key_id, time.time())
        return [dict(r) for r in await self._db.execute_fetchall(sql + " ORDER BY expires_at", args)]

    async def use_coupon(self, coupon_id: int, key_id: int) -> Optional[dict[str, Any]]:
        """标记用掉（只能用一次、要没过期、要是自己的）。成功返回这张券，否则 None。"""
        now = time.time()
        cur = await self._db.execute(
            "UPDATE coupons SET used_at=? WHERE id=? AND key_id=? AND used_at IS NULL AND expires_at>?",
            (now, coupon_id, key_id, now))
        await self._db.commit()
        if cur.rowcount != 1:
            return None
        rows = await self._db.execute_fetchall("SELECT id, kind, note, expires_at, used_at FROM coupons WHERE id=?", (coupon_id,))
        return dict(rows[0]) if rows else None

    async def reset_daily_image_quota(self, key_id: int, day: str) -> None:
        """重置单个 Key 当日的 V5 与 Anlas 可用额度基线。

        保留原始记账用量，以重置时的累计量更新基线。
        """
        await self._db.execute(
            """INSERT INTO daily_quota_offsets (key_id, day, anlas, v5)
               SELECT key_id, day, anlas, v5 FROM counters WHERE key_id=? AND day=?
               ON CONFLICT(key_id, day) DO UPDATE SET anlas=excluded.anlas, v5=excluded.v5""",
            (key_id, day),
        )
        await self._db.commit()

    async def month_anlas(self, key_id: int, month: str) -> float:
        cur = await self._db.execute(
            "SELECT COALESCE(SUM(anlas),0) AS a FROM counters WHERE key_id=? AND substr(day,1,7)=?",
            (key_id, month),
        )
        row = await cur.fetchone()
        return float(row["a"] or 0)

    async def day_v5_total(self, day: str) -> int:
        """全站当日 V5 消耗，不包含明确配置为独立额度的 Key。"""
        cur = await self._db.execute(
            """SELECT COALESCE(SUM(c.v5),0) AS c
               FROM counters AS c
               LEFT JOIN api_keys AS k ON k.id = c.key_id
               LEFT JOIN deleted_key_usage_flags AS d ON d.key_id = c.key_id
               WHERE c.day=? AND COALESCE(k.exclude_global_v5, d.exclude_global_v5, 1)=0""",
            (day,),
        )
        row = await cur.fetchone()
        return int(row["c"] or 0)

    async def month_anlas_all(self, month: str) -> float:
        """全站所有 Key 本月的 Anlas 消耗（用于全站预算总闸）。"""
        cur = await self._db.execute(
            "SELECT COALESCE(SUM(anlas),0) AS a FROM counters WHERE substr(day,1,7)=?",
            (month,),
        )
        row = await cur.fetchone()
        return float(row["a"] or 0)

    # ---------- site settings ----------
    async def get_setting(self, key: str, default: Any = None) -> Any:
        cur = await self._db.execute(
            "SELECT value FROM site_settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else default

    async def set_setting(self, key: str, value: Any) -> None:
        await self._db.execute(
            """INSERT INTO site_settings (key, value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, str(value)),
        )
        await self._db.commit()

    async def set_settings_bulk(self, values: dict[str, Any]) -> None:
        """Persist a validated group of runtime controls in one transaction."""
        await self._db.executemany(
            """INSERT INTO site_settings (key, value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            [(key, str(value)) for key, value in values.items()],
        )
        await self._db.commit()

    # ---------- logs ----------
    async def record_success(
        self, key_id: int, key_name: str, kind: str, model: str, day: str, *,
        images: int = 0, anlas: float = 0.0, tokens: int = 0, v5: int = 0,
        legacy_free_images: int = 0, detail: str = "", unconfirmed_anlas: float = 0.0,
        wait_ms: int = 0, dur_ms: int = 0, client: str = "", up_status: int = 0, rid: str = "", src: str = "",
    ) -> None:
        """成功日志、额度与使用时间一起提交；写入失败时整笔回退。
        数据库被别的写入（清理原图、存大图）短暂锁住时重试几次：这时图已经出了、上游可能已经扣了，
        记账失败会让成员拿不到图、计数也丢（2026-10-10 审查 F5）。"""
        args = (key_id, key_name, kind, model, day, images, anlas, tokens, v5, legacy_free_images, detail,
                unconfirmed_anlas, wait_ms, dur_ms, client, up_status, rid, src)
        for attempt in range(4):
            try:
                return await self._record_success_once(*args)
            except aiosqlite.OperationalError as exc:
                if attempt == 3 or not any(w in str(exc).lower() for w in ("locked", "busy")):
                    raise
                await asyncio.sleep(0.5 * 2 ** attempt)

    async def _record_success_once(self, key_id, key_name, kind, model, day, images, anlas, tokens, v5,
                                   legacy_free_images, detail, unconfirmed_anlas, wait_ms, dur_ms, client,
                                   up_status, rid, src) -> None:
        # 不使用共享连接，避免其他请求的 commit 提前保存半笔记账。
        async with self._record_lock, self._open_connection() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                now = time.time()
                await db.execute(_INSERT_LOG, (
                    now, key_id, key_name, kind, model, "ok", images, anlas,
                    tokens, detail[:500], unconfirmed_anlas, max(0, int(wait_ms)),
                    max(0, int(dur_ms)), client[:256], int(up_status), rid[:16], src[:40], RULE_VERSION,
                ))
                await db.execute(_UPSERT_COUNTERS, (
                    key_id, day, images, anlas, tokens, 1, v5, legacy_free_images,
                ))
                await db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (now, key_id))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def forget_registration_for_key(self, key_id: int) -> list[str]:
        """删除该 Key 对应的领取记录，返回被删记录的 Discord ID（用于摘除身份组）。"""
        rows = await self._db.execute_fetchall(
            "SELECT discord_id FROM discord_registrations WHERE key_id=?", (key_id,))
        await self._db.execute("DELETE FROM discord_registrations WHERE key_id=?", (key_id,))
        await self._db.commit()
        return [str(r[0]) for r in rows]

    async def execute_fetchall_compat(self, sql: str, args: tuple = ()) -> list:
        return list(await (await self._db.execute(sql, args)).fetchall())

    async def add_audit(self, key_id, key_name: str, kind: str, model: str, status: str,
                        prompt: str, negative: str, thumb: Optional[bytes], extra: str = "",
                        image: Optional[bytes] = None, image_type: str = "") -> None:
        await self._db.execute(
            """INSERT INTO generation_audit (ts,key_id,key_name,kind,model,status,prompt,negative,extra,thumb,image,image_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), key_id, key_name[:80], kind, model[:80], status, prompt, negative, extra, thumb, image, image_type))
        await self._db.commit()

    async def list_audit(self, limit: int, offset: int, key_id: Optional[int] = None) -> tuple[list, int]:
        where, args = ("WHERE key_id=?", (key_id,)) if key_id is not None else ("", ())
        total = (await (await self._db.execute(f"SELECT COUNT(*) FROM generation_audit {where}", args)).fetchone())[0]
        rows = await (await self._db.execute(
            f"""SELECT id,ts,key_id,key_name,kind,model,status,prompt,negative,extra,
                       (thumb IS NOT NULL) AS has_thumb, (image IS NOT NULL) AS has_image
                FROM generation_audit {where} ORDER BY id DESC LIMIT ? OFFSET ?""",
            (*args, limit, offset))).fetchall()
        return rows, int(total)

    async def audit_thumb(self, audit_id: int) -> Optional[bytes]:
        row = await (await self._db.execute("SELECT thumb FROM generation_audit WHERE id=?", (audit_id,))).fetchone()
        return bytes(row["thumb"]) if row and row["thumb"] is not None else None

    async def audit_image(self, audit_id: int) -> tuple[Optional[bytes], str]:
        row = await (await self._db.execute(
            "SELECT image, image_type FROM generation_audit WHERE id=?", (audit_id,))).fetchone()
        if row and row["image"] is not None:
            return bytes(row["image"]), (row["image_type"] or "image/png")
        return None, ""

    async def audit_images_for(self, key_id: int, max_bytes: int = 0):
        """导出用：某成员带原图的记录，产出 (id, ts, image_type, prompt, negative, extra, image)。
        max_bytes>0 时只取最新的、原图合计不超过这么多字节的那些（防止一次打包把内存撑爆）。"""
        if max_bytes > 0:
            sizes = await self._db.execute_fetchall(
                "SELECT id, length(image) FROM generation_audit WHERE key_id=? AND image IS NOT NULL ORDER BY ts DESC",
                (key_id,))
            keep, total = [], 0
            for rid, size in sizes:
                if total + int(size or 0) > max_bytes and keep:
                    break
                keep.append(int(rid))
                total += int(size or 0)
            if not keep:
                return []
            marks = ",".join("?" * len(keep))
            return await self._db.execute_fetchall(
                "SELECT id, ts, image_type, prompt, negative, extra, image FROM generation_audit "
                f"WHERE id IN ({marks}) ORDER BY ts", tuple(keep))
        rows = await self._db.execute_fetchall(
            "SELECT id, ts, image_type, prompt, negative, extra, image FROM generation_audit "
            "WHERE key_id=? AND image IS NOT NULL ORDER BY ts", (key_id,))
        return rows

    async def audit_image_count(self, key_id: int) -> int:
        r = await self._db.execute_fetchall(
            "SELECT COUNT(*) FROM generation_audit WHERE key_id=? AND image IS NOT NULL", (key_id,))
        return int(r[0][0])

    async def purge_audit_images(self, older_than: float) -> int:
        """只清原图 BLOB（置空），保留缩略图、提示词、参数。"""
        cur = await self._db.execute(
            "UPDATE generation_audit SET image=NULL, image_type='' WHERE image IS NOT NULL AND ts<?", (older_than,))
        await self._db.commit()
        return cur.rowcount or 0

    async def touch_key_source(self, key_id: int, net_hash: str, label: str,
                               now: float, window_start: float) -> bool:
        """记录来源网段；返回该网段在窗口内是否为新出现（用于决定是否检查分享告警）。"""
        cur = await self._db.execute(
            "SELECT last_seen FROM key_sources WHERE key_id=? AND net_hash=?", (key_id, net_hash))
        row = await cur.fetchone()
        await self._db.execute(
            """INSERT INTO key_sources (key_id, net_hash, label, first_seen, last_seen, hits)
               VALUES (?,?,?,?,?,1)
               ON CONFLICT(key_id, net_hash) DO UPDATE SET last_seen=excluded.last_seen,
                   label=excluded.label, hits=hits+1""",
            (key_id, net_hash, label, now, now))
        await self._db.commit()
        return row is None or row[0] < window_start

    async def key_source_labels(self, key_id: int, since: float) -> list[str]:
        # 按打码标签（IPv4 /16、IPv6 /32）去重：WARP、手机运营商等同一网络内频繁换 /24 的不算多个来源
        cur = await self._db.execute(
            """SELECT label FROM key_sources WHERE key_id=? AND last_seen>=?
               GROUP BY label ORDER BY MAX(last_seen) DESC""", (key_id, since))
        return [r[0] for r in await cur.fetchall()]

    async def key_source_summary(self, since: float) -> dict[int, list[str]]:
        """每把 Key 在 since 之后出现过的来源网段标签（最近的在前）。"""
        cur = await self._db.execute(
            """SELECT key_id, label FROM key_sources WHERE last_seen>=?
               GROUP BY key_id, label ORDER BY MAX(last_seen) DESC""", (since,))
        out: dict[int, list[str]] = {}
        for key_id, label in await cur.fetchall():
            out.setdefault(int(key_id), []).append(label)
        return out

    async def add_admin_action(self, actor: str, action: str, target: str, detail: str, ok: bool) -> None:
        await self._db.execute(
            "INSERT INTO admin_actions (ts, actor, action, target, detail, ok) VALUES (?,?,?,?,?,?)",
            (time.time(), actor, action, target, detail, 1 if ok else 0))
        await self._db.commit()

    async def list_admin_actions(self, limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
        cur = await self._db.execute(
            "SELECT * FROM admin_actions ORDER BY id DESC LIMIT ? OFFSET ?",
            (max(1, min(int(limit), 200)), max(0, int(offset))))
        return [dict(r) for r in await cur.fetchall()]

    async def count_admin_actions(self) -> int:
        row = await (await self._db.execute("SELECT COUNT(*) FROM admin_actions")).fetchone()
        return int(row[0])

    async def purge_admin_actions(self, older_than: float) -> int:
        cur = await self._db.execute("DELETE FROM admin_actions WHERE ts<?", (older_than,))
        await self._db.commit()
        return cur.rowcount or 0

    async def purge_key_sources(self, older_than: float) -> int:
        cur = await self._db.execute("DELETE FROM key_sources WHERE last_seen<?", (older_than,))
        await self._db.commit()
        return cur.rowcount or 0

    async def purge_usage_log(self, older_than: float) -> int:
        """只清理明细日志；每日计数器和账本不受影响。"""
        cur = await self._db.execute("DELETE FROM usage_log WHERE ts<?", (older_than,))
        await self._db.commit()
        return cur.rowcount

    async def purge_audit(self, older_than: float) -> int:
        cur = await self._db.execute("DELETE FROM generation_audit WHERE ts<?", (older_than,))
        await self._db.commit()
        return cur.rowcount

    async def member_usage(self, since_day: str) -> dict[int, dict]:
        """每个 Key 近一段时间的用量汇总（来自每日计数器）。"""
        rows = await (await self._db.execute(
            """SELECT key_id, COALESCE(SUM(images),0) AS images, COALESCE(SUM(v5),0) AS v5,
                      COALESCE(SUM(anlas),0) AS anlas, COALESCE(SUM(text_tokens),0) AS text_tokens,
                      COALESCE(SUM(requests),0) AS requests
               FROM counters WHERE day>=? GROUP BY key_id""", (since_day,))).fetchall()
        return {int(r["key_id"]): dict(r) for r in rows}

    async def add_log(
        self, key_id: Optional[int], key_name: str, kind: str, model: str,
        status: str, images: int = 0, anlas: float = 0.0, tokens: int = 0,
        detail: str = "", unconfirmed_anlas: float = 0.0,
        wait_ms: int = 0, dur_ms: int = 0, client: str = "", up_status: int = 0, rid: str = "", src: str = "",
        reason: str = "",
    ) -> None:
        await self._db.execute(
            _INSERT_REJECT,
            (time.time(), key_id, key_name[:80], kind, model[:80], status,
             images, anlas, tokens, detail[:500], unconfirmed_anlas,
             max(0, int(wait_ms)), max(0, int(dur_ms)), client[:256], int(up_status), rid[:16], src[:40], RULE_VERSION,
             reason[:24]),
        )
        await self._db.commit()

    @staticmethod
    def _log_filter(key_id: Optional[int], kinds: Optional[list[str]],
                    hide_test: bool = False, rid: str = "") -> tuple[str, tuple]:
        clauses, args = [], []
        if rid:
            clauses.append("rid=?")
            args.append(rid)
        if hide_test:
            clauses.append(NOT_TEST)
        if key_id:
            clauses.append("key_id=?")
            args.append(key_id)
        if kinds is not None:
            clauses.append("kind IN (%s)" % ",".join("?" * len(kinds)) if kinds else "0")
            args.extend(kinds)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", tuple(args)

    async def list_logs(self, limit: int = 20, offset: int = 0,
                        key_id: Optional[int] = None,
                        kinds: Optional[list[str]] = None, hide_test: bool = False,
                        rid: str = "") -> list[aiosqlite.Row]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        where, args = self._log_filter(key_id, kinds, hide_test, rid)
        cur = await self._db.execute(
            f"SELECT * FROM usage_log{where} ORDER BY id DESC LIMIT ? OFFSET ?", args + (limit, offset))
        return list(await cur.fetchall())

    async def count_logs(self, key_id: Optional[int] = None,
                         kinds: Optional[list[str]] = None, hide_test: bool = False,
                         rid: str = "") -> int:
        where, args = self._log_filter(key_id, kinds, hide_test, rid)
        cur = await self._db.execute(f"SELECT COUNT(*) AS c FROM usage_log{where}", args)
        row = await cur.fetchone()
        return int(row["c"])

    async def member_milestones(self, since: float) -> dict[int, dict[str, Any]]:
        """每把 Key：首次 / 最近一次成功出图时间，以及 since 之后被拒次数（用于成员筛选）。
        基于用量日志，超过日志保留期的早期记录不计入。"""
        out: dict[int, dict[str, Any]] = {}
        cur = await self._db.execute(
            """SELECT key_id, MIN(ts), MAX(ts) FROM usage_log
               WHERE status='ok' AND kind IN ('image','image_stream') AND key_id IS NOT NULL GROUP BY key_id""")
        for key_id, first, last in await cur.fetchall():
            out[int(key_id)] = {"first_image_at": first, "last_image_at": last, "rejected_24h": 0}
        cur = await self._db.execute(
            """SELECT key_id, COUNT(*) FROM usage_log WHERE status='rejected' AND ts>=? AND key_id IS NOT NULL
               GROUP BY key_id""", (since,))
        for key_id, n in await cur.fetchall():
            out.setdefault(int(key_id), {"first_image_at": None, "last_image_at": None})["rejected_24h"] = int(n)
        # 被拒原因（每把 Key 前 3 个）：后台成员详情的「一句话结论」用
        cur = await self._db.execute(
            """SELECT key_id, reason, MIN(detail), COUNT(*) FROM usage_log
               WHERE status='rejected' AND ts>=? AND key_id IS NOT NULL
               GROUP BY key_id, CASE WHEN reason!='' THEN reason ELSE substr(detail, 1, 24) END""", (since,))
        why: dict[int, dict[str, int]] = {}
        for key_id, code, detail, n in await cur.fetchall():      # 同一句话（只差数字）合并
            label = reason_label(code, detail)
            why.setdefault(int(key_id), {})[label] = why.get(int(key_id), {}).get(label, 0) + int(n)
        for key_id, items in why.items():
            out.setdefault(key_id, {"first_image_at": None, "last_image_at": None, "rejected_24h": 0})["rejected_why"] = \
                sorted(([k, v] for k, v in items.items()), key=lambda x: -x[1])[:3]
        return out

    async def latest_member_clients(self) -> dict[int, str]:
        """每把 Key 最近一次请求自报的客户端（User-Agent 摘要）。"""
        cur = await self._db.execute(
            """SELECT u.key_id, u.client FROM usage_log u
               JOIN (SELECT key_id, MAX(id) AS max_id FROM usage_log
                     WHERE key_id IS NOT NULL AND client != ''
                     GROUP BY key_id) m
               ON u.id = m.max_id"""
        )
        return {int(row[0]): str(row[1]) for row in await cur.fetchall()}

    async def image_perf_rows(self, since: float) -> list[tuple]:
        """出图请求的 (ts, model, status, images, wait_ms, dur_ms, up_status, detail)，供上游表现分析。"""
        cur = await self._db.execute(
            """SELECT ts, model, status, images, wait_ms, dur_ms, up_status, detail FROM usage_log
               WHERE ts>=? AND kind IN ('image','image_stream') AND status IN ('ok','error')
                 AND """ + NOT_TEST + """
               ORDER BY ts""", (since,))
        return [tuple(r) for r in await cur.fetchall()]

    async def shadow_rows(self, since: float) -> list[tuple]:
        """调度影子模式回放用：出图请求的 (ts, key_id, key_name, status, images, wait_ms, dur_ms)，不含测试 Key。"""
        cur = await self._db.execute(
            """SELECT ts, key_id, key_name, status, images, wait_ms, dur_ms FROM usage_log
               WHERE ts>=? AND kind IN ('image','image_stream') AND status IN ('ok','error')
                 AND """ + NOT_TEST + """
               ORDER BY ts""", (since,))
        return [tuple(r) for r in await cur.fetchall()]

    async def image_stability(self, since: float) -> dict[str, int]:
        """since 之后出图请求的成功 / 上游失败次数（不含参数错误等被拒请求），用于首页稳定性图标。"""
        cur = await self._db.execute(
            """SELECT status, COUNT(*) FROM usage_log
               WHERE ts>=? AND kind IN ('image','image_stream') AND status IN ('ok','error')
                 AND """ + NOT_TEST + """
               GROUP BY status""", (since,))
        out = {"ok": 0, "error": 0}
        for status, n in await cur.fetchall():
            out[status] = int(n)
        return out

    async def usage_by_kind(self, since: float) -> list[dict[str, Any]]:
        """按 kind / status 汇总 since 之后的用量日志（日志保留期内有效）。"""
        cur = await self._db.execute(
            """SELECT kind, status, COUNT(*) AS n, COALESCE(SUM(images),0) AS images,
                      COALESCE(SUM(tokens),0) AS tokens, COALESCE(SUM(anlas),0) AS anlas,
                      COUNT(DISTINCT key_id) AS users
               FROM usage_log WHERE ts>=? AND """ + NOT_TEST + """ GROUP BY kind, status""", (since,))
        return [dict(r) for r in await cur.fetchall()]

    async def generated_image_totals(self, key_id: Optional[int] = None) -> dict[int, int]:
        """Count successful generations (including completed stream images) per Key."""
        sql = """SELECT key_id, COALESCE(SUM(images), 0) AS images
                 FROM usage_log
                 WHERE key_id IS NOT NULL AND kind IN ('image', 'image_stream')
                   AND status='ok'"""
        args: tuple = ()
        if key_id is not None:
            sql += " AND key_id=?"
            args = (key_id,)
        sql += " GROUP BY key_id"
        rows = await (await self._db.execute(sql, args)).fetchall()
        return {int(row["key_id"]): int(row["images"]) for row in rows}

    # ---------- overview ----------
    async def overview(self, today: str, week_days: list[str]) -> dict[str, Any]:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        async def one(sql: str, args: tuple = ()) -> Any:
            cur = await self._db.execute(sql, args)
            row = await cur.fetchone()
            return row[0] if row else 0

        today_images = await one(
            "SELECT COALESCE(SUM(images),0) FROM counters WHERE day=?", (today,)
        )
        today_anlas = await one(
            "SELECT COALESCE(SUM(anlas),0) FROM counters WHERE day=?", (today,)
        )
        today_tokens = await one(
            "SELECT COALESCE(SUM(text_tokens),0) FROM counters WHERE day=?", (today,)
        )
        today_requests = await one(
            "SELECT COALESCE(SUM(requests),0) FROM counters WHERE day=?", (today,)
        )
        today_v5 = await one(
            """SELECT COALESCE(SUM(c.v5),0)
               FROM counters AS c
               LEFT JOIN api_keys AS k ON k.id = c.key_id
               LEFT JOIN deleted_key_usage_flags AS d ON d.key_id = c.key_id
               WHERE c.day=? AND COALESCE(k.exclude_global_v5, d.exclude_global_v5, 1)=0""",
            (today,),
        )
        keys_total = await one("SELECT COUNT(*) FROM api_keys")
        keys_active = await one(
            "SELECT COUNT(*) FROM api_keys WHERE enabled=1 AND (expires_at IS NULL OR expires_at>?)",
            (time.time(),),
        )
        # 日志按时间戳保存；统计边界必须与配额使用同一时区。
        day_start = datetime.fromisoformat(today).replace(tzinfo=ZoneInfo(self.tz))
        anomaly = {}
        for period, start in (("today", day_start), ("month", day_start.replace(day=1))):
            row = await (await self._db.execute(
                """SELECT COUNT(*), COALESCE(SUM(unconfirmed_anlas),0) FROM usage_log
                   WHERE unconfirmed_anlas>0 AND ts>=? AND ts<?""",
                (start.timestamp(), (day_start + timedelta(days=1)).timestamp()),
            )).fetchone()
            anomaly[period] = {"unconfirmed_requests": int(row[0]),
                               "unconfirmed_anlas": round(float(row[1]), 2)}
        ph = ",".join("?" * len(week_days))
        cur = await self._db.execute(
            f"""SELECT day,
                       SUM(images) AS images, SUM(anlas) AS anlas,
                       SUM(text_tokens) AS text_tokens, SUM(requests) AS requests, SUM(v5) AS v5
                FROM counters WHERE day IN ({ph}) GROUP BY day""",
            tuple(week_days),
        )
        by_day = {r["day"]: dict(r) for r in await cur.fetchall()}
        week = []
        for d in week_days:
            r = by_day.get(d, {})
            week.append({
                "day": d,
                "images": int(r.get("images") or 0),
                "anlas": float(r.get("anlas") or 0),
                "text_tokens": int(r.get("text_tokens") or 0),
                "requests": int(r.get("requests") or 0),
                "v5": int(r.get("v5") or 0),
            })
        return {
            "today": {
                "images": int(today_images),
                # 成员出图：不含测试 Key 和已删除的 Key（例如早先的冒烟测试）；上面的总数是账号真实消耗
                "member_images": int(await one(
                    """SELECT COALESCE(SUM(c.images),0) FROM counters c JOIN api_keys k ON k.id=c.key_id
                       WHERE c.day=? AND k.is_test=0""", (today,))),
                "anlas": round(float(today_anlas), 2),
                "text_tokens": int(today_tokens),
                "requests": int(today_requests),
                "v5": int(today_v5),
                **anomaly["today"],
            },
            "keys_total": int(keys_total),
            "keys_active": int(keys_active),
            "week": week,
            "month": {"anlas": round(float(await one(
                "SELECT COALESCE(SUM(anlas),0) FROM counters WHERE substr(day,1,7)=?",
                (today[:7],))), 2), **anomaly["month"]},
        }
