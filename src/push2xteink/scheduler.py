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
        self._orphans: list[Pipeline] = []

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

    def start(self) -> None:
        if self._shutdown or self._aps.running:
            return
        self._register_jobs()
        self._aps.start()

    @staticmethod
    def _safe_close(pipeline: Pipeline) -> None:
        try:
            pipeline.close()
        except Exception:  # noqa: BLE001 - close is best-effort; never mask shutdown
            logger.exception("pipeline close() raised during shutdown/reload")

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
        # Close orphans first: a raise in the current pipeline's close() must not
        # strand them (and _shutdown is already True, so a retry is a no-op).
        for orphan in self._orphans:
            self._safe_close(orphan)
        self._orphans.clear()
        self._safe_close(self._pipeline)

    def reload(self, new_config: Config) -> None:
        if self._shutdown or not self._aps.running:
            logger.warning("reload ignored: scheduler not running")
            return
        self._aps.pause()
        # pause() only suppresses NEW triggers; a job already being dispatched
        # could in principle append to _active_ids just after drain sees empty.
        # Acceptable: 1-minute cron granularity and the 5s reload poll cadence
        # dwarf the sub-ms dispatch window.
        drained = self._drain(self._drain_timeout)

        old_pipeline = self._pipeline
        self._pipeline = self._pipeline_factory(new_config, self._state)
        self._config = new_config
        self._register_jobs()
        self._aps.resume()

        if drained:
            self._safe_close(old_pipeline)
        else:
            logger.warning(
                "reload could not drain; deferring old pipeline close to shutdown"
            )
            self._orphans.append(old_pipeline)
