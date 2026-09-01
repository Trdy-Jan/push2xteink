from __future__ import annotations

import base64
import os
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from ..config import load_config
from ..scheduler import Scheduler
from ..state import State
from ..watcher import ConfigWatcher

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

# Docker HEALTHCHECK has no way to carry credentials; /healthz leaks nothing
# beyond "the process is up", so it stays unauthenticated by design.
_AUTH_EXEMPT_PATHS = frozenset({"/healthz"})

# Web-initiated reloads happen inside a request. 120s (the CLI default) would
# hold a worker thread past every sane reverse-proxy timeout; a run that outlives
# 25s is handed to P4's _orphans list, which closes it at shutdown.
_WEB_DRAIN_TIMEOUT = 25.0


class _BasicAuthMiddleware(BaseHTTPMiddleware):
    """Basic-Auth gate + same-origin guard for every route.

    A ``FastAPI(dependencies=[...])`` only covers ``APIRoute``s, leaving
    ``/openapi.json``, ``/docs``, ``/redoc`` and the ``/static`` mount wide open.
    An HTTP middleware sees all of them.

    The ``Origin`` check is CSRF defence: browsers auto-attach Basic-Auth
    credentials to cross-site ``<form method=post>`` submissions, so without it
    any page on the internet could rewrite the config of a password-protected
    instance.
    """

    async def dispatch(self, request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host", ""):
                return Response(status_code=403, content="cross-origin request refused")

        if request.url.path not in _AUTH_EXEMPT_PATHS:
            pw = os.environ.get("WEB_PASSWORD")
            if pw and not _basic_auth_ok(request.headers.get("authorization", ""), pw):
                return Response(
                    status_code=401, headers={"WWW-Authenticate": "Basic"}
                )
        return await call_next(request)


def _basic_auth_ok(header: str, password: str) -> bool:
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except Exception:  # noqa: BLE001 - any malformed header is simply unauthorized
        return False
    _, sep, given = decoded.partition(":")
    if not sep:
        return False
    return secrets.compare_digest(given.encode("utf-8"), password.encode("utf-8"))


def create_app(config_path: Path, db_path: Path) -> FastAPI:
    config_path, db_path = Path(config_path), Path(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        state: State | None = None
        sched: Scheduler | None = None
        watcher: ConfigWatcher | None = None
        try:
            state = State(db_path)
            sched = Scheduler(
                load_config(config_path), state, drain_timeout=_WEB_DRAIN_TIMEOUT
            )
            sched.start()
            sched.prime_config_token(config_path)
            watcher = ConfigWatcher(config_path, lambda: sched.maybe_reload(config_path))
            watcher.start()
        except BaseException:
            # Close whatever got built so a failed startup doesn't strand a
            # Pipeline's httpx clients or the SQLite handle.
            if watcher is not None:
                watcher.stop()
            if sched is not None:
                sched.shutdown()
            if state is not None:
                state.close()
            raise
        app.state.db_state = state
        app.state.scheduler = sched
        app.state.config_path = config_path
        # Serializes apply_config_change: without it two concurrent handlers can
        # lose an update and permanently desync live config from disk.
        app.state.config_lock = threading.Lock()
        try:
            yield
        finally:
            watcher.stop()
            sched.shutdown()
            state.close()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(_BasicAuthMiddleware)
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
