from __future__ import annotations

import uuid

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from ._common import apply_config_change, current_config, get_db, get_scheduler
from .app import templates

router = APIRouter()

_BADGE = {
    "success": ("成功", "badge-success"),
    "failed": ("失败", "badge-failed"),
    "skipped": ("跳过", "badge-skipped"),
    "running": ("运行中", "badge-running"),
}


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
