import pytest


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
