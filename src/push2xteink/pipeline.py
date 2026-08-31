from __future__ import annotations

import dataclasses
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .builders import BuildError, build_epub, build_txt, html_to_text
from .extract import apply_full_text
from .feeds import fetch_feed, select_new_articles
from .models import Article, Config, Feed, Task
from .state import State
from .summarize import SummarizeError, Summarizer
from .xteink import XteinkClient, XteinkUploadError


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
        # With ``output_dir`` set the caller owns that directory's lifecycle
        # (Pipeline never deletes it) and must not run the same task concurrently
        # into it -- two runs would race on the built file name.
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

    # --- step 3-4: full-text + summary ---
    def _prepare(
        self, task: Task, articles: list[Article], *, warnings: list[str]
    ) -> list[Article]:
        def do_extract(article: Article) -> Article:
            feed = self._feed(article.feed_id)
            return apply_full_text(
                article,
                enabled=feed.full_text,
                proxy_url=self._proxy_for(feed),
                timeout=self._config.fetch.timeout_seconds,
            )

        workers = max(1, self._config.fetch.concurrency)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            articles = list(pool.map(do_extract, articles))

        if task.summarize:
            if self._summarizer is None:
                warnings.append("summarize requested but no AI summarizer configured")
            else:
                for article in articles:
                    try:
                        # In-place mutation is deliberate and safe: a fresh
                        # Article is fetched per run, nothing else aliases it,
                        # and Article is not frozen / no validate_assignment.
                        article.summary = self._summarizer.summarize(
                            html_to_text(article.content_html)
                        )
                    except SummarizeError as exc:
                        warnings.append(f"summary failed for {article.link}: {exc}")
        return articles

    # --- step 5: build file ---
    def _same_day_success(self, task_id: str, now: datetime) -> bool:
        return self._state.has_success_on_day(task_id, now.strftime("%Y-%m-%d"))

    def _build(
        self, task: Task, articles: list[Article], *, now: datetime, out_dir: Path
    ) -> Path:
        title = f"{task.name}_{now:%Y%m%d}"
        if self._same_day_success(task.id, now):
            title = f"{title}_{now:%H%M%S}"
        if task.format == "epub":
            return build_epub(title, articles, out_dir=out_dir)
        return build_txt(title, articles, out_dir=out_dir)

    # --- orchestration: spec §6 steps 0->7 ---
    @staticmethod
    def _join(warnings: list[str], extra: str | None) -> str | None:
        parts = [w for w in warnings if w]
        if extra:
            parts.append(extra)
        return "\n".join(parts) if parts else None

    def _finish(self, run_id: int, **kw: object) -> None:
        try:
            self._state.finish_run(run_id, **kw)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - run row is best-effort; never mask the outcome
            pass

    def run_task(self, task_id: str, *, now: datetime | None = None) -> RunOutcome:
        """Run one task end to end. Never raises: every failure is captured in
        the returned RunOutcome (and, best-effort, the runs row). With
        ``output_dir`` set the caller owns cleanup and must not run the same
        task concurrently into that directory."""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)  # store/compare dates in UTC

        try:
            task = self._task(task_id)
        except KeyError:
            return RunOutcome(task_id, "failed", message=f"unknown task {task_id!r}")

        try:
            run_id = self._state.start_run(task_id, now=now)
        except Exception as exc:  # noqa: BLE001 - a DB failure here yields no runs row
            return RunOutcome(task_id, "failed", message=f"could not start run: {exc!r}")

        warnings: list[str] = []
        try:
            if self._output_dir is not None:
                work_dir = self._output_dir
                work_dir.mkdir(parents=True, exist_ok=True)
                own_dir = False
            else:
                work_dir = Path(tempfile.mkdtemp(prefix="p2x-"))
                own_dir = True
        except OSError as exc:
            msg = f"could not create work dir: {exc!r}"
            self._finish(run_id, status="failed", message=msg, now=now)
            return RunOutcome(task_id, "failed", message=msg)

        articles: list[Article] = []
        built_name: str | None = None
        try:
            articles, kept_guids = self._gather(task, now=now, warnings=warnings)
            if not articles:
                msg = self._join(warnings, "no new items")
                self._finish(
                    run_id, status="skipped", item_count=0, message=msg, now=now
                )
                return RunOutcome(task_id, "skipped", 0, message=msg)

            articles = self._prepare(task, articles, warnings=warnings)
            path = self._build(task, articles, now=now, out_dir=work_dir)
            built_name = path.name
            record_id = self._xteink.push_file(path, path.name)

            # A post-upload DB failure below leaves guids un-marked, so the same
            # items re-push next run -- the safe direction (dup on reader, no loss).
            for feed_id, guids in kept_guids.items():
                self._state.mark_pushed(feed_id, guids, now=now)

            msg = self._join(warnings, None)
            self._finish(
                run_id, status="success", item_count=len(articles),
                file_name=path.name, message=msg, now=now,
            )
            return RunOutcome(
                task_id, "success", len(articles), file_name=path.name,
                record_id=record_id, message=msg,
            )
        except (BuildError, XteinkUploadError) as exc:
            msg = self._join(warnings, f"{type(exc).__name__}: {exc}")
            self._finish(run_id, status="failed", item_count=len(articles) or None,
                         file_name=built_name, message=msg, now=now)
            return RunOutcome(task_id, "failed", message=msg)
        except Exception as exc:  # noqa: BLE001 - never let a run crash the scheduler
            msg = self._join(warnings, f"unexpected error: {exc!r}")
            self._finish(run_id, status="failed", item_count=len(articles) or None,
                         file_name=built_name, message=msg, now=now)
            return RunOutcome(task_id, "failed", message=msg)
        finally:
            if own_dir:
                shutil.rmtree(work_dir, ignore_errors=True)
