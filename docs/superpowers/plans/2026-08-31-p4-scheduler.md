# P4 调度（scheduler + serve 入口）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 实现 `scheduler.py`——用 APScheduler 按每个 `enabled` task 的 cron 表达式定时调用 `Pipeline.run_task`，支持配置热重载（reload 前排空在飞的 run）——外加 `python -m push2xteink serve` 单进程入口：启动调度器、监听信号优雅退出、监视 `config.yaml` 变更自动 reload。

**Architecture:** 一个 `Scheduler` 类，持有 `Config` + `State` + 一个 `Pipeline`（P3 的 `from_config`）+ 一个 `BackgroundScheduler`。job 函数 = 内部 `_run(task_id)`，它做「登记活跃 → `pipeline.run_task` → 注销活跃」，活跃计数用 `threading.Condition` 以便 reload/shutdown 排空。`reload(new_config)` = `pause → 排空 → 换 Pipeline + 重注册 job → resume → close 旧 Pipeline`。APScheduler 的 executor 线程池压到 4（spec §14：每个 job 内部还会 fan out `fetch.concurrency` 个线程）。`serve` 命令在 CLI 里，用 `threading.Event` + 信号处理阻塞主线程，顺带每 5s 检查 config 文件 mtime。

**Tech Stack:** Python 3.12、APScheduler 3.x（已是依赖）、标准库（`signal`、`threading`、`os`）、pytest。无新第三方依赖。

**Spec:** `docs/superpowers/specs/2026-08-31-push2xteink-design.md` 第 9 节（调度器）、第 3、10 节（单进程入口）、第 13 节（APScheduler 星期语义）、第 14 节「P3 实现后补充」（reload 排空、executor 池上限）。

## Global Constraints

- Python `>=3.12`；`X | None` / `list[...]` 标注。
- APScheduler：`from apscheduler.schedulers.background import BackgroundScheduler`、`from apscheduler.triggers.cron import CronTrigger`、`from apscheduler.executors.pool import ThreadPoolExecutor as APSThreadPoolExecutor`。
- job defaults：`max_instances=1`、`coalesce=True`、`misfire_grace_time=None`（spec §9：同 task 不并发；停机错过的 job 重启后合并成一次补跑）。
- executor：`{"default": APSThreadPoolExecutor(4)}`（spec §14——不要用默认的 10）。
- **不用持久化 jobstore**——job 每次 `start()` / `reload()` 从 config 重新推导，用默认内存 jobstore。
- **`_run` 绝不抛异常**：`Pipeline.run_task` 本身已保证不抛（P3），但 `_run` 里活跃计数的 `finally` 必须无条件执行；job 函数里任何意外也要吞掉并记日志（APScheduler 会把 job 异常打到它自己的 logger，可接受，但活跃计数不能漏减）。
- reload 顺序严格按 spec §14：`pause → 排空活跃 job → 换 Pipeline/job → resume → close 旧 Pipeline`。**先 resume 再 close**——close 旧 Pipeline 时新 Pipeline 已就位，且已排空所以没有 run 在用旧的。
- `Scheduler` 不拥有 `State`（和 `Pipeline` 一样）——调用方（`serve`）建 `State`、`Scheduler` 用它、`serve` 负责 `state.close()`。
- 源文件 `src/push2xteink/`，测试 `tests/`。

## P1–P3 依赖签名（master 上已合并，不可改）

```python
# pipeline
class Pipeline:
    @classmethod
    def from_config(cls, config: Config, state: State, *, output_dir=None) -> Pipeline
    def run_task(self, task_id: str, *, now: datetime | None = None) -> RunOutcome   # 绝不抛
    def close(self) -> None
@dataclass
class RunOutcome:
    task_id: str; status: str; item_count: int; file_name: str | None
    record_id: str | None; message: str | None
# config / models
def load_config(path: Path) -> Config ; class ConfigError(Exception)
# Config: .tasks list[Task] ; Task: .id .name .schedule(str, 合法 cron，P1 已校验) .enabled(bool) ...
# state
class State:
    def __init__(self, db_path) -> None
    def close(self) -> None
# cli (P3 已有 run / list；本计划加 serve)
def main(argv: list[str] | None = None) -> int
```

