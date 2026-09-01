from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import load_config
from ..scheduler import Scheduler
from ..state import State
from ..watcher import ConfigWatcher

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
_basic = HTTPBasic(auto_error=False)


def _auth_dep(creds: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    pw = os.environ.get("WEB_PASSWORD")
    if not pw:
        return
    if creds is None or not secrets.compare_digest(
        creds.password.encode("utf-8"), pw.encode("utf-8")
    ):
        raise HTTPException(
            status_code=401,
            detail="unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


def create_app(config_path: Path, db_path: Path) -> FastAPI:
    config_path, db_path = Path(config_path), Path(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        state = State(db_path)
        sched = Scheduler(load_config(config_path), state)
        sched.start()
        sched.prime_config_token(config_path)
        watcher = ConfigWatcher(config_path, lambda: sched.maybe_reload(config_path))
        watcher.start()
        app.state.db_state = state
        app.state.scheduler = sched
        app.state.config_path = config_path
        try:
            yield
        finally:
            watcher.stop()
            sched.shutdown()
            state.close()

    app = FastAPI(lifespan=lifespan, dependencies=[Depends(_auth_dep)])
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    from .api_feeds import router as feeds_router
    from .api_runs import router as runs_router
    from .api_settings import router as settings_router
    from .api_tasks import router as tasks_router
    from .pages import router as pages_router

    for r in (tasks_router, feeds_router, settings_router, runs_router, pages_router):
        app.include_router(r)
    return app
