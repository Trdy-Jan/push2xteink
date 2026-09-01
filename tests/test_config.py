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


# --------------------------------------------------------------------------- #
# I1 — standalone comments between list items must not drift
# --------------------------------------------------------------------------- #

# Every field explicit, so model_dump() adds nothing and write_config is a pure
# identity transform. Two standalone comments (one above the first feed, one
# BETWEEN feeds) plus an inline eol comment.
EXPLICIT_CONFIG = """# push2xteink config
xteink:
  username: "15800000000"
  password: "secret"
  api_base: https://api-prod.xteink.cn

feeds:
  # the good stuff
  - id: a
    url: https://a.example/rss
    full_text: true
    use_proxy: false
  # standalone between a and b
  - id: b
    url: https://b.example/rss   # inline eol comment
    full_text: false
    use_proxy: false

tasks:
  - id: t1
    name: T1
    feeds: [a, b]
    schedule: "0 7 * * *"
    summarize: false
    format: epub
    enabled: true
    first_run_lookback_hours: 48

proxy:
  # no proxy for now
  url:

fetch:
  timeout_seconds: 20
  concurrency: 5
"""


def test_write_identity_roundtrip_is_byte_identical(tmp_path):
    src = tmp_path / "c.yaml"
    src.write_text(EXPLICIT_CONFIG, encoding="utf-8")
    write_config(src, load_config(src))
    assert src.read_text(encoding="utf-8") == EXPLICIT_CONFIG


def test_standalone_comment_between_items_survives_a_real_edit(tmp_path):
    src = tmp_path / "c.yaml"
    src.write_text(EXPLICIT_CONFIG, encoding="utf-8")
    cfg = load_config(src)
    cfg.tasks[0].name = "晨报"
    write_config(src, cfg)

    text = src.read_text(encoding="utf-8")
    assert load_config(src).tasks[0].name == "晨报"
    # each comment still immediately precedes the item it documents
    assert "# the good stuff\n  - id: a" in text
    assert "# standalone between a and b\n  - id: b" in text
    assert "# inline eol comment" in text
    assert "# no proxy for now" in text


def test_reorder_carries_each_comment_with_its_item(tmp_path):
    src = tmp_path / "c.yaml"
    src.write_text(EXPLICIT_CONFIG, encoding="utf-8")
    cfg = load_config(src)
    cfg.feeds = list(reversed(cfg.feeds))
    write_config(src, cfg)

    text = src.read_text(encoding="utf-8")
    assert [f.id for f in load_config(src).feeds] == ["b", "a"]
    # comments moved WITH their feed, not left pinned to the old positions
    assert "# standalone between a and b\n  - id: b" in text
    assert "# the good stuff\n  - id: a" in text


def test_deleting_an_item_drops_only_its_own_comment(tmp_path):
    src = tmp_path / "c.yaml"
    src.write_text(EXPLICIT_CONFIG, encoding="utf-8")
    cfg = load_config(src)
    cfg.feeds = [f for f in cfg.feeds if f.id == "b"]
    cfg.tasks[0].feeds = ["b"]
    write_config(src, cfg)

    text = src.read_text(encoding="utf-8")
    assert "# standalone between a and b\n  - id: b" in text
    assert "# the good stuff" not in text


def test_unchanged_scalars_keep_their_original_quoting(tmp_path):
    src = tmp_path / "c.yaml"
    src.write_text(EXPLICIT_CONFIG, encoding="utf-8")
    cfg = load_config(src)
    cfg.fetch.concurrency = 7
    write_config(src, cfg)
    text = src.read_text(encoding="utf-8")
    assert 'username: "15800000000"' in text   # not re-emitted unquoted
    assert "feeds: [a, b]" in text             # flow style preserved
    assert "concurrency: 7" in text


# --------------------------------------------------------------------------- #
# minor — the proxy block keeps its key (and its comments) when url is None
# --------------------------------------------------------------------------- #


def test_proxy_block_kept_with_null_url(tmp_path):
    src = tmp_path / "c.yaml"
    shutil.copy(FIXTURE, src)
    cfg = load_config(src)
    assert cfg.proxy.url is None
    write_config(src, cfg)
    text = src.read_text(encoding="utf-8")
    assert "proxy:" in text
    assert load_config(src).proxy.url is None


# --------------------------------------------------------------------------- #
# I7 — ConfigError must not leak secrets from pydantic's input_value
# --------------------------------------------------------------------------- #


def test_config_error_does_not_leak_secrets(tmp_path):
    src = tmp_path / "c.yaml"
    src.write_text(
        "xteink:\n"
        "  password: hunter2-TOPSECRET\n"          # username missing
        "feeds:\n  - id: a\n    url: https://a.example/rss\n"
        "tasks:\n  - id: t\n    name: T\n    feeds: [a]\n    schedule: '0 7 * * *'\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as ei:
        load_config(src)
    msg = str(ei.value)
    assert "xteink.username: Field required" in msg
    assert "hunter2-TOPSECRET" not in msg
    assert "input_value" not in msg


# --------------------------------------------------------------------------- #
# I10 — write_config must not widen the file mode
# --------------------------------------------------------------------------- #


def test_write_preserves_file_mode(tmp_path):
    import os
    import sys

    src = tmp_path / "c.yaml"
    shutil.copy(FIXTURE, src)
    os.chmod(src, 0o600)
    before = os.stat(src).st_mode & 0o777
    write_config(src, load_config(src))
    after = os.stat(src).st_mode & 0o777
    if sys.platform == "win32":
        pytest.skip("POSIX permission bits are not meaningful on Windows")
    assert after == before == 0o600


def test_write_new_file_is_owner_only(tmp_path):
    import os
    import sys

    src = tmp_path / "seed.yaml"
    shutil.copy(FIXTURE, src)
    dst = tmp_path / "fresh.yaml"
    write_config(dst, load_config(src))
    assert dst.exists()
    if sys.platform == "win32":
        pytest.skip("POSIX permission bits are not meaningful on Windows")
    assert os.stat(dst).st_mode & 0o777 == 0o600