## File Structure

| 文件 | 职责 |
|---|---|
| `src/push2xteink/scheduler.py` | `Scheduler`（`start` / `shutdown` / `reload` / `run_now` / 内部 `_run` + 活跃排空） |
| `src/push2xteink/cli.py` | 追加 `serve` 子命令 + `_serve()` |
| `tests/test_scheduler.py` | `Scheduler` 单测（fake pipeline factory，不依赖 cron 真实触发） |
| `tests/test_cli.py` | 追加 `serve` 测试（注入 fake `Scheduler` + 预置 stop event） |

## Interfaces（本计划对外产出）

```python
# push2xteink.scheduler
class Scheduler:
    def __init__(
        self, config: Config, state: State, *,
        pipeline_factory: "Callable[[Config, State], Pipeline]" = Pipeline.from_config,
        scheduler_factory: "Callable[[], BackgroundScheduler] | None" = None,  # 测试注入
        drain_timeout: float = 120.0,
    ) -> None
    def start(self) -> None                     # 注册 job + aps.start()
    def shutdown(self, *, wait: bool = True) -> None   # aps.shutdown + 排空 + pipeline.close()（幂等）
    def reload(self, new_config: Config) -> None       # pause → drain → swap → resume → close old
    def run_now(self, task_id: str) -> RunOutcome      # 同步执行一个 task（web「立即执行」/ 手动），登记为活跃
    @property
    def enabled_task_ids(self) -> list[str]
    @property
    def active_count(self) -> int

# push2xteink.cli  (追加)
#   子命令 serve：python -m push2xteink serve  → _serve(config_path, db_path) -> int
#   python -m push2xteink （无子命令）→ 默认等价于 serve
```

---

## Task 1: `Scheduler` 核心——`__init__` / `_run` / 活跃计数 / `run_now` / `_register_jobs`

**Files:**
- Create: `src/push2xteink/scheduler.py`
- Create: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `pipeline.Pipeline`, `pipeline.RunOutcome`, `models.Config`, `state.State`
- Produces: `Scheduler.__init__`, `Scheduler._run`, `Scheduler.run_now`, `Scheduler._register_jobs`, `Scheduler.enabled_task_ids`, `Scheduler.active_count`

**行为：**
- `__init__`：`self._pipeline = pipeline_factory(config, state)`；建 `BackgroundScheduler`（executor 池 4，job_defaults 如上）——除非 `scheduler_factory` 注入。活跃跟踪：`self._active: set[str]` + `self._cond = threading.Condition()`。
- `_run(task_id)`：
  ```
  with self._cond: self._active_ids.append(task_id)   # 允许同名？max_instances=1 保证同 task 不并发，但 run_now 可能与 cron 撞——用 list 计数即可
  try:
      outcome = self._pipeline.run_task(task_id)
      return outcome
  except Exception:                                   # run_task 不该抛，但兜底
      logger.exception("run_task(%s) raised unexpectedly", task_id)
      return None
  finally:
      with self._cond:
          self._active_ids.remove(task_id)
          self._cond.notify_all()
  ```
- `run_now(task_id)`：直接 `return self._run(task_id)`（同步、阻塞调用方线程）。给 web 的「立即执行」和 P3 CLI 未来复用。返回 `RunOutcome`（`_run` 返回 None 时包一个 `RunOutcome(task_id, "failed", message="scheduler internal error")`）。
- `_register_jobs()`：`self._aps.remove_all_jobs()`；对 `config.tasks` 里 `t.enabled` 的，`self._aps.add_job(self._run, CronTrigger.from_crontab(t.schedule), args=[t.id], id=t.id, replace_existing=True)`。
- `enabled_task_ids` → `[t.id for t in self._config.tasks if t.enabled]`。
- `active_count` → `len(self._active_ids)`（加锁读）。

