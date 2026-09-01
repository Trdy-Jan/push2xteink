import shutil
from pathlib import Path

import pytest

from push2xteink.config import ConfigError, load_config, write_config

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


def test_unknown_top_level_key_rejected(tmp_path):
    dst = tmp_path / "config.yaml"
    shutil.copy(FIXTURE, dst)
    dst.write_text(
        dst.read_text(encoding="utf-8") + "\nnotes: keep me\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_config(dst)


def test_unknown_nested_key_rejected(tmp_path):
    dst = tmp_path / "config.yaml"
    dst.write_text(
        "xteink:\n"
        "  username: u\n"
        "  password: p\n"
        "feeds:\n"
        "  - id: a\n"
        "    url: https://a.example/rss\n"
        "    ful_text: true\n"
        "tasks:\n"
        "  - id: t\n"
        "    name: T\n"
        "    feeds: [a]\n"
        "    schedule: '0 7 * * *'\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(dst)


def test_write_over_empty_file(tmp_path):
    dst = tmp_path / "config.yaml"
    src_cfg = load_config_fixture(tmp_path)
    dst.write_bytes(b"")
    write_config(dst, src_cfg)
    assert load_config(dst).tasks[0].id == "brief"


def test_write_over_broken_yaml(tmp_path):
    dst = tmp_path / "config.yaml"
    src_cfg = load_config_fixture(tmp_path)
    dst.write_text("not: [valid yaml", encoding="utf-8")
    write_config(dst, src_cfg)
    reloaded = load_config(dst)
    assert reloaded.tasks[0].id == "brief"
    assert [f.id for f in reloaded.feeds] == ["hn"]


def test_write_roundtrip_values(tmp_path):
    dst = tmp_path / "config.yaml"
    shutil.copy(FIXTURE, dst)
    cfg = load_config(dst)
    cfg.tasks[0].name = "晨间简报"
    cfg.feeds.append(
        type(cfg.feeds[0])(id="lobsters", url="https://lobste.rs/rss")
    )
    write_config(dst, cfg)

    reloaded = load_config(dst)
    assert reloaded.tasks[0].name == "晨间简报"
    assert [f.id for f in reloaded.feeds] == ["hn", "lobsters"]
    assert reloaded.feeds[1].full_text is True  # 默认值回填


def test_write_preserves_header_comment(tmp_path):
    dst = tmp_path / "config.yaml"
    shutil.copy(FIXTURE, dst)
    cfg = load_config(dst)
    write_config(dst, cfg)
    assert "# push2xteink config" in dst.read_text(encoding="utf-8")


def test_write_creates_new_file(tmp_path):
    dst = tmp_path / "fresh.yaml"
    src_cfg = load_config_fixture(tmp_path)
    write_config(dst, src_cfg)
    assert dst.exists()
    assert load_config(dst).tasks[0].id == "brief"


def test_write_preserves_inline_comments_on_unchanged_leaves(tmp_path):
    src = tmp_path / "c.yaml"
    src.write_text(
        "# header\n"
        "xteink:\n  username: u  # my phone\n  password: p\n"
        "proxy:\n  url: http://127.0.0.1:7890  # clash\n"
        "feeds:\n"
        "  - id: hn\n    url: https://news.ycombinator.com/rss\n    full_text: true  # hn note\n"
        "tasks:\n"
        "  - id: brief\n    name: 早报\n    feeds: [hn]\n    schedule: '0 7 * * *'\n",
        encoding="utf-8",
    )
    cfg = load_config(src)
    cfg.tasks[0].name = "晨报"                       # change one scalar
    cfg.feeds.append(type(cfg.feeds[0])(id="lob", url="https://lobste.rs/rss"))
    write_config(src, cfg)

    text = src.read_text(encoding="utf-8")
    assert "# header" in text
    assert "# my phone" in text        # unchanged leaf comment kept
    assert "# clash" in text
    assert "# hn note" in text         # matched feed item's comment kept
    reloaded = load_config(src)
    assert reloaded.tasks[0].name == "晨报"
    assert [f.id for f in reloaded.feeds] == ["hn", "lob"]


def test_write_removes_deleted_feed(tmp_path):
    src = tmp_path / "c.yaml"
    shutil.copy(FIXTURE, src)
    cfg = load_config(src)
    feed_cls = type(cfg.feeds[0])
    cfg.feeds.append(feed_cls(id="extra", url="https://extra.example/rss"))
    write_config(src, cfg)

    cfg = load_config(src)
    original_ids = [f.id for f in cfg.feeds]
    cfg.feeds.pop()
    # a task may reference the removed feed in the fixture — adjust tasks too
    cfg.tasks[0].feeds = [cfg.feeds[0].id]
    write_config(src, cfg)
    assert [f.id for f in load_config(src).feeds] == original_ids[:-1]


def load_config_fixture(tmp_path):
    tmp = tmp_path / "_src.yaml"
    shutil.copy(FIXTURE, tmp)
    return load_config(tmp)
