from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request, Response

from ..models import Task
from ._common import apply_config_change, current_config, get_db, get_scheduler

router = APIRouter()


def _task_view(task: Task, sched, db) -> dict:
    d = task.model_dump(mode="json")
    nrt = sched.next_run_time(task.id)
    d["next_run_time"] = nrt.isoformat() if nrt else None
    row = db.last_run_for_task(task.id)
    d["last_run"] = (
        {
            "status": row["status"],
            "started_at": row["started_at"],
            "item_count": row["item_count"],
            "file_name": row["file_name"],
        }
        if row is not None
        else None
    )
    return d


@router.get("/api/tasks")
def list_tasks(request: Request) -> list[dict]:
    sched = get_scheduler(request)
    db = get_db(request)
    return [_task_view(t, sched, db) for t in sched.config.tasks]


@router.post("/api/tasks", status_code=201)
def create_task(request: Request, task: Task) -> dict:
    def mutate(raw: dict) -> None:
        raw["tasks"].append(task.model_dump(mode="json"))

    cfg = apply_config_change(request, mutate)
    created = next(t for t in cfg.tasks if t.id == task.id)
    return _task_view(created, get_scheduler(request), get_db(request))


@router.put("/api/tasks/{task_id}")
def update_task(
    request: Request, task_id: str, body: dict = Body(...)
) -> dict:
    if task_id not in {t.id for t in current_config(request).tasks}:
        raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")

    def mutate(raw: dict) -> None:
        for t in raw["tasks"]:
            if t["id"] == task_id:
                t.update({k: v for k, v in body.items() if k != "id"})

    cfg = apply_config_change(request, mutate)
    updated = next(t for t in cfg.tasks if t.id == task_id)
    return _task_view(updated, get_scheduler(request), get_db(request))


@router.delete("/api/tasks/{task_id}", status_code=204)
def delete_task(request: Request, task_id: str) -> Response:
    if task_id not in {t.id for t in current_config(request).tasks}:
        raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")

    def mutate(raw: dict) -> None:
        raw["tasks"] = [t for t in raw["tasks"] if t["id"] != task_id]

    apply_config_change(request, mutate)
    return Response(status_code=204)


@router.post("/api/tasks/{task_id}/toggle")
def toggle_task(request: Request, task_id: str) -> dict:
    tasks = {t.id: t for t in current_config(request).tasks}
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")
    new_value = not tasks[task_id].enabled

    def mutate(raw: dict) -> None:
        for t in raw["tasks"]:
            if t["id"] == task_id:
                t["enabled"] = new_value

    apply_config_change(request, mutate)
    return {"enabled": new_value}


@router.post("/api/tasks/{task_id}/run", status_code=202)
def run_task(request: Request, task_id: str) -> dict:
    sched = get_scheduler(request)
    if task_id not in {t.id for t in sched.config.tasks}:
        raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")
    sched.submit(task_id)
    return {"submitted": task_id}
