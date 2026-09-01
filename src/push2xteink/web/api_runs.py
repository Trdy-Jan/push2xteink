from __future__ import annotations

from datetime import datetime, timezone

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, HTTPException, Query, Request

from ._common import get_db, get_scheduler

router = APIRouter()

_SCAN_LIMIT = 5000


@router.get("/api/runs")
def list_runs(
    request: Request,
    limit: int = Query(50, gt=0, le=_SCAN_LIMIT),
    task_id: str | None = None,
) -> list[dict]:
    rows = [dict(r) for r in get_db(request).recent_runs(_SCAN_LIMIT)]
    if task_id is not None:
        rows = [r for r in rows if r["task_id"] == task_id]
    return rows[:limit]


@router.post("/api/runs/{run_id}/rerun", status_code=202)
def rerun(request: Request, run_id: int) -> dict:
    sched = get_scheduler(request)
    row = next(
        (r for r in get_db(request).recent_runs(_SCAN_LIMIT) if r["id"] == run_id),
        None,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    task_id = row["task_id"]
    sched.submit(task_id)
    return {"submitted": task_id}


def _cron_preview(expr: str) -> dict:
    try:
        trig = CronTrigger.from_crontab(expr)
    except (ValueError, TypeError) as exc:
        return {"valid": False, "next": [], "error": str(exc)}
    now = datetime.now(timezone.utc)
    out: list[str] = []
    prev: datetime | None = None
    for _ in range(3):
        nxt = trig.get_next_fire_time(prev, now)
        if nxt is None:
            break
        out.append(nxt.isoformat())
        prev = nxt
    return {"valid": True, "next": out, "error": None}


@router.get("/api/cron/preview")
def cron_preview(expr: str) -> dict:
    return _cron_preview(expr)
