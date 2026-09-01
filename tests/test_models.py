from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from push2xteink.models import Article, Config, DEFAULT_PROMPT, ProxyConfig


@pytest.mark.parametrize("url", ["http://127.0.0.1:7890", "socks5://h:1", "https://p:8080", None])
def test_proxy_url_valid_schemes(url):
    assert ProxyConfig(url=url).url == url


@pytest.mark.parametrize("url", ["127.0.0.1:7890", "ftp://x", "just-a-host", ""])
def test_proxy_url_bad_scheme_rejected(url):
    with pytest.raises(ValidationError, match="proxy url"):
        ProxyConfig(url=url)


def test_task_name_length_bounds(valid_config_dict):
    valid_config_dict["tasks"][0]["name"] = "x" * 61
    with pytest.raises(ValidationError, match="at most 60"):
        Config.model_validate(valid_config_dict)
    valid_config_dict["tasks"][0]["name"] = ""
    with pytest.raises(ValidationError):
        Config.model_validate(valid_config_dict)


def test_valid_config_parses(valid_config_dict):
    cfg = Config.model_validate(valid_config_dict)
    assert cfg.xteink.username == "15800000000"
    assert cfg.xteink.api_base == "https://api-prod.xteink.cn"
    assert len(cfg.feeds) == 2
    assert cfg.tasks[0].name == "早报"


def test_config_defaults(valid_config_dict):
    cfg = Config.model_validate(valid_config_dict)
    # feed 默认
    assert cfg.feeds[0].full_text is True
    assert cfg.feeds[0].use_proxy is False
    assert cfg.feeds[1].full_text is False
    # task 默认
    assert cfg.tasks[0].format == "epub"
    assert cfg.tasks[0].enabled is True
    assert cfg.tasks[0].first_run_lookback_hours == 48
    # 顶层默认段
    assert cfg.proxy.url is None
    assert cfg.fetch.timeout_seconds == 20
    assert cfg.fetch.concurrency == 5
    # ai 默认
    assert cfg.ai.fallback is None
    assert cfg.ai.use_proxy is False
    assert cfg.ai.prompt == DEFAULT_PROMPT
    assert cfg.ai.timeout_seconds == 60
    assert cfg.ai.max_retries == 2
    assert cfg.ai.qps == 1.0


def test_ai_optional_when_absent(valid_config_dict):
    valid_config_dict.pop("ai")
    valid_config_dict["tasks"][0]["summarize"] = False
    cfg = Config.model_validate(valid_config_dict)
    assert cfg.ai is None


def test_invalid_format_rejected(valid_config_dict):
    valid_config_dict["tasks"][0]["format"] = "pdf"
    with pytest.raises(ValidationError):
        Config.model_validate(valid_config_dict)


def test_duplicate_feed_id_rejected(valid_config_dict):
    valid_config_dict["feeds"].append(
        {"id": "hn", "url": "https://other.example/rss"}
    )
    with pytest.raises(ValidationError, match="duplicate feed id"):
        Config.model_validate(valid_config_dict)


def test_duplicate_task_id_rejected(valid_config_dict):
    valid_config_dict["tasks"].append(
        {
            "id": "brief",
            "name": "夜报",
            "feeds": ["hn"],
            "schedule": "0 22 * * *",
        }
    )
    with pytest.raises(ValidationError, match="duplicate task id"):
        Config.model_validate(valid_config_dict)


def test_task_unknown_feed_rejected(valid_config_dict):
    valid_config_dict["tasks"][0]["feeds"] = ["hn", "ghost"]
    with pytest.raises(ValidationError, match="unknown feed"):
        Config.model_validate(valid_config_dict)


def test_task_empty_feeds_rejected(valid_config_dict):
    valid_config_dict["tasks"][0]["feeds"] = []
    with pytest.raises(ValidationError, match="no feeds"):
        Config.model_validate(valid_config_dict)


def test_invalid_cron_rejected(valid_config_dict):
    valid_config_dict["tasks"][0]["schedule"] = "not a cron"
    with pytest.raises(ValidationError, match="invalid cron"):
        Config.model_validate(valid_config_dict)


def test_summarize_without_ai_rejected(valid_config_dict):
    valid_config_dict.pop("ai")
    # tasks[0].summarize 仍为 True
    with pytest.raises(ValidationError, match="summarize=true"):
        Config.model_validate(valid_config_dict)


@pytest.mark.parametrize(
    "section, patch",
    [
        ("ai", {"qps": 0}),
        ("ai", {"max_retries": -1}),
        ("ai", {"timeout_seconds": 0}),
        ("fetch", {"concurrency": 0}),
        ("fetch", {"timeout_seconds": 0}),
    ],
)
def test_numeric_lower_bounds_rejected(valid_config_dict, section, patch):
    if section == "fetch":
        valid_config_dict["fetch"] = patch
    else:
        valid_config_dict["ai"].update(patch)
    with pytest.raises(ValidationError):
        Config.model_validate(valid_config_dict)


def test_first_run_lookback_hours_zero_rejected(valid_config_dict):
    valid_config_dict["tasks"][0]["first_run_lookback_hours"] = 0
    with pytest.raises(ValidationError):
        Config.model_validate(valid_config_dict)


def test_unknown_key_rejected(valid_config_dict):
    valid_config_dict["notes"] = "keep me"
    with pytest.raises(ValidationError):
        Config.model_validate(valid_config_dict)
    valid_config_dict.pop("notes")
    valid_config_dict["feeds"][0]["feed_url"] = "typo"
    with pytest.raises(ValidationError):
        Config.model_validate(valid_config_dict)


def test_article_minimal():
    a = Article(feed_id="hn", guid="g1", title="T", link="https://x/1")
    assert a.published_at is None
    assert a.summary is None
    assert a.content_html == ""
    assert a.content_is_full_text is False


def test_article_full():
    a = Article(
        feed_id="hn",
        guid="g1",
        title="T",
        link="https://x/1",
        published_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        author="A",
        source_title="Hacker News",
        content_html="<p>body</p>",
        content_is_full_text=True,
        summary="- 要点",
    )
    assert a.content_is_full_text is True
    assert a.published_at.year == 2026
