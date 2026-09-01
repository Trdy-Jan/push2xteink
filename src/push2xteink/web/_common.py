from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import HTTPException, Request
from pydantic import ValidationError

from ..config import load_config, write_config
from ..models import Config
from ..scheduler import Scheduler
from ..state import State

MASK = "********"


def get_scheduler(request: Request) -> Scheduler:
    return request.app.state.scheduler


def get_db(request: Request) -> State:
    return request.app.state.db_state


def get_config_path(request: Request) -> Path:
    return request.app.state.config_path


def current_config(request: Request) -> Config:
    return get_scheduler(request).config


def mask_secret(v: object) -> str:
    return MASK


def _first_error(exc: ValidationError) -> str:
    errs = exc.errors()
    if not errs:
        return str(exc)
    parts = []
    for e in errs[:3]:
        loc = ".".join(str(x) for x in e.get("loc", ()))
        msg = e.get("msg", "invalid")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def apply_config_change(
    request: Request, mutate: Callable[[dict], None]
) -> Config:
    """Load the live config as a dict, apply ``mutate``, re-validate the whole
    config, persist it via ``write_config`` and reload the scheduler.

    A ``ValidationError`` raised by the re-validation becomes HTTP 400 and the
    file is left untouched.
    """
    sched = get_scheduler(request)
    path = get_config_path(request)
    # Held across read-modify-write-reload: concurrent handlers run on anyio's
    # threadpool with no serialization otherwise, which loses updates and can
    # leave the live config permanently out of sync with the file on disk.
    with request.app.state.config_lock:
        raw = load_config(path).model_dump(mode="json")
        mutate(raw)
        try:
            new_config = Config.model_validate(raw)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=_first_error(exc)) from exc
        write_config(path, new_config)
        # Prime BEFORE reload: the file content is already final here, and
        # reload() can block for the whole drain timeout — long enough for the
        # ConfigWatcher to tick, see a changed token and queue a second,
        # identical reload.
        sched.prime_config_token(path)
        if not sched.reload(new_config):
            # Config is valid and now on disk, but the scheduler could not build
            # a pipeline for it. Drop the token so the ConfigWatcher retries the
            # on-disk config, and surface the failure instead of a false 200.
            sched.invalidate_config_token()
            raise HTTPException(
                status_code=500,
                detail="config saved but scheduler reload failed — check server logs",
            )
        return new_config
