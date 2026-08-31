import threading

from push2xteink.models import Config, Feed, FetchConfig, ProxyConfig, Task, XteinkConfig, AIConfig, AIProvider
from push2xteink.pipeline import RunOutcome
from push2xteink.scheduler import Scheduler
from push2xteink.state import State


def _config(**over) -> Config:
    base = dict(
        xteink=XteinkConfig(username="u", password="p"),
        proxy=ProxyConfig(), fetch=FetchConfig(),
        ai=AIConfig(primary=AIProvider(base_url="https://a/v1", api_key="k", model="m")),
        feeds=[Feed(id="a", url="https://a.example/rss")],
        tasks=[
            Task(id="t1", name="T1", feeds=["a"], schedule="0 7 * * *", enabled=True),
            Task(id="t2", name="T2", feeds=["a"], schedule="0 8 * * *", enabled=False),
        ],
    )
    base.update(over)
    return Config(**base)


class FakePipeline:
    def __init__(self): self.calls = []; self.closed = False; self.block = None
    def run_task(self, task_id, *, now=None):
        self.calls.append(task_id)
        if self.block is not None:
            self.block.wait(timeout=5)
        return RunOutcome(task_id, "success", 1, "f.epub", "rec", None)
    def close(self): self.closed = True


def _sched(config, state, fp=None):
    fp = fp or FakePipeline()
    s = Scheduler(config, state, pipeline_factory=lambda c, st: fp)
    return s, fp


def test_run_now_invokes_pipeline_and_returns_outcome(tmp_path):
    st = State(tmp_path / "s.db")
    s, fp = _sched(_config(), st)
    out = s.run_now("t1")
    assert out.status == "success" and fp.calls == ["t1"]
    assert s.active_count == 0
    st.close()


def test_enabled_task_ids_excludes_disabled(tmp_path):
    st = State(tmp_path / "s.db")
    s, _ = _sched(_config(), st)
    assert s.enabled_task_ids == ["t1"]
    st.close()


def test_register_jobs_only_enabled(tmp_path):
    st = State(tmp_path / "s.db")
    s, _ = _sched(_config(), st)
    s._register_jobs()
    job_ids = {j.id for j in s._aps.get_jobs()}
    assert job_ids == {"t1"}
    st.close()


def test_active_count_tracks_in_flight_run(tmp_path):
    st = State(tmp_path / "s.db")
    fp = FakePipeline(); fp.block = threading.Event()
    s, _ = _sched(_config(), st, fp)
    t = threading.Thread(target=s.run_now, args=("t1",)); t.start()
    # wait until the run has started
    for _ in range(200):
        if fp.calls: break
        __import__("time").sleep(0.01)
    try:
        assert s.active_count == 1
    finally:
        fp.block.set(); t.join(timeout=5)
    assert s.active_count == 0
    st.close()


def test_run_now_survives_unexpected_pipeline_error(tmp_path):
    st = State(tmp_path / "s.db")
    class Boom:
        def run_task(self, tid, *, now=None): raise RuntimeError("nope")
        def close(self): pass
    s = Scheduler(_config(), st, pipeline_factory=lambda c, st: Boom())
    out = s.run_now("t1")
    assert out.status == "failed"
    assert s.active_count == 0     # finally still ran
    st.close()


def test_start_registers_and_starts(tmp_path):
    st = State(tmp_path / "s.db")
    s, _ = _sched(_config(), st)
    s.start()
    try:
        assert s._aps.running
        assert {j.id for j in s._aps.get_jobs()} == {"t1"}
    finally:
        s.shutdown()
    st.close()


def test_shutdown_closes_pipeline_and_is_idempotent(tmp_path):
    st = State(tmp_path / "s.db")
    s, fp = _sched(_config(), st)
    s.start()
    s.shutdown()
    assert fp.closed is True
    s.shutdown()  # no raise
    st.close()


def test_shutdown_drains_run_now_in_flight(tmp_path):
    import time
    st = State(tmp_path / "s.db")
    fp = FakePipeline(); fp.block = threading.Event()
    s, _ = _sched(_config(), st, fp)
    s.start()
    t = threading.Thread(target=s.run_now, args=("t1",)); t.start()
    for _ in range(200):
        if fp.calls: break
        time.sleep(0.01)
    # release shortly after shutdown() begins waiting
    threading.Timer(0.3, fp.block.set).start()
    s.shutdown()                       # must block until the run finishes
    assert s.active_count == 0 and fp.closed is True
    t.join(timeout=5)
    st.close()


def test_reload_swaps_config_and_jobs(tmp_path):
    st = State(tmp_path / "s.db")
    pipelines = []
    def factory(c, s):
        fp = FakePipeline(); fp.cfg = c; pipelines.append(fp); return fp
    s = Scheduler(_config(), st, pipeline_factory=factory)
    s.start()
    try:
        new = _config(tasks=[
            Task(id="t1", name="T1", feeds=["a"], schedule="0 7 * * *", enabled=False),
            Task(id="t3", name="T3", feeds=["a"], schedule="0 9 * * *", enabled=True),
        ])
        s.reload(new)
        assert s.enabled_task_ids == ["t3"]
        assert {j.id for j in s._aps.get_jobs()} == {"t3"}
        assert pipelines[0].closed is True      # old pipeline closed
        assert pipelines[1].closed is False     # new one live
    finally:
        s.shutdown()
    st.close()


def test_reload_waits_for_in_flight_run_before_closing_old_pipeline(tmp_path):
    import time
    st = State(tmp_path / "s.db")
    made = []
    def factory(c, s):
        fp = FakePipeline(); made.append(fp); return fp
    s = Scheduler(_config(), st, pipeline_factory=factory)
    s.start()
    old = made[0]; old.block = threading.Event()
    t = threading.Thread(target=s.run_now, args=("t1",)); t.start()
    for _ in range(200):
        if old.calls: break
        time.sleep(0.01)
    threading.Timer(0.3, old.block.set).start()
    s.reload(_config())               # must block on drain before closing `old`
    assert old.closed is True         # closed only after the run finished
    t.join(timeout=5)
    s.shutdown()
    st.close()


def test_double_start_is_noop(tmp_path):
    st = State(tmp_path / "s.db")
    s, _ = _sched(_config(), st)
    s.start()
    try:
        s.start()  # no SchedulerAlreadyRunningError
        assert s._aps.running
    finally:
        s.shutdown()
    st.close()


def test_reload_after_shutdown_is_noop(tmp_path):
    st = State(tmp_path / "s.db")
    s, _ = _sched(_config(), st)
    s.start()
    s.shutdown()
    s.reload(_config())  # no SchedulerNotRunningError
    s.start()            # also a no-op post-shutdown
    assert s._aps.running is False
    st.close()


def test_shutdown_closes_orphans_even_if_current_pipeline_close_raises(tmp_path):
    st = State(tmp_path / "s.db")
    made = []
    def factory(c, s):
        fp = FakePipeline(); made.append(fp); return fp
    s = Scheduler(_config(), st, pipeline_factory=factory, drain_timeout=0.05)
    s.start()
    old = made[0]; old.block = threading.Event()
    t = threading.Thread(target=s.run_now, args=("t1",)); t.start()
    for _ in range(200):
        if old.calls: break
        __import__("time").sleep(0.01)
    s.reload(_config())               # drain times out -> old deferred to _orphans
    old.block.set(); t.join(timeout=5)
    assert s._orphans == [old]
    current = made[1]
    def boom(): raise RuntimeError("close failed")
    current.close = boom
    s.shutdown()                      # must not raise; orphan still closed
    assert old.closed is True
    st.close()
