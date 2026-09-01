from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from .api_feeds import test_feed as _api_test_feed
from .api_runs import _cron_preview
from .api_settings import (
    _deep_update_keep_masked,
    _SECTIONS,
    _settings_view,
    test_ai as _api_test_ai,
    test_proxy as _api_test_proxy,
    test_xteink as _api_test_xteink,
)
from ._common import (
    apply_config_change,
    current_config,
    get_db,
    get_scheduler,
)
from .app import templates

router = APIRouter()

_BADGE = {
    "success": ("成功", "badge-success"),
    "failed": ("失败", "badge-failed"),
    "skipped": ("跳过", "badge-skipped"),
    "running": ("运行中", "badge-running"),
}

# Scaffold so the AI settings <fieldset> is always renderable, even before
# [ai] exists in config.yaml. Empty strings (not "********") so the user can
# actually fill them in.
_AI_SCAFFOLD = {
    "primary": {"base_url": "", "api_key": "", "model": ""},
    "fallback": None,
    "prompt": "",
    "timeout_seconds": 60,
    "max_retries": 2,
    "qps": 1.0,
    "use_proxy": False,
}


def _fmt_ts(value: object) -> str:
    """Render an ISO timestamp (or datetime) as 'YYYY-MM-DD HH:MM' in UTC."""
    if value in (None, ""):
        return "-"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


templates.env.filters["ts"] = _fmt_ts


def _gen_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------- #
# tasks
# --------------------------------------------------------------------------- #


def _task_row_view(request: Request, task) -> dict:
    sched = get_scheduler(request)
    db = get_db(request)
    nrt = sched.next_run_time(task.id)
    last = db.last_run_for_task(task.id)
    status = last["status"] if last is not None else None
    text, cls = _BADGE.get(status, ("未运行", "badge"))
    return {
        "id": task.id,
        "name": task.name,
        "enabled": task.enabled,
        "schedule": task.schedule,
        "next_run_time": nrt.isoformat() if nrt else None,
        "badge_text": text,
        "badge_class": cls,
    }


def _task_table(request: Request, *, oob: bool) -> HTMLResponse:
    tasks = [_task_row_view(request, t) for t in current_config(request).tasks]
    return templates.TemplateResponse(
        request, "_task_table.html", {"tasks": tasks, "oob": oob}
    )


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    tasks = [_task_row_view(request, t) for t in current_config(request).tasks]
    return templates.TemplateResponse(request, "tasks.html", {"tasks": tasks})


def _form_ctx(request: Request, task: dict | None, action: str, error: str | None):
    return {
        "task": task,
        "feeds": [f.model_dump(mode="json") for f in current_config(request).feeds],
        "action": action,
        "error": error,
    }


@router.get("/tasks/new", response_class=HTMLResponse)
def task_new(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "task_form.html", _form_ctx(request, None, "/tasks", None)
    )


@router.get("/tasks/{task_id}/edit", response_class=HTMLResponse)
def task_edit(request: Request, task_id: str) -> HTMLResponse:
    task = next(
        (t for t in current_config(request).tasks if t.id == task_id), None
    )
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")
    return templates.TemplateResponse(
        request,
        "task_form.html",
        _form_ctx(request, task.model_dump(mode="json"), f"/tasks/{task_id}", None),
    )


def _save_task(
    request: Request,
    task_id: str | None,
    *,
    name: str,
    feeds: list[str],
    schedule: str,
    summarize: bool,
    fmt: str,
    lookback: int,
) -> HTMLResponse:
    payload = {
        "name": name,
        "feeds": feeds,
        "schedule": schedule,
        "summarize": summarize,
        "format": fmt,
        "first_run_lookback_hours": lookback,
    }

    def mutate(raw: dict) -> None:
        if task_id is not None:
            for t in raw["tasks"]:
                if t["id"] == task_id:
                    t.update(payload)
                    return
            raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")
        raw["tasks"].append({"id": _gen_id("t"), "enabled": True, **payload})

    try:
        apply_config_change(request, mutate)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise
        action = f"/tasks/{task_id}" if task_id else "/tasks"
        ctx = _form_ctx(request, {"id": task_id, **payload}, action, str(exc.detail))
        return templates.TemplateResponse(
            request, "task_form.html", ctx, status_code=200
        )
    return _task_table(request, oob=True)


@router.post("/tasks", response_class=HTMLResponse)
def task_create(
    request: Request,
    name: str = Form(...),
    feeds: list[str] = Form(default=[]),
    schedule: str = Form(...),
    summarize: bool = Form(False),
    format: str = Form("epub"),
    first_run_lookback_hours: int = Form(48),
) -> HTMLResponse:
    return _save_task(
        request,
        None,
        name=name,
        feeds=feeds,
        schedule=schedule,
        summarize=summarize,
        fmt=format,
        lookback=first_run_lookback_hours,
    )