- [ ] **Step 1: 写失败测试**

```python
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
```

- [ ] **Step 2: 运行 → 失败**（`ModuleNotFoundError: push2xteink.scheduler`）

- [ ] **Step 3: 实现**

```python
from __future__ import annotations

import logging
import threading
from typing import Callable

from apscheduler.executors.pool import ThreadPoolExecutor as APSThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .models import Config
from .pipeline import Pipeline, RunOutcome
from .state import State

logger = logging.getLogger(__name__)

_EXECUTOR_WORKERS = 4


def _default_scheduler() -> BackgroundScheduler:
    return BackgroundScheduler(
        executors={"default": APSThreadPoolExecutor(_EXECUTOR_WORKERS)},
        job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": None},
    )


class Scheduler:
    def __init__(
        self,
        config: Config,
        state: State,
        *,
        pipeline_factory: Callable[[Config, State], Pipeline] = Pipeline.from_config,
        scheduler_factory: Callable[[], BackgroundScheduler] | None = None,
        drain_timeout: float = 120.0,
    ) -> None:
        self._config = config
        self._state = state
        self._pipeline_factory = pipeline_factory
        self._pipeline = pipeline_factory(config, state)
        self._aps = (scheduler_factory or _default_scheduler)()
        self._drain_timeout = drain_timeout
        self._active_ids: list[str] = []
        self._cond = threading.Condition()
        self._shutdown = False

    # --- job body ---
    def _run(self, task_id: str) -> RunOutcome | None:
        with self._cond:
            self._active_ids.append(task_id)
        try:
            return self._pipeline.run_task(task_id)
        except Exception:  # noqa: BLE001 - run_task shouldn't raise; never leak
            logger.exception("run_task(%s) raised unexpectedly", task_id)
            return None
        finally:
            with self._cond:
                self._active_ids.remove(task_id)
                self._cond.notify_all()

    def run_now(self, task_id: str) -> RunOutcome:
        outcome = self._run(task_id)
        if outcome is None:
            return RunOutcome(task_id, "failed", 0, None, None, "scheduler internal error")
        return outcome

    # --- jobs ---
    def _register_jobs(self) -> None:
        self._aps.remove_all_jobs()
        for task in self._config.tasks:
            if not task.enabled:
                continue
            self._aps.add_job(
                self._run,
                CronTrigger.from_crontab(task.schedule),
                args=[task.id],
                id=task.id,
                replace_existing=True,
            )

    @property
    def enabled_task_ids(self) -> list[str]:
        return [t.id for t in self._config.tasks if t.enabled]

    @property
    def active_count(self) -> int:
        with self._cond:
            return len(self._active_ids)

    def start(self) -> None:  # Task 2
        raise NotImplementedError

    def shutdown(self, *, wait: bool = True) -> None:  # Task 2
        raise NotImplementedError

    def reload(self, new_config: Config) -> None:  # Task 3
        raise NotImplementedError
```

- [ ] **Step 4: 运行 → 全绿。Commit** `feat: Scheduler core - _run, run_now, job registration`

---

## Task 2: `start` / `shutdown` + 排空

**Files:**
- Modify: `src/push2xteink/scheduler.py`
- Modify: `tests/test_scheduler.py`

**Interfaces:**
- Produces: `Scheduler.start`, `Scheduler.shutdown`, `Scheduler._drain`

**行为：**
- `start()`：`self._register_jobs()`；`self._aps.start()`。
- `_drain(timeout)`：`with self._cond: self._cond.wait_for(lambda: not self._active_ids, timeout=timeout)`；超时返回 `False`（记 warning），否则 `True`。
- `shutdown(wait=True)`：幂等（`self._shutdown` 标记）。`self._aps.shutdown(wait=wait)`（`wait=True` → 阻塞到 APScheduler 自己的 job 跑完）；再 `self._drain(self._drain_timeout)` 兜底（`run_now` 触发的活跃不在 APScheduler 管辖内）；最后 `self._pipeline.close()`。

