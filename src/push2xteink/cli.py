from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

import uvicorn

from .config import ConfigError, load_config
from .pipeline import Pipeline
from .state import State


# Spec §10: a first `docker compose up` with no config.yaml must leave the user
# a filled-in-able template, not a crashloop on "config file not found".
# Mirrors the spec §4 example. It loads as-is (the container comes up serving it);
# task runs just fail at the xteink upload step until real credentials are filled in.
_SAMPLE_CONFIG = """\
# push2xteink 配置文件
# 修改后保存即可热更新（无需重启容器）；Web 界面读写的也是这个文件。

xteink:
  api_base: https://api-prod.xteink.cn
  username: "<手机号>"           # 必填
  password: "<密码>"             # 必填

proxy:
  # 留空表示不走代理；支持 http:// https:// socks5://
  url:

# AI 摘要（可选）。不需要摘要就整段删掉，并把 tasks[].summarize 设为 false。
# ai:
#   use_proxy: false             # AI 请求是否走上面的 proxy.url
#   primary:
#     base_url: https://api.example.com/v1
#     api_key: "<key>"
#     model: gpt-4o-mini
#   fallback:                    # 可选；primary 失败时启用
#     base_url: https://api.backup.com/v1
#     api_key: "<key>"
#     model: claude-3-5-haiku
#   timeout_seconds: 60
#   max_retries: 2
#   qps: 1

fetch:
  timeout_seconds: 20
  concurrency: 5

feeds:
  - id: hn
    url: https://news.ycombinator.com/rss
    full_text: true              # 默认 true；抓全文失败时回退到 RSS 内容
    use_proxy: false

tasks:
  - id: morning-brief
    name: 早报
    feeds: [hn]
    schedule: "0 7 * * *"        # 标准 5 段 cron，按容器时区（TZ）解释
    summarize: false             # 需要 AI 摘要时改 true，并填好上面的 ai 段
    format: epub                 # epub | txt
    enabled: true
    first_run_lookback_hours: 48 # 仅任务从未成功执行过时生效
"""


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="push2xteink")
    p.add_argument("--config", default=os.environ.get("CONFIG_PATH", "data/config.yaml"))
    p.add_argument("--db", default=os.environ.get("DB_PATH", "data/state.db"))
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list", help="list configured tasks")
    run = sub.add_parser("run", help="run one task now")
    run.add_argument("task_id")
    sub.add_parser("serve", help="run the web app + scheduler (blocks)")
    return p


def _serve(config_path: Path, db_path: Path, *, _run=None) -> int:
    # Headless containers have nothing else wiring up logging: without this the
    # scheduler's logger.* calls reach stderr only via lastResort (no timestamp)
    # and APScheduler's INFO job lines vanish entirely. basicConfig is idempotent.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Only `serve` bootstraps — it's the container entrypoint. `list` / `run`
    # keep the plain "not found" error.
    if not config_path.exists():
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(_SAMPLE_CONFIG, encoding="utf-8")
        except OSError as exc:
            print(f"cannot create config file {config_path}: {exc}", file=sys.stderr)
            return 2
        print(
            f"wrote a sample config to {config_path} — fill it in and restart",
            file=sys.stderr,
        )
        return 2

    try:
        load_config(config_path)  # fail-fast; the app's lifespan loads it again
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Imported here so `list` / `run` don't pay the fastapi import cost.
    from .web.app import create_app

    app = create_app(config_path, db_path)
    run = _run or uvicorn.run
    run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
    return 0


def main(argv: list[str] | None = None, *, _run=None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)

    if args.cmd is None or args.cmd == "serve":
        return _serve(Path(args.config), Path(args.db), _run=_run)

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
