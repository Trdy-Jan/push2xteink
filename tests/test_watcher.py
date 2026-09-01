import time

from push2xteink.watcher import ConfigWatcher


def test_watcher_calls_on_tick_until_stopped(tmp_path):
    hits = []
    w = ConfigWatcher(tmp_path / "c.yaml", lambda: hits.append(1), interval=0.05)
    w.start()
    time.sleep(0.3)
    w.stop()
    n = len(hits)
    assert n >= 2
    time.sleep(0.15)
    assert len(hits) == n            # stopped


def test_watcher_survives_on_tick_exception(tmp_path):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("x")

    w = ConfigWatcher(tmp_path / "c.yaml", boom, interval=0.05)
    w.start()
    time.sleep(0.25)
    w.stop()
    assert len(calls) >= 2           # kept ticking despite exceptions


def test_start_stop_idempotent(tmp_path):
    w = ConfigWatcher(tmp_path / "c.yaml", lambda: None, interval=0.05)
    w.start()
    w.start()
    w.stop()
    w.stop()
