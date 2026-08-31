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
            self.block.wait()
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
    assert s.active_count == 1
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
