import shutil
from pathlib import Path

import pytest

from push2xteink.models import (
    AIConfig,
    AIProvider,
    Config,
    Feed,
    FetchConfig,
    ProxyConfig,
    Task,
    XteinkConfig,
)


@pytest.fixture
def pipeline_config() -> Config:
    return Config(
        xteink=XteinkConfig(username="u", password="p"),
        proxy=ProxyConfig(url=None),
        fetch=FetchConfig(timeout_seconds=5, concurrency=3),
        ai=AIConfig(primary=AIProvider(base_url="https://ai/v1", api_key="k", model="m")),
        feeds=[
            Feed(id="a", url="https://a.example/rss", full_text=True),
            Feed(id="b", url="https://b.example/rss", full_text=False),
        ],
        tasks=[
            Task(id="brief", name="早报", feeds=["a", "b"], schedule="0 7 * * *",
                 summarize=True, format="epub", first_run_lookback_hours=48),
            Task(id="plain", name="纯文", feeds=["a"], schedule="0 8 * * *",
                 summarize=False, format="txt"),
        ],
    )


@pytest.fixture
def valid_config_dict() -> dict:
    return {
        "xteink": {"username": "15800000000", "password": "secret"},
        "ai": {
            "primary": {
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-test",
                "model": "gpt-4o-mini",
            }
        },
        "feeds": [
            {"id": "hn", "url": "https://news.ycombinator.com/rss"},
            {"id": "blog", "url": "https://example.com/atom.xml", "full_text": False},
        ],
        "tasks": [
            {
                "id": "brief",
                "name": "早报",
                "feeds": ["hn", "blog"],
                "schedule": "0 7 * * *",
                "summarize": True,
            }
        ],
    }


WEB_FIXTURE = Path("tests/fixtures/config_valid.yaml")


@pytest.fixture
def web_env(tmp_path, monkeypatch):
    """(config_path, db_path) for a web app; WEB_PASSWORD cleared."""
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    cfg = tmp_path / "config.yaml"
    shutil.copy(WEB_FIXTURE, cfg)
    return cfg, tmp_path / "state.db"


@pytest.fixture
def web_client(web_env):
    from fastapi.testclient import TestClient

    from push2xteink.web.app import create_app

    cfg, db = web_env
    with TestClient(create_app(cfg, db)) as c:
        yield c
