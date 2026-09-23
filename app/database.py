"""SQLite 持久化：虚拟 key、每日计数器、用量日志。"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import aiosqlite

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
    expires_at REAL,
    created_at REAL NOT NULL,
    last_used_at REAL
);
CREATE TABLE IF NOT EXISTS site_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
    detail TEXT NOT NULL DEFAULT ''
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
CREATE TABLE IF NOT EXISTS upstream_token_settings (
    token_id TEXT PRIMARY KEY,
    v5_daily_limit INTEGER NOT NULL
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


class Database:
    def __init__(self, path: str, tz: str = "Asia/Shanghai"):
        self.path = path
        self.tz = tz
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.path)
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
            "ALTER TABLE upstream_token_counters ADD COLUMN images INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                await self._db.execute(ddl)
                await self._db.commit()
            except aiosqlite.OperationalError:
                pass  # 列已存在
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

    # ---------- upstream token counters ----------
    async def get_upstream_counter(self, token_id: str, day: str) -> dict[str, int]:
        cur = await self._db.execute(
            "SELECT images, v5 FROM upstream_token_counters WHERE token_id=? AND day=?",
            (token_id, day),
        )
        row = await cur.fetchone()
        if not row:
            return {"images": 0, "v5": 0}
        return {"images": int(row["images"]), "v5": int(row["v5"])}

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

    async def get_upstream_v5_counter(self, token_id: str, day: str) -> int:
        return (await self.get_upstream_counter(token_id, day))["v5"]

    async def bump_upstream_image_counter(self, token_id: str, day: str,
                                           images: int) -> None:
        if images < 1:
            return
        await self._db.execute(
            """INSERT INTO upstream_token_counters(token_id, day, images) VALUES (?,?,?)
               ON CONFLICT(token_id, day) DO UPDATE SET images=images+excluded.images""",
            (token_id, day, images),
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
               daily_text_tokens, rpm, allow_anlas, allow_img2img, exclude_global_v5, image_model_scope, is_admin, expires_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            "daily_text_tokens", "rpm", "allow_anlas", "allow_img2img", "exclude_global_v5", "image_model_scope", "expires_at",
        }
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append(f"{k}=?")
            if k in ("enabled", "allow_anlas", "allow_img2img", "exclude_global_v5"):
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
        await self._db.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
        await self._db.commit()

    async def inactive_key_ids(self, cutoff: float) -> list[int]:
        """返回超过截止时间未通过鉴权使用的 Key；从未使用时按创建时间计算。"""
        cur = await self._db.execute(
            """SELECT id FROM api_keys
               WHERE is_admin=0 AND COALESCE(last_used_at, created_at) < ?
               ORDER BY id ASC""",
            (cutoff,),
        )
        return [int(row["id"]) for row in await cur.fetchall()]

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
            """INSERT INTO counters (key_id, day, images, anlas, text_tokens, requests, v5, legacy_free_images)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(key_id, day) DO UPDATE SET
                 images = images + excluded.images,
                 anlas = anlas + excluded.anlas,
                 text_tokens = text_tokens + excluded.text_tokens,
                 requests = requests + excluded.requests,
                 v5 = v5 + excluded.v5,
                 legacy_free_images = legacy_free_images + excluded.legacy_free_images""",
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

    # ---------- logs ----------
    async def add_log(
        self, key_id: Optional[int], key_name: str, kind: str, model: str,
        status: str, images: int = 0, anlas: float = 0.0, tokens: int = 0,
        detail: str = "",
    ) -> None:
        await self._db.execute(
            """INSERT INTO usage_log (ts, key_id, key_name, kind, model, status,
                                      images, anlas, tokens, detail)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), key_id, key_name, kind, model, status,
             images, anlas, tokens, detail[:500]),
        )
        await self._db.commit()

    async def list_logs(self, limit: int = 20, offset: int = 0,
                        key_id: Optional[int] = None) -> list[aiosqlite.Row]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        if key_id:
            cur = await self._db.execute(
                "SELECT * FROM usage_log WHERE key_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (key_id, limit, offset),
            )
        else:
            cur = await self._db.execute(
                "SELECT * FROM usage_log ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
            )
        return list(await cur.fetchall())

    async def count_logs(self, key_id: Optional[int] = None) -> int:
        if key_id:
            cur = await self._db.execute(
                "SELECT COUNT(*) AS c FROM usage_log WHERE key_id=?", (key_id,)
            )
        else:
            cur = await self._db.execute("SELECT COUNT(*) AS c FROM usage_log")
        row = await cur.fetchone()
        return int(row["c"])

    # ---------- overview ----------
    async def overview(self, today: str, week_days: list[str]) -> dict[str, Any]:
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
        ph = ",".join("?" * len(week_days))
        cur = await self._db.execute(
            f"""SELECT day,
                       SUM(images) AS images, SUM(anlas) AS anlas,
                       SUM(text_tokens) AS text_tokens, SUM(requests) AS requests
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
            })
        return {
            "today": {
                "images": int(today_images),
                "anlas": round(float(today_anlas), 2),
                "text_tokens": int(today_tokens),
                "requests": int(today_requests),
                "v5": int(today_v5),
            },
            "keys_total": int(keys_total),
            "keys_active": int(keys_active),
            "week": week,
            "month": {"anlas": round(float(await one(
                "SELECT COALESCE(SUM(anlas),0) FROM counters WHERE substr(day,1,7)=?",
                (today[:7],))), 2)},
        }
