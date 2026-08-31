from __future__ import annotations

from datetime import datetime
from typing import Literal

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_PROMPT = "用中文简洁总结以下文章的核心要点，输出 3-5 条要点，每条一行。"


class AIProvider(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    api_key: str
    model: str


class AIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary: AIProvider
    fallback: AIProvider | None = None
    use_proxy: bool = False
    prompt: str = DEFAULT_PROMPT
    timeout_seconds: int = Field(60, gt=0)
    max_retries: int = Field(2, ge=0)
    qps: float = Field(1.0, gt=0)


class XteinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str
    api_base: str = "https://api-prod.xteink.cn"


class ProxyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = None


class FetchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: int = Field(20, gt=0)
    concurrency: int = Field(5, gt=0)


class Feed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    url: str
    full_text: bool = True
    use_proxy: bool = False


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    feeds: list[str]
    schedule: str
    summarize: bool = False
    format: Literal["epub", "txt"] = "epub"
    enabled: bool = True
    first_run_lookback_hours: int = Field(48, gt=0)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    xteink: XteinkConfig
    feeds: list[Feed]
    tasks: list[Task]
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    ai: AIConfig | None = None

    @model_validator(mode="after")
    def _cross_field_checks(self) -> "Config":
        feed_ids = [f.id for f in self.feeds]
        dupes = sorted({x for x in feed_ids if feed_ids.count(x) > 1})
        if dupes:
            raise ValueError(f"duplicate feed id(s): {dupes}")

        task_ids = [t.id for t in self.tasks]
        dupes = sorted({x for x in task_ids if task_ids.count(x) > 1})
        if dupes:
            raise ValueError(f"duplicate task id(s): {dupes}")

        known = set(feed_ids)
        for t in self.tasks:
            if not t.feeds:
                raise ValueError(f"task {t.id!r} has no feeds")
            missing = [fid for fid in t.feeds if fid not in known]
            if missing:
                raise ValueError(
                    f"task {t.id!r} references unknown feed(s): {missing}"
                )
            try:
                CronTrigger.from_crontab(t.schedule)
            except ValueError as exc:
                raise ValueError(
                    f"task {t.id!r} has invalid cron {t.schedule!r}: {exc}"
                ) from exc
            if t.summarize and self.ai is None:
                raise ValueError(
                    f"task {t.id!r} has summarize=true but [ai] is not configured"
                )
        return self


class Article(BaseModel):
    feed_id: str
    guid: str
    title: str
    link: str
    published_at: datetime | None = None
    author: str | None = None
    source_title: str | None = None
    content_html: str = ""
    content_is_full_text: bool = False
    summary: str | None = None
