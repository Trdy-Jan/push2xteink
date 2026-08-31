from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
  status      TEXT NOT NULL,
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
    return dt.astimezone(timezone.utc).isoformat()


class State:
    def __init__(self, db_path: Path | str) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- kv ---
    def kv_get(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM kv WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
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
        now = now or _utcnow()
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
        ts = _iso(now or _utcnow())
        self._conn.executemany(
            "UPDATE seen_items SET pushed_at = ? "
            "WHERE feed_id = ? AND item_guid = ?",
            [(ts, feed_id, g) for g in guids],
        )
        self._conn.commit()
