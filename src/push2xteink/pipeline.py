from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

from .feeds import fetch_feed, select_new_articles
from .models import Article, Config, Feed, Task
from .state import State
from .summarize import Summarizer
from .xteink import XteinkClient


@dataclasses.dataclass
class RunOutcome:
    task_id: str
    status: str
    item_count: int = 0
    file_name: str | None = None
    record_id: str | None = None
    message: str | None = None


class Pipeline:
    def __init__(
        self,
        config: Config,
        state: State,
        *,
        summarizer: Summarizer | None,
        xteink_client: XteinkClient,
        output_dir: str | Path | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._summarizer = summarizer
        self._xteink = xteink_client
        self._output_dir = Path(output_dir) if output_dir is not None else None

    @classmethod
    def from_config(
        cls, config: Config, state: State, *, output_dir: str | Path | None = None
    ) -> "Pipeline":
        summarizer = (
            Summarizer(config.ai, proxy_url=config.proxy.url)
            if config.ai is not None
            else None
        )
        xteink = XteinkClient(config.xteink, state)
        return cls(
            config,
            state,
            summarizer=summarizer,
            xteink_client=xteink,
            output_dir=output_dir,
        )

    def close(self) -> None:
        if self._summarizer is not None:
            self._summarizer.close()
        self._xteink.close()

    def __enter__(self) -> "Pipeline":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- lookup helpers ---
    def _feed(self, feed_id: str) -> Feed:
        for f in self._config.feeds:
            if f.id == feed_id:
                return f
        raise KeyError(f"unknown feed {feed_id!r}")

    def _task(self, task_id: str) -> Task:
        for t in self._config.tasks:
            if t.id == task_id:
                return t
        raise KeyError(f"unknown task {task_id!r}")

    def _proxy_for(self, feed: Feed) -> str | None:
        return self._config.proxy.url if feed.use_proxy else None

    # --- step 1-2: fetch + dedup ---
    def _gather(
        self, task: Task, *, now: datetime, warnings: list[str]
    ) -> tuple[list[Article], dict[str, list[str]]]:
        first_run = not self._state.task_has_successful_run(task.id)
        kept: list[Article] = []
        kept_guids: dict[str, list[str]] = {}
        for feed_id in task.feeds:
            feed = self._feed(feed_id)
            result = fetch_feed(
                feed,
                proxy_url=self._proxy_for(feed),
                timeout=self._config.fetch.timeout_seconds,
            )
            if result.error:
                warnings.append(f"feed {feed_id}: {result.error}")
                continue
            new = select_new_articles(
                self._state,
                feed_id,
                result.articles,
                first_run=first_run,
                lookback_hours=task.first_run_lookback_hours,
                now=now,
            )
            if new:
                kept.extend(new)
                kept_guids[feed_id] = [a.guid for a in new]
        return kept, kept_guids

    def run_task(self, task_id: str, *, now=None) -> RunOutcome:  # Task 5
        raise NotImplementedError
