import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.base import STATE_RUNNING

from push2xteink.config import load_config
from push2xteink.models import Config, Feed, FetchConfig, ProxyConfig, Task, XteinkConfig, AIConfig, AIProvider
from push2xteink.pipeline import RunOutcome
from push2xteink.scheduler import Scheduler
from push2xteink.state import State

FIXTURE = Path(__file__).parent / "fixtures" / "config_valid.yaml"


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
    def __init__(self):
        self.calls = []
        self.closed = False
        self.closed_while_active = False
        self.in_flight = 0
        self.block = None
    def run_task(self, task_id, *, now=None):
        self.in_flight += 1
        self.calls.append(task_id)
        try:
            if self.block is not None:
                self.block.wait(timeout=5)
            return RunOutcome(task_id, "success", 1, "f.epub", "rec", None)
        finally:
            self.in_flight -= 1
    def close(self):
        if self.in_flight > 0:
            self.closed_while_active = True
        self.closed = True


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
    assert fp.closed_while_active is False   # drained before close()
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
    # The real failure: a run that captured `old` is still in_flight when
    # close() runs. Fails against a missing drain AND against the I1 swap race.
    assert old.closed_while_active is False
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


# --- I1: the reload drain->swap race ---
def test_reload_swap_race_never_closes_a_pipeline_mid_run(tmp_path):
    import time
    st = State(tmp_path / "s.db")
    made = []
    swap_window = threading.Event()
    def factory(c, s):
        fp = FakePipeline(); made.append(fp)
        if len(made) == 2:            # called by reload(), while it holds the swap
            swap_window.set()
            time.sleep(0.15)          # give the racing run a chance to sneak in
        return fp
    s = Scheduler(_config(), st, pipeline_factory=factory)
    s.start()
    old = made[0]; old.block = threading.Event()

    def racing_run():
        swap_window.wait(2)
        s.run_now("t1")               # must capture the NEW pipeline, not `old`

    rr = threading.Thread(target=racing_run); rr.start()
    s.reload(_config())
    old.block.set()
    rr.join(5)
    s.shutdown()
    assert old.closed is True
    # Against the drain->swap race the racing run captured `old` and is still
    # in_flight when reload closes it.
    assert old.closed_while_active is False
    assert made[1].calls == ["t1"]   # racing run hit the post-swap pipeline
    st.close()


# --- C1: a bad new config must not brick the scheduler ---
def test_reload_aborts_and_stays_running_when_factory_raises(tmp_path):
    st = State(tmp_path / "s.db")
    made = []
    def factory(c, s):
        if made:
            raise RuntimeError("bad proxy.url: scheme missing")
        fp = FakePipeline(); made.append(fp); return fp
    s = Scheduler(_config(), st, pipeline_factory=factory)
    s.start()
    try:
        s.reload(_config(tasks=[
            Task(id="t3", name="T3", feeds=["a"], schedule="0 9 * * *", enabled=True),
        ]))  # must NOT raise
        assert s._aps.state == STATE_RUNNING       # resumed, not left PAUSED
        assert made[0].closed is False             # old pipeline still live
        assert {j.id for j in s._aps.get_jobs()} == {"t1"}   # jobs unchanged
        assert [t.id for t in s.config.tasks] == ["t1", "t2"]  # config unchanged
        assert s.run_now("t1").status == "success"  # still functional
    finally:
        s.shutdown()
    st.close()


# --- I2: reload() re-entrancy ---
def test_concurrent_reloads_do_not_leak_or_mix(tmp_path):
    st = State(tmp_path / "s.db")
    made = []
    mk_lock = threading.Lock()
    def factory(c, s):
        with mk_lock:
            fp = FakePipeline(); made.append(fp); return fp
    s = Scheduler(_config(), st, pipeline_factory=factory)
    s.start()
    cfg_a = _config(tasks=[Task(id="ta", name="A", feeds=["a"], schedule="0 7 * * *", enabled=True)])
    cfg_b = _config(tasks=[Task(id="tb", name="B", feeds=["a"], schedule="0 8 * * *", enabled=True)])
    gate = threading.Event()
    def do(cfg):
        gate.wait(2); s.reload(cfg)
    t1 = threading.Thread(target=do, args=(cfg_a,))
    t2 = threading.Thread(target=do, args=(cfg_b,))
    t1.start(); t2.start(); gate.set()
    t1.join(5); t2.join(5)
    s.shutdown()
    assert all(p.closed for p in made)              # no leak
    assert not any(p.closed_while_active for p in made)
    winner = [t.id for t in s.config.tasks]
    assert winner in (["ta"], ["tb"])              # wholly one input, not a mix
    st.close()


# --- I3 + M7: run_now respects non-concurrency and refuses post-shutdown ---
def test_run_now_skips_when_task_already_active(tmp_path):
    import time
    st = State(tmp_path / "s.db")
    fp = FakePipeline(); fp.block = threading.Event()
    s, _ = _sched(_config(), st, fp)
    t = threading.Thread(target=s.run_now, args=("t1",)); t.start()
    for _ in range(200):
        if fp.calls: break
        time.sleep(0.01)
    try:
        out = s.run_now("t1")
        assert out.status == "skipped" and "already running" in out.message
    finally:
        fp.block.set(); t.join(5)
    assert fp.calls == ["t1"]      # pipeline invoked only once
    st.close()