@router.post("/tasks/{task_id}", response_class=HTMLResponse)
def task_update(
    request: Request,
    task_id: str,
    name: str = Form(...),
    feeds: list[str] = Form(default=[]),
    schedule: str = Form(...),
    summarize: bool = Form(False),
    format: str = Form("epub"),
    first_run_lookback_hours: int = Form(48),
) -> HTMLResponse:
    return _save_task(
        request,
        task_id,
        name=name,
        feeds=feeds,
        schedule=schedule,
        summarize=summarize,
        fmt=format,
        lookback=first_run_lookback_hours,
    )


@router.post("/tasks/{task_id}/toggle", response_class=HTMLResponse)
def task_toggle(request: Request, task_id: str) -> HTMLResponse:
    tasks = {t.id: t for t in current_config(request).tasks}
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")
    new_value = not tasks[task_id].enabled

    def mutate(raw: dict) -> None:
        for t in raw["tasks"]:
            if t["id"] == task_id:
                t["enabled"] = new_value

    apply_config_change(request, mutate)
    task = next(t for t in current_config(request).tasks if t.id == task_id)
    return templates.TemplateResponse(
        request, "_task_row.html", {"t": _task_row_view(request, task)}
    )


@router.post("/tasks/{task_id}/run", response_class=HTMLResponse)
def task_run(request: Request, task_id: str) -> HTMLResponse:
    sched = get_scheduler(request)
    if task_id not in {t.id for t in sched.config.tasks}:
        raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")
    sched.submit(task_id)
    task = next(t for t in sched.config.tasks if t.id == task_id)
    return templates.TemplateResponse(
        request,
        "_task_row.html",
        {"t": _task_row_view(request, task), "triggered": True},
    )


@router.delete("/tasks/{task_id}", response_class=HTMLResponse)
def task_delete(request: Request, task_id: str) -> HTMLResponse:
    if task_id not in {t.id for t in current_config(request).tasks}:
        raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")

    def mutate(raw: dict) -> None:
        raw["tasks"] = [t for t in raw["tasks"] if t["id"] != task_id]

    apply_config_change(request, mutate)
    return HTMLResponse("")


# --------------------------------------------------------------------------- #
# feeds
# --------------------------------------------------------------------------- #


def _feeds_table(request: Request, *, error: str | None = None) -> HTMLResponse:
    feeds = [f.model_dump(mode="json") for f in current_config(request).feeds]
    return templates.TemplateResponse(
        request, "_feed_table.html", {"feeds": feeds, "error": error}
    )


def _feeds_apply(request: Request, mutate) -> HTMLResponse:
    try:
        apply_config_change(request, mutate)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise
        return _feeds_table(request, error=str(exc.detail))
    return _feeds_table(request)


@router.get("/feeds", response_class=HTMLResponse)
def feeds_page(request: Request) -> HTMLResponse:
    feeds = [f.model_dump(mode="json") for f in current_config(request).feeds]
    return templates.TemplateResponse(request, "feeds.html", {"feeds": feeds})


@router.post("/feeds", response_class=HTMLResponse)
def feed_create(
    request: Request,
    url: str = Form(...),
    full_text: bool = Form(True),
    use_proxy: bool = Form(False),
) -> HTMLResponse:
    def mutate(raw: dict) -> None:
        raw["feeds"].append(
            {
                "id": _gen_id("f"),
                "url": url,
                "full_text": full_text,
                "use_proxy": use_proxy,
            }
        )

    return _feeds_apply(request, mutate)


@router.post("/feeds/{feed_id}", response_class=HTMLResponse)
def feed_update(
    request: Request,
    feed_id: str,
    url: str = Form(...),
    full_text: bool = Form(False),
    use_proxy: bool = Form(False),
) -> HTMLResponse:
    if feed_id not in {f.id for f in current_config(request).feeds}:
        raise HTTPException(status_code=404, detail=f"feed {feed_id!r} not found")

    def mutate(raw: dict) -> None:
        for f in raw["feeds"]:
            if f["id"] == feed_id:
                f.update({"url": url, "full_text": full_text, "use_proxy": use_proxy})

    return _feeds_apply(request, mutate)


@router.delete("/feeds/{feed_id}", response_class=HTMLResponse)
def feed_delete(request: Request, feed_id: str) -> HTMLResponse:
    cfg = current_config(request)
    if feed_id not in {f.id for f in cfg.feeds}:
        raise HTTPException(status_code=404, detail=f"feed {feed_id!r} not found")
    refs = [t.id for t in cfg.tasks if feed_id in t.feeds]
    if refs:
        return _feeds_table(
            request, error=f"源 {feed_id!r} 被任务 {refs} 引用，无法删除（409）"
        )

    def mutate(raw: dict) -> None:
        raw["feeds"] = [f for f in raw["feeds"] if f["id"] != feed_id]

    return _feeds_apply(request, mutate)


