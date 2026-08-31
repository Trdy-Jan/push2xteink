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

    def start(self) -> None:  # Task 2
        raise NotImplementedError

    def shutdown(self, *, wait: bool = True) -> None:  # Task 2
        raise NotImplementedError

    def reload(self, new_config: Config) -> None:  # Task 3
        raise NotImplementedError
