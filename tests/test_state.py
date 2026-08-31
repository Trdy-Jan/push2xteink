from push2xteink.state import State


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