# --------------------------------------------------------------------------- #
# runs
# --------------------------------------------------------------------------- #


@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request) -> HTMLResponse:
    db = get_db(request)
    names = {t.id: t.name for t in current_config(request).tasks}
    rows = []
    for r in db.recent_runs(100):
        d = dict(r)
        d["task_name"] = names.get(d["task_id"], d["task_id"])
        text, cls = _BADGE.get(d["status"], ("未运行", "badge"))
        d["badge_text"], d["badge_class"] = text, cls
        rows.append(d)
    has_running = any(d["status"] == "running" for d in rows)
    return templates.TemplateResponse(
        request, "runs.html", {"rows": rows, "has_running": has_running}
    )


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #


def _settings_view_scaffolded(request: Request) -> dict:
    view = _settings_view(current_config(request))
    if view.get("ai") is None:
        view["ai"] = {**_AI_SCAFFOLD, "primary": dict(_AI_SCAFFOLD["primary"])}
    return view


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"s": _settings_view_scaffolded(request), "banner": None},
    )


def _set_nested(root: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    d = root
    for p in parts[:-1]:
        nxt = d.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            d[p] = nxt
        d = nxt
    d[parts[-1]] = value


@router.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request) -> HTMLResponse:
    form = await request.form()
    body: dict = {}
    for key, val in form.multi_items():
        section = key.split(".", 1)[0]
        if section not in _SECTIONS:
            continue
        # Empty inputs don't overwrite existing values (mirrors the "********"
        # keep-behaviour) — except proxy.url, where clearing it is meaningful.
        if val == "" and key != "proxy.url":
            continue
        _set_nested(body, key, None if val == "" else val)

    # The hidden ai.use_proxy field rides along on every save. Only treat the
    # AI section as "being edited" when at least one primary field is filled,
    # so a fresh install can save xteink/proxy/fetch without a spurious
    # "ai.primary required" 400.
    ai = body.get("ai")
    if isinstance(ai, dict):
        prim = ai.get("primary") or {}
        if not any(prim.get(k) for k in ("base_url", "api_key", "model")):
            body.pop("ai")

    def mutate(raw: dict) -> None:
        for section in _SECTIONS:
            incoming = body.get(section)
            if not isinstance(incoming, dict):
                continue
            if not isinstance(raw.get(section), dict):
                raw[section] = {}
            _deep_update_keep_masked(raw[section], incoming)

    banner, is_error = "已保存", False
    try:
        apply_config_change(request, mutate)
    except HTTPException as exc:
        banner, is_error = str(exc.detail), True
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "s": _settings_view_scaffolded(request),
            "banner": banner,
            "is_error": is_error,
        },
        status_code=200,
    )


# --------------------------------------------------------------------------- #
# /ui/* — HTML-fragment wrappers around the tested JSON /api/* probes
# --------------------------------------------------------------------------- #


@router.get("/ui/cron-preview", response_class=HTMLResponse)
def ui_cron_preview(request: Request, expr: str = "") -> HTMLResponse:
    res = _cron_preview(expr)
    return templates.TemplateResponse(
        request,
        "_cron_preview.html",
        {"next_runs": res["next"], "error": None if res["valid"] else res["error"]},
    )


@router.post("/ui/feeds/{feed_id}/test", response_class=HTMLResponse)
def ui_feed_test(request: Request, feed_id: str) -> HTMLResponse:
    res = _api_test_feed(request, feed_id)
    return templates.TemplateResponse(
        request,
        "_feed_test.html",
        {"entries": res["entries"], "error": res["error"]},
    )


@router.post("/ui/test/{target}", response_class=HTMLResponse)
def ui_probe(request: Request, target: str) -> HTMLResponse:
    if target == "ai":
        res = _api_test_ai(request)
        return templates.TemplateResponse(
            request,
            "_ai_test.html",
            {"primary": res["primary"], "fallback": res["fallback"]},
        )
    if target == "xteink":
        res = _api_test_xteink(request)
    elif target == "proxy":
        res = _api_test_proxy(request)
    else:
        raise HTTPException(status_code=404, detail=f"unknown probe {target!r}")
    return templates.TemplateResponse(
        request, "_probe_result.html", {"ok": res["ok"], "error": res.get("error")}
    )


@router.post("/ui/runs/{run_id}/rerun", response_class=HTMLResponse)
def ui_rerun(request: Request, run_id: int) -> HTMLResponse:
    row = get_db(request).get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    get_scheduler(request).submit(row["task_id"])
    return HTMLResponse('<span class="hint">已触发重跑</span>')