- [ ] **Step 1: 写失败测试**

```python
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
```

- [ ] **Step 2: 运行 → 失败**

- [ ] **Step 3: 实现** —— 替换 `start` / `shutdown` 桩、加 `_drain`：

```python
    def start(self) -> None:
        self._register_jobs()
        self._aps.start()

    def _drain(self, timeout: float) -> bool:
        with self._cond:
            drained = self._cond.wait_for(lambda: not self._active_ids, timeout=timeout)
        if not drained:
            logger.warning("drain timed out with %d active run(s)", self.active_count)
        return drained

    def shutdown(self, *, wait: bool = True) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        if self._aps.running:
            self._aps.shutdown(wait=wait)
        self._drain(self._drain_timeout)
        self._pipeline.close()
```

- [ ] **Step 4: 运行 → 全绿。Commit** `feat: Scheduler start/shutdown with drain`

---

## Task 3: `reload`——热重载（pause → 排空 → 换 Pipeline/job → resume → close 旧）

**Files:**
- Modify: `src/push2xteink/scheduler.py`
- Modify: `tests/test_scheduler.py`

**Interfaces:**
- Produces: `Scheduler.reload(new_config: Config) -> None`

**行为**（spec §14 P3 备注的严格顺序）：
```
self._aps.pause()                      # 停止触发新 job（已在跑的不受影响）
self._drain(self._drain_timeout)       # 等活跃 job（含 run_now）排空
old_pipeline = self._pipeline
self._pipeline = self._pipeline_factory(new_config, self._state)
self._config = new_config
self._register_jobs()                  # 用新 config 重建 job
self._aps.resume()
old_pipeline.close()                   # 新的已就位、已排空——安全
```
排空超时（`_drain` 返回 False）：仍继续 reload，但**不 close 旧 pipeline**（有 run 还在用它），记 warning，把旧 pipeline 挂到 `self._orphans` 列表，`shutdown` 时再 close。（简单起见：`self._orphans.append(old_pipeline)`，`shutdown` 里 `for p in self._orphans: p.close()`。）

- [ ] **Step 1: 写失败测试**

```python
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
```

- [ ] **Step 2: 运行 → 失败**

- [ ] **Step 3: 实现** —— `__init__` 加 `self._orphans: list[Pipeline] = []`；替换 `reload` 桩；`shutdown` 里加 orphan 清理：

```python
    def reload(self, new_config: Config) -> None:
        self._aps.pause()
        drained = self._drain(self._drain_timeout)

        old_pipeline = self._pipeline
        self._pipeline = self._pipeline_factory(new_config, self._state)
        self._config = new_config
        self._register_jobs()
        self._aps.resume()

        if drained:
            old_pipeline.close()
        else:
            logger.warning("reload could not drain; deferring old pipeline close to shutdown")
            self._orphans.append(old_pipeline)
```

`shutdown` 末尾（`self._pipeline.close()` 之后）：
```python
        for orphan in self._orphans:
            orphan.close()
        self._orphans.clear()
```

- [ ] **Step 4: 运行 → 全绿。Commit** `feat: Scheduler.reload with drain-before-close`

---

## Task 4: `serve` CLI 命令 + 信号 + config 监视

