from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import sys
import threading
from pathlib import Path

from .config import ConfigError, load_config
from .pipeline import Pipeline
from .scheduler import Scheduler
from .state import State


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="push2xteink")
    p.add_argument("--config", default=os.environ.get("CONFIG_PATH", "data/config.yaml"))
    p.add_argument("--db", default=os.environ.get("DB_PATH", "data/state.db"))
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list", help="list configured tasks")
    run = sub.add_parser("run", help="run one task now")
    run.add_argument("task_id")
    sub.add_parser("serve", help="run the scheduler (blocks)")
    return p


def _serve(
    config_path: Path,
    db_path: Path,
    *,
    _stop: threading.Event | None = None,
    _scheduler_cls: type | None = None,
) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        state = State(db_path)
    except (OSError, sqlite3.Error) as exc:
        print(f"database error: {exc}", file=sys.stderr)
        return 2

    stop = _stop or threading.Event()
    if _stop is None:
        # Main-thread only; skip when a test injects its own stop event so we
        # don't clobber the test process's signal handlers.
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())

    try:
        sched = (_scheduler_cls or Scheduler)(config, state)
        sched.start()
    except Exception as exc:  # noqa: BLE001 - bad proxy.url etc. -> friendly exit 2
        print(f"scheduler init failed: {exc}", file=sys.stderr)
        state.close()
        return 2
    ids = sched.enabled_task_ids
    print(f"scheduler started: {len(ids)} task(s): {', '.join(ids)}")

    try:
        last_mtime = config_path.stat().st_mtime
    except OSError:
        last_mtime = None

    try:
        while not stop.wait(5.0):
            try:
                mtime = config_path.stat().st_mtime
            except OSError:
                continue
            if mtime == last_mtime:
                continue
            last_mtime = mtime
            try:
                sched.reload(load_config(config_path))
                print("config reloaded")
            except ConfigError as exc:
                print(f"reload skipped (invalid config): {exc}", file=sys.stderr)
    finally:
        try:
            sched.shutdown()
        finally:
            state.close()
    return 0


def main(
    argv: list[str] | None = None,
    *,
    _serve_stop: threading.Event | None = None,
) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)

    if args.cmd is None or args.cmd == "serve":
        return _serve(Path(args.config), Path(args.db), _stop=_serve_stop)

    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.cmd == "list":
        for t in config.tasks:
            print(
                f"{t.id}\t{t.name}\t{t.schedule}\t"
                f"summarize={t.summarize}\tformat={t.format}\tenabled={t.enabled}"
            )
        return 0

    # args.cmd == "run" -- the only path that touches the DB
    known = {t.id for t in config.tasks}
    if args.task_id not in known:
        print(
            f"unknown task {args.task_id!r}; known: {', '.join(sorted(known))}",
            file=sys.stderr,
        )
        return 2

    db = Path(args.db)
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        state = State(db)
    except (OSError, sqlite3.Error) as exc:
        print(f"database error: {exc}", file=sys.stderr)
        return 2

    try:
        try:
            pipe_cm = Pipeline.from_config(config, state)
        except Exception as exc:  # noqa: BLE001 - bad proxy.url etc. -> friendly exit 2
            print(f"pipeline init failed: {exc}", file=sys.stderr)
            return 2
        with pipe_cm as pipe:
            outcome = pipe.run_task(args.task_id)
        print(
            f"{outcome.status} items={outcome.item_count} "
            f"file={outcome.file_name or ''} record={outcome.record_id or ''}"
        )
        if outcome.message:
            print(outcome.message, file=sys.stderr)
        return 0 if outcome.status in ("success", "skipped") else 1
    finally:
        state.close()
