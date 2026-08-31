import pytest
from pydantic import ValidationError

from push2xteink.models import Config, DEFAULT_PROMPT


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