**Files:**
- Modify: `src/push2xteink/cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `scheduler.Scheduler`, `config.load_config` / `ConfigError`, `state.State`
- Produces: CLI 子命令 `serve`；`python -m push2xteink`（无子命令）默认走 serve

**行为：**
- `_parser()`：加 `sub.add_parser("serve", help="run the scheduler (blocks)")`。P3 已把 `dest="cmd"` 设为非必需——把 `main` 里 `args.cmd is None` 的分支从「报错 exit 2」改成「等价于 serve」。
- `_serve(config_path: Path, db_path: Path) -> int`：
  ```
  try: config = load_config(config_path)
  except ConfigError as e: stderr; return 2
  db_path.parent.mkdir(parents=True, exist_ok=True)
  try: state = State(db_path)
  except (OSError, sqlite3.Error) as e: stderr; return 2
  stop = threading.Event()
  for sig in (SIGINT, SIGTERM): signal.signal(sig, lambda *_: stop.set())
  sched = Scheduler(config, state)
  sched.start()
  print(f"scheduler started: {len(sched.enabled_task_ids)} task(s): {', '.join(sched.enabled_task_ids)}")
  last_mtime = config_path.stat().st_mtime
  try:
      while not stop.wait(5.0):
          try: mtime = config_path.stat().st_mtime
          except OSError: continue
          if mtime != last_mtime:
              last_mtime = mtime
              try:
                  sched.reload(load_config(config_path))
                  print("config reloaded")
              except ConfigError as e:
                  print(f"config reload skipped (invalid): {e}", file=sys.stderr)
  finally:
      sched.shutdown()
      state.close()
  return 0
  ```
- `main`：`serve` 分支和「无子命令」分支都调 `_serve(Path(args.config), Path(args.db))`。
- 依赖 import：`import signal`, `import threading`（cli.py 顶部）。

**测试注意**：`_serve` 会阻塞，测试要（a）注入一个已 set 的 stop event 让循环立即退出，或（b）monkeypatch `Scheduler` 为 fake 且让 `stop.wait` 立即返回 True。最简单：把 `_serve` 拆成可注入——`_serve(config_path, db_path, *, _scheduler_cls=Scheduler, _stop: threading.Event | None = None)`；测试传一个预置 `_stop`（已 set）和 fake `_scheduler_cls`。信号注册在有 `_stop` 注入时跳过（避免污染测试进程的信号处理）。

- [ ] **Step 1: 写失败测试**

```python
import threading
from pathlib import Path

from push2xteink.cli import main


class FakeSched:
    instances = []
    def __init__(self, config, state, **kw):
        self.started = self.shut = False
        self.enabled_task_ids = ["t1"]
        FakeSched.instances.append(self)
    def start(self): self.started = True
    def shutdown(self, **kw): self.shut = True
    def reload(self, cfg): pass


def test_serve_starts_and_shuts_down(tmp_path, capsys, monkeypatch):
    FakeSched.instances.clear()
    monkeypatch.setattr("push2xteink.cli.Scheduler", FakeSched)
    cfg = tmp_path / "config.yaml"
    import shutil
    shutil.copy(Path(__file__).parent / "fixtures" / "config_valid.yaml", cfg)
    stop = threading.Event(); stop.set()   # loop exits immediately
    rc = main(["--config", str(cfg), "--db", str(tmp_path / "s.db"), "serve"],
              _serve_stop=stop)
    assert rc == 0
    assert FakeSched.instances[0].started and FakeSched.instances[0].shut
    assert "scheduler started" in capsys.readouterr().out


def test_bare_invocation_defaults_to_serve(tmp_path, monkeypatch):
    FakeSched.instances.clear()
    monkeypatch.setattr("push2xteink.cli.Scheduler", FakeSched)
    cfg = tmp_path / "config.yaml"
    import shutil
    shutil.copy(Path(__file__).parent / "fixtures" / "config_valid.yaml", cfg)
    stop = threading.Event(); stop.set()
    rc = main(["--config", str(cfg), "--db", str(tmp_path / "s.db")], _serve_stop=stop)
    assert rc == 0 and FakeSched.instances[0].started


def test_serve_bad_config_returns_2(tmp_path, monkeypatch):
    monkeypatch.setattr("push2xteink.cli.Scheduler", FakeSched)
    bad = tmp_path / "bad.yaml"; bad.write_text("x: [unclosed\n", encoding="utf-8")
    stop = threading.Event(); stop.set()
    rc = main(["--config", str(bad), "--db", str(tmp_path / "s.db"), "serve"], _serve_stop=stop)
    assert rc == 2
