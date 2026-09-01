from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


class ConfigWatcher:
    """Poll ``path`` on a daemon thread, calling ``on_tick`` every ``interval``.

    ``start()`` / ``stop()`` are idempotent. An exception raised by ``on_tick``
    is logged and swallowed so the polling loop keeps running.
    """

    def __init__(
        self,
        path: Path,
        on_tick: Callable[[], object],
        *,
        interval: float = 5.0,
    ) -> None:
        self._path = Path(path)
        self._on_tick = on_tick
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="config-watcher", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._on_tick()
            except Exception:
                logger.exception("config watcher on_tick failed")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)
            self._thread = None
