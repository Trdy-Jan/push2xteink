from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ._common import get_scheduler
from .app import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    sched = get_scheduler(request)
    tasks = [
        {
            "id": t.id,
            "name": t.name,
            "enabled": t.enabled,
            "schedule": t.schedule,
            "next_run_time": sched.next_run_time(t.id),
        }
        for t in sched.config.tasks
    ]
    return templates.TemplateResponse(
        request, "tasks.html", {"tasks": tasks}
    )