```

> `main` 需要接一个测试专用的 `_serve_stop` kwarg（默认 None），转给 `_serve`。生产路径不传。这是最小侵入的可测形态；在 `main` 签名里加 `*, _serve_stop: threading.Event | None = None` 并只在调 `_serve` 时透传。

- [ ] **Step 2: 运行 → 失败**

- [ ] **Step 3: 实现** —— 见上方行为描述。关键点：
  - `main(argv=None, *, _serve_stop=None)`。
  - `serve` / 无子命令 → `return _serve(Path(args.config), Path(args.db), _stop=_serve_stop)`。
  - `_serve(config_path, db_path, *, _stop=None, _scheduler_cls=None)`：`_scheduler_cls or Scheduler`；`stop = _stop or threading.Event()`；**只有 `_stop is None` 时才注册信号处理**。
  - cli.py 顶部 `from .scheduler import Scheduler`（模块级——测试 monkeypatch `push2xteink.cli.Scheduler`）。

- [ ] **Step 4: 运行完整套件 → 全绿。手动冒烟**：
  ```
  PYTHONPATH=src python -m push2xteink --config tests/fixtures/config_valid.yaml --db /tmp/p4.db serve &
  sleep 2 ; kill -INT %1 ; wait
  ```
  应打印 `scheduler started: 1 task(s): brief`，收到 SIGINT 后干净退出（无 traceback）。
  **Commit** `feat: serve command (scheduler + signal handling + config watch)`

---

## Self-Review

**Spec coverage：**
- 第 9 节：启动读 config、`enabled` task 用 `CronTrigger.from_crontab` 注册（Task 1 `_register_jobs`）；job 函数 = `run_task`（Task 1 `_run`）；`reload()` 移除全部 job 重注册（Task 3）；`max_instances=1` / `coalesce=True`（Task 1 job_defaults）；`misfire_grace_time=None` 重启补跑（Task 1）。
- 第 3、10 节：`python -m push2xteink` 单进程入口——Task 4 `serve`（无子命令默认 serve）。**注意本计划只启动调度器**；P5 会把 FastAPI 挂进同一进程（在 `serve` 里或 FastAPI lifespan 启动 scheduler）。已在 spec §10 备注 serve 的现状。
- 第 13 节 APScheduler 星期 `0`=周一：本计划不归一化（校验与执行都用同一 `CronTrigger`，自洽）；P5 的「人类可读预览」需按 APScheduler 语义生成——已在 spec 记录，此处不动。
- 第 14 节 P3 备注：reload 排空顺序（Task 3，严格 `pause → drain → swap → resume → close old`）；executor 池 4（Task 1）；排空超时的 orphan 处理（Task 3）。

**Placeholder scan：** Task 1 的 `start` / `shutdown` / `reload` 桩 `raise NotImplementedError` 标注后续任务号，Task 2/3 给出完整实现。无 TODO。

**Type consistency：** `Scheduler.__init__` / `start` / `shutdown` / `reload` / `run_now` / `_run` / `_register_jobs` / `_drain` 签名在 Interfaces / 各 Task Produces / 实现 / 测试一致。`pipeline_factory: Callable[[Config, State], Pipeline]`、`run_now -> RunOutcome`（`_run` 返回 `RunOutcome | None`，`run_now` 兜底包装）。CLI `main(argv=None, *, _serve_stop=None) -> int`。

**线程安全：** 活跃计数用 `threading.Condition` 保护读写 + `wait_for` 排空；`_run` 的 `finally` 无条件减计数 + `notify_all`。`BackgroundScheduler` 自带线程池跑 job；`shutdown(wait=True)` 阻塞到 APScheduler job 完成，再 `_drain` 兜住 `run_now` 触发的活跃。`Pipeline` / `State` 跨线程共享安全（P1–P3 已保证）。reload 先 `pause` 阻止新触发、`drain` 等旧的跑完，再换 `self._pipeline`——换的瞬间无 run 在用它。

**已知延后（不在本计划，spec §14 已记）：** `ProxyConfig.url` scheme 校验（可放这里也可 P5——本计划不做，留 P5 config-load 层）；M8 短正文跳过摘要（P5）；`Task.name` max_length（P1 follow-up）。
