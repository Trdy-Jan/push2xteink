from datetime import datetime, timedelta, timezone

import pytest

from push2xteink.state import State

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def test_creates_tables_idempotently(tmp_path):
    db = tmp_path / "state.db"
    State(db).close()
    # 再次打开不报错
    s = State(db)
    s.close()


def test_kv_roundtrip(tmp_path):
    s = State(tmp_path / "state.db")
    assert s.kv_get("token") is None
    s.kv_set("token", "abc")
    assert s.kv_get("token") == "abc"
    s.kv_set("token", "def")
    assert s.kv_get("token") == "def"
    s.close()


def test_new_guid_is_pushable(tmp_path):
    s = State(tmp_path / "s.db")
    assert s.is_item_pushable("hn", "g1", 48, now=NOW) is True
    s.close()


def test_record_seen_is_idempotent(tmp_path):
    s = State(tmp_path / "s.db")
    s.record_seen("hn", "g1", now=NOW)
    s.record_seen("hn", "g1", now=NOW + timedelta(hours=1))
    row = s._conn.execute(
        "SELECT first_seen_at FROM seen_items WHERE feed_id='hn' AND item_guid='g1'"
    ).fetchone()
    assert row["first_seen_at"] == NOW.isoformat()
    s.close()


def test_seen_unpushed_within_window_is_pushable(tmp_path):
    s = State(tmp_path / "s.db")
    s.record_seen("hn", "g1", now=NOW - timedelta(hours=10))
    assert s.is_item_pushable("hn", "g1", 48, now=NOW) is True
    s.close()


def test_seen_unpushed_outside_window_not_pushable(tmp_path):
    s = State(tmp_path / "s.db")
    s.record_seen("hn", "g1", now=NOW - timedelta(hours=60))
    assert s.is_item_pushable("hn", "g1", 48, now=NOW) is False
    s.close()


def test_pushed_item_not_pushable(tmp_path):
    s = State(tmp_path / "s.db")
    s.record_seen("hn", "g1", now=NOW - timedelta(hours=1))
    s.mark_pushed("hn", ["g1"], now=NOW)
    assert s.is_item_pushable("hn", "g1", 48, now=NOW + timedelta(hours=1)) is False
    s.close()


def test_mark_pushed_only_affects_listed_guids(tmp_path):
    s = State(tmp_path / "s.db")
    s.record_seen("hn", "g1", now=NOW)
    s.record_seen("hn", "g2", now=NOW)
    s.mark_pushed("hn", ["g1"], now=NOW)
    assert s.is_item_pushable("hn", "g1", 48, now=NOW) is False
    assert s.is_item_pushable("hn", "g2", 48, now=NOW) is True
    s.close()


def test_run_lifecycle(tmp_path):
    s = State(tmp_path / "s.db")
    rid = s.start_run("brief", now=NOW)
    assert isinstance(rid, int)
    assert s.task_has_successful_run("brief") is False

    s.finish_run(
        rid, status="success", item_count=3, file_name="早报_20260831.epub",
        now=NOW + timedelta(minutes=2),
    )
    assert s.task_has_successful_run("brief") is True

    row = s.recent_runs(10)[0]
    assert row["status"] == "success"
    assert row["item_count"] == 3
    assert row["file_name"] == "早报_20260831.epub"
    assert row["finished_at"] == (NOW + timedelta(minutes=2)).isoformat()
    s.close()


def test_failed_run_does_not_count_as_success(tmp_path):
    s = State(tmp_path / "s.db")
    rid = s.start_run("brief", now=NOW)
    s.finish_run(rid, status="failed", message="upload 500", now=NOW)
    assert s.task_has_successful_run("brief") is False
    s.close()


def test_recent_runs_ordered_desc(tmp_path):
    s = State(tmp_path / "s.db")
    r1 = s.start_run("a", now=NOW)
    r2 = s.start_run("b", now=NOW + timedelta(minutes=1))
    ids = [row["id"] for row in s.recent_runs(10)]
    assert ids == [r2, r1]
    s.close()