def test_run_now_after_shutdown_returns_failed(tmp_path):
    st = State(tmp_path / "s.db")
    s, fp = _sched(_config(), st)
    s.start()
    s.shutdown()
    out = s.run_now("t1")
    assert out.status == "failed" and "shut down" in out.message
    assert fp.calls == []          # pipeline not called
    st.close()


# --- P5-enabling accessors ---
def test_config_property_reflects_live_config(tmp_path):
    st = State(tmp_path / "s.db")
    s, _ = _sched(_config(), st)
    s.start()
    try:
        assert [t.id for t in s.config.tasks] == ["t1", "t2"]
        s.reload(_config(tasks=[
            Task(id="t9", name="T9", feeds=["a"], schedule="0 7 * * *", enabled=True),
        ]))
        assert [t.id for t in s.config.tasks] == ["t9"]
    finally:
        s.shutdown()
    st.close()


def test_submit_dispatches_manual_run_via_executor(tmp_path):
    st = State(tmp_path / "s.db")
    fp = FakePipeline()
    s = Scheduler(_config(), st, pipeline_factory=lambda c, x: fp)
    s.start()
    try:
        s.submit("t1")
        for _ in range(200):
            if fp.calls:
                break
            time.sleep(0.01)
        assert fp.calls == ["t1"]
        assert {j.id for j in s._aps.get_jobs()} & {"manual:t1", "t1"}  # cron job unaffected
    finally:
        s.shutdown()
    st.close()


def test_maybe_reload_only_on_change(tmp_path):
    cfgfile = tmp_path / "c.yaml"
    shutil.copy(FIXTURE, cfgfile)
    st = State(tmp_path / "s.db")
    built = []
    s = Scheduler(load_config(cfgfile), st, pipeline_factory=lambda c, x: (built.append(c) or FakePipeline()))
    s.start()
    s.prime_config_token(cfgfile)
    try:
        assert s.maybe_reload(cfgfile) is False        # unchanged
        cfgfile.write_text(cfgfile.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        import os
        os.utime(cfgfile, None)
        assert s.maybe_reload(cfgfile) is True          # changed
        assert s.maybe_reload(cfgfile) is False         # unchanged again
    finally:
        s.shutdown()
    st.close()


def test_maybe_reload_bad_config_keeps_running(tmp_path, caplog):
    cfgfile = tmp_path / "c.yaml"
    shutil.copy(FIXTURE, cfgfile)
    st = State(tmp_path / "s.db")
    s = Scheduler(load_config(cfgfile), st, pipeline_factory=lambda c, x: FakePipeline())
    s.start()
    s.prime_config_token(cfgfile)
    try:
        cfgfile.write_text("xteink: [unclosed\n", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="push2xteink.scheduler"):
            assert s.maybe_reload(cfgfile) is False
            warnings = [
                r for r in caplog.records
                if r.levelno == logging.WARNING
                and ("invalid" in r.getMessage() or "reload" in r.getMessage())
            ]
            assert len(warnings) == 1
            # same bad file again -> logged once per distinct bad token
            assert s.maybe_reload(cfgfile) is False
            warnings = [
                r for r in caplog.records
                if r.levelno == logging.WARNING
                and ("invalid" in r.getMessage() or "reload" in r.getMessage())
            ]
            assert len(warnings) == 1
        assert s._aps.running                            # still up
    finally:
        s.shutdown()
    st.close()


def test_maybe_reload_does_not_advance_token_when_swap_aborts(tmp_path):
    cfgfile = tmp_path / "c.yaml"
    shutil.copy(FIXTURE, cfgfile)
    st = State(tmp_path / "s.db")
    calls = {"n": 0}

    def factory(c, x):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("cannot build pipeline for this config")
        return FakePipeline()

    s = Scheduler(load_config(cfgfile), st, pipeline_factory=factory)
    s.start()
    s.prime_config_token(cfgfile)
    try:
        cfgfile.write_text(cfgfile.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        # valid YAML+schema, but the pipeline factory raises -> reload aborts
        assert s.maybe_reload(cfgfile) is False
        assert s._aps.running
        # token NOT advanced: a later successful build retries the same file
        calls["n"] = 0
        assert s.maybe_reload(cfgfile) is True
    finally:
        s.shutdown()
    st.close()


def test_next_run_time_accessor(tmp_path):
    st = State(tmp_path / "s.db")
    s, _ = _sched(_config(), st)
    s.start()
    try:
        assert isinstance(s.next_run_time("t1"), datetime)   # enabled
        assert s.next_run_time("t2") is None                 # disabled -> no job
        assert s.next_run_time("ghost") is None              # unknown
    finally:
        s.shutdown()
    st.close()
