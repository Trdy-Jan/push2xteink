from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from apscheduler.executors.pool import ThreadPoolExecutor as APSThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .config import ConfigError, load_config
from .models import Config
from .pipeline import Pipeline, RunOutcome
from .state import State

logger = logging.getLogger(__name__)

_EXECUTOR_WORKERS = 4
_SEEN_RETENTION_DAYS = 90
_PRUNE_JOB_ID = "__prune__"


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
        # Serializes reload()/shutdown() against each other: in P5 the mtime
        # watcher and a FastAPI request thread can both call reload().
        self._reload_lock = threading.RLock()
        self._shutdown = False
        self._orphans: list[Pipeline] = []
        self._config_token: tuple[int, int] | None = None
        self._last_bad_token: tuple[int, int] | None = None

    # --- job body ---
    def _run(self, task_id: str) -> RunOutcome | None:
        with self._cond:
            if self._shutdown:
                return RunOutcome(
                    task_id, "failed", 0, None, None, "scheduler is shut down"
                )
            if task_id in self._active_ids:
                # Spec §9: a task never runs concurrently with itself.
                # APScheduler's max_instances=1 only covers repeats of the SAME
                # job id, so a cron fire of `t1` and the `manual:t1` job from
                # submit() would otherwise both execute.
                logger.info("skipping %s: already running", task_id)
                return RunOutcome(
                    task_id, "skipped", 0, None, None, "task already running"
                )
            self._active_ids.append(task_id)
            # Capture the pipeline reference atomically with registration so a
            # concurrent reload() either waits for this run in its drain (we
            # registered first) or swaps in a new pipeline that we never see.
            pipeline = self._pipeline
        try:
            return pipeline.run_task(task_id)
        except Exception:  # noqa: BLE001 - run_task shouldn't raise; never leak
            logger.exception("run_task(%s) raised unexpectedly", task_id)
            return None
        finally:
            with self._cond:
                self._active_ids.remove(task_id)
                self._cond.notify_all()

    def run_now(self, task_id: str) -> RunOutcome:
        # The shutdown/already-running guards live in _run so that EVERY dispatch
        # path (cron fire, submit(), run_now) shares them atomically.
        outcome = self._run(task_id)
        if outcome is None:
            return RunOutcome(task_id, "failed", 0, None, None, "scheduler internal error")
        return outcome

    def submit(self, task_id: str) -> None:
        """Dispatch a one-off manual run of ``task_id`` via the executor.

        The cron job (if any) for the same task is untouched; the manual job
        uses a distinct ``manual:`` id. Non-concurrency is enforced in ``_run``.
        """
        with self._cond:
            if self._shutdown:
                logger.info("submit(%s) ignored: scheduler is shut down", task_id)
                return
        self._aps.add_job(
            self._run,
            DateTrigger(run_date=datetime.now(timezone.utc)),
            args=[task_id],
            id=f"manual:{task_id}",
            replace_existing=True,
        )

    # --- config reload ---
    @staticmethod
    def _token(path) -> tuple[int, int] | None:
        try:
            st = Path(path).stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def prime_config_token(self, path: Path) -> None:
        """Record the current config file token so a following unchanged
        ``maybe_reload`` is a no-op. Called by serve/lifespan after ``start()``."""
        self._config_token = self._token(path)

    def invalidate_config_token(self) -> None:
        """Forget the recorded token so the next ``maybe_reload`` re-reads the
        file. Used when a write succeeded but the swap did not."""
        self._config_token = None

    def maybe_reload(self, config_path) -> bool:
        """Reload iff the config file changed since the last recorded token.

        Returns True only when a reload actually happened. A file that is
        missing, unchanged, or invalid returns False and leaves the scheduler
        running on its current config.
        """
        token = self._token(config_path)
        if token is None or token == self._config_token:
            return False
        try:
            new_config = load_config(Path(config_path))
        except ConfigError as exc:
            if token != self._last_bad_token:
                logger.warning("config reload skipped (invalid): %s", exc)
                self._last_bad_token = token
            return False
        if not self.reload(new_config):
            # Valid file, but the swap aborted (e.g. pipeline build failed).
            # Don't advance _config_token, so a fix to the file is retried.
            if token != self._last_bad_token:
                logger.warning("config reload aborted (pipeline build failed)")
                self._last_bad_token = token
            return False
        self._config_token = token
        return True

    # --- jobs ---
    def _prune(self) -> None:
        try:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=_SEEN_RETENTION_DAYS)
            ).isoformat()
            n = self._state.prune_seen_items(cutoff)
            logger.info("pruned %d old seen_items rows", n)
        except Exception:  # noqa: BLE001 - a sqlite error must not kill the worker
            logger.exception("prune_seen_items failed")

    def _ensure_prune_job(self) -> None:
        self._aps.add_job(
            self._prune,
            CronTrigger(hour=3, minute=17),
            id=_PRUNE_JOB_ID,
            replace_existing=True,
        )

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
        # remove_all_jobs() above wipes __prune__ too; re-add so reload() keeps it.
        self._ensure_prune_job()

    @property
    def enabled_task_ids(self) -> list[str]:
        return [t.id for t in self._config.tasks if t.enabled]

    @property
    def active_count(self) -> int:
        with self._cond:
            return len(self._active_ids)

    @property
    def config(self) -> Config:
        """The live config (reflects the most recent successful reload)."""
        return self._config

    def next_run_time(self, task_id: str) -> datetime | None:
        job = self._aps.get_job(task_id)
        return job.next_run_time if job else None

    def start(self) -> None:
        if self._shutdown or self._aps.running:
            return
        self._register_jobs()
        self._aps.start()
        for job in self._aps.get_jobs():
            logger.info("task %s scheduled, next run %s", job.id, job.next_run_time)

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

    def shutdown(self) -> None:
        with self._reload_lock:
            if self._shutdown:
                return
            self._shutdown = True
            if self._aps.running:
                # wait=False + one bounded _drain: drain_timeout covers cron jobs
                # too, instead of aps.shutdown(wait=True) blocking unbounded on a
                # wedged run.
                self._aps.shutdown(wait=False)
            self._drain(self._drain_timeout)
            # Close orphans first: a raise in the current pipeline's close() must
            # not strand them (and _shutdown is already True, so a retry is a
            # no-op).
            for orphan in self._orphans:
                self._safe_close(orphan)
            self._orphans.clear()
            self._safe_close(self._pipeline)

    def reload(self, new_config: Config) -> bool:
        """Swap in ``new_config``. Returns True on a successful swap, False if
        the scheduler is not running or the new pipeline could not be built."""
        with self._reload_lock:
            if self._shutdown or not self._aps.running:
                logger.warning("reload ignored: scheduler not running")
                return False
            self._aps.pause()
            # pause() only suppresses NEW triggers. A job already being dispatched
            # by APScheduler could still call _run; it registers in _active_ids
            # under _cond before reading self._pipeline, so the drain below either
            # waits for it (registered first) or it reads the post-swap pipeline.
            drained = False
            with self._cond:
                drained = self._cond.wait_for(
                    lambda: not self._active_ids, timeout=self._drain_timeout
                )
                old_pipeline = self._pipeline
                try:
                    new_pipeline = self._pipeline_factory(new_config, self._state)
                except Exception:  # noqa: BLE001 - a bad config must not brick us
                    logger.exception(
                        "reload aborted: could not build pipeline for new config; "
                        "keeping current"
                    )
                    self._aps.resume()
                    return False
                # Swap under the same lock the drain waited on, so no run can
                # capture the old pipeline after this point.
                self._pipeline = new_pipeline
                self._config = new_config

            if not drained:
                logger.warning("drain timed out during reload with active run(s)")

            self._register_jobs()
            self._aps.resume()

            if drained:
                self._safe_close(old_pipeline)
            else:
                logger.warning(
                    "reload could not drain; deferring old pipeline close to shutdown"
                )
                self._orphans.append(old_pipeline)
            return True
