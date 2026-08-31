from __future__ import annotations

import dataclasses
from pathlib import Path

from .models import Config
from .state import State
from .summarize import Summarizer
from .xteink import XteinkClient


@dataclasses.dataclass
class RunOutcome:
    task_id: str
    status: str
    item_count: int = 0
    file_name: str | None = None
    record_id: str | None = None
    message: str | None = None


class Pipeline:
    def __init__(
        self,
        config: Config,
        state: State,
        *,
        summarizer: Summarizer | None,
        xteink_client: XteinkClient,
        output_dir: str | Path | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._summarizer = summarizer
        self._xteink = xteink_client
        self._output_dir = Path(output_dir) if output_dir is not None else None

    @classmethod
    def from_config(
        cls, config: Config, state: State, *, output_dir: str | Path | None = None
    ) -> "Pipeline":
        summarizer = (
            Summarizer(config.ai, proxy_url=config.proxy.url)
            if config.ai is not None
            else None
        )
        xteink = XteinkClient(config.xteink, state)
        return cls(
            config,
            state,
            summarizer=summarizer,
            xteink_client=xteink,
            output_dir=output_dir,
        )

    def close(self) -> None:
        if self._summarizer is not None:
            self._summarizer.close()
        self._xteink.close()

    def __enter__(self) -> "Pipeline":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def run_task(self, task_id: str, *, now=None) -> RunOutcome:  # Task 5
        raise NotImplementedError
