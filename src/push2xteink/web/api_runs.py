from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, HTTPException, Query, Request

from ._common import get_db, get_scheduler

router = APIRouter()


@router.get("/api/runs")
def list_runs(
    request: Request,
    limit: int = Query(50, gt=0, le=1000),
    task_id: str | None = None,
) -> list[dict]:
    db = get_db(request)
    if task_id is None:
        return [dict(r) for r in db.recent_runs(limit)]
    # Filter to one task: scan a wider window, then cap at limit.
    rows = [dict(r) for r in db.recent_runs(max(limit * 20, 200))]
    return [r for r in rows if r["task_id"] == task_id][:limit]


@router.post("/api/runs/{run_id}/rerun", status_code=202)
def rerun(request: Request, run_id: int) -> dict:
    row = get_db(request).get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    task_id = row["task_id"]
    get_scheduler(request).submit(task_id)
    return {"submitted": task_id}


def _cron_preview(expr: str) -> dict:
    try:
        trig = CronTrigger.from_crontab(expr)
    except (ValueError, TypeError) as exc:
        return {"valid": False, "next": [], "error": str(exc)}
    now = datetime.now(timezone.utc)
    out: list[str] = []
    prev: datetime | None = None
    cursor = now
    for _ in range(3):
        nxt = trig.get_next_fire_time(prev, cursor)
        if nxt is None:
            break
        out.append(nxt.isoformat())
        prev = nxt
        cursor = nxt + timedelta(seconds=1)
    return {"valid": True, "next": out, "error": None}


@router.get("/api/cron/preview")
def cron_preview(expr: str) -> dict:
    return _cron_preview(expr)
