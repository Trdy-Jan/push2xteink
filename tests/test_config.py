import shutil
from pathlib import Path

import pytest

from push2xteink.config import ConfigError, load_config

FIXTURE = Path(__file__).parent / "fixtures" / "config_valid.yaml"


def test_load_valid(tmp_path):
    dst = tmp_path / "config.yaml"
    shutil.copy(FIXTURE, dst)
    cfg = load_config(dst)
    assert cfg.tasks[0].id == "brief"
    assert cfg.feeds[0].url == "https://news.ycombinator.com/rss"


def test_load_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_load_empty_file(tmp_path):
    dst = tmp_path / "config.yaml"
    dst.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="empty"):
        load_config(dst)


def test_load_bad_yaml(tmp_path):
    dst = tmp_path / "config.yaml"
    dst.write_text("xteink: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(dst)


def test_load_invalid_config(tmp_path):
    dst = tmp_path / "config.yaml"
    # task references missing feed below
    dst.write_text(
        "xteink:\n"
        "  username: u\n"
        "  password: p\n"
        "feeds:\n"
        "  - id: a\n"
        "    url: https://a.example/rss\n"
        "tasks:\n"
        "  - id: t\n"
        "    name: T\n"
        "    feeds: [missing]\n"
        "    schedule: '0 7 * * *'\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown feed"):
        load_config(dst)
