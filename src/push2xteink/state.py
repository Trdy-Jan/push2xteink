from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_items (
  feed_id       TEXT NOT NULL,
  item_guid     TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  pushed_at     TEXT,
  PRIMARY KEY (feed_id, item_guid)
);
CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  status      TEXT NOT NULL CHECK (status IN ('running','success','skipped','failed')),
  item_count  INTEGER,
  file_name   TEXT,
  message     TEXT
);
CREATE TABLE IF NOT EXISTS kv (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  updated_at TEXT
);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class State:
    def __init__(self, db_path: Path | str) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.commit()
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- kv ---
    def kv_get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kv WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, _iso(_utcnow())),
            )
            self._conn.commit()

    # --- seen_items ---
    def record_seen(
        self, feed_id: str, guid: str, *, now: datetime | None = None
    ) -> None:
        with self._lock:
            ts = _iso(now or _utcnow())
            self._conn.execute(
                "INSERT OR IGNORE INTO seen_items(feed_id, item_guid, first_seen_at) "
                "VALUES(?, ?, ?)",
                (feed_id, guid, ts),
            )
            self._conn.commit()

    def is_item_pushable(
        self,
        feed_id: str,
        guid: str,
        lookback_hours: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        with self._lock:
            now = now or _utcnow()
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            row = self._conn.execute(
                "SELECT first_seen_at, pushed_at FROM seen_items "
                "WHERE feed_id = ? AND item_guid = ?",
                (feed_id, guid),
            ).fetchone()
            if row is None:
                return True
            if row["pushed_at"] is not None:
                return False
            first_seen = datetime.fromisoformat(row["first_seen_at"])
            return first_seen >= now - timedelta(hours=lookback_hours)

    def mark_pushed(
        self, feed_id: str, guids: list[str], *, now: datetime | None = None
    ) -> None:
        with self._lock:
            ts = _iso(now or _utcnow())
            self._conn.executemany(
                "UPDATE seen_items SET pushed_at = ? "
                "WHERE feed_id = ? AND item_guid = ?",
                [(ts, feed_id, g) for g in guids],
            )
            self._conn.commit()

    # --- runs ---
    def start_run(self, task_id: str, *, now: datetime | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO runs(task_id, started_at, status) VALUES(?, ?, 'running')",
                (task_id, _iso(now or _utcnow())),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        status: Literal["running", "success", "skipped", "failed"],
        item_count: int | None = None,
        file_name: str | None = None,
        message: str | None = None,
        now: datetime | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET finished_at = ?, status = ?, item_count = ?, "
                "file_name = ?, message = ? WHERE id = ?",
                (_iso(now or _utcnow()), status, item_count, file_name, message, run_id),
            )
            self._conn.commit()

    def task_has_successful_run(self, task_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM runs WHERE task_id = ? AND status = 'success' LIMIT 1",
                (task_id,),
            ).fetchone()
            return row is not None

    def has_success_on_day(self, task_id: str, day: str) -> bool:
        """day is 'YYYY-MM-DD' (UTC). True if this task has a status='success'
        run started that day."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM runs WHERE task_id = ? AND status = 'success' "
                "AND started_at >= ? AND started_at < ? LIMIT 1",
                (task_id, f"{day}T00:00:00+00:00", f"{day}T99"),
            ).fetchone()
        return row is not None

    def recent_runs(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
