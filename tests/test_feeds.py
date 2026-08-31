from pathlib import Path
from datetime import timezone

import httpx
import pytest
import respx

from push2xteink.feeds import FeedResult, fetch_feed
from push2xteink.models import Feed

FIX = Path(__file__).parent / "fixtures"


@respx.mock
def test_fetch_atom_maps_entries():
    respx.get("https://blog.example/atom.xml").mock(
        return_value=httpx.Response(200, content=(FIX / "rss_atom.xml").read_bytes())
    )
    result = fetch_feed(Feed(id="blog", url="https://blog.example/atom.xml"))
    assert result.error is None
    assert len(result.articles) == 2
    a = result.articles[0]
    assert a.feed_id == "blog"
    assert a.guid  # from <id>
    assert a.title
    assert a.source_title == "Example Blog"
    assert a.author == "Alice"
    assert a.published_at is not None and a.published_at.tzinfo is not None
    assert a.published_at.astimezone(timezone.utc).isoformat() == "2026-08-30T10:00:00+00:00"
    assert "Full body one" in a.content_html
    assert a.content_is_full_text is False


@respx.mock
def test_fetch_rss2_uses_description_when_no_content():
    respx.get("https://x.example/rss").mock(
        return_value=httpx.Response(200, content=(FIX / "rss_rss2.xml").read_bytes())
    )
    result = fetch_feed(Feed(id="x", url="https://x.example/rss"))
    assert len(result.articles) == 2
    assert "Summary text" in result.articles[0].content_html


@respx.mock
def test_fetch_skips_entries_without_guid_or_link():
    respx.get("https://nd.example/rss").mock(
        return_value=httpx.Response(200, content=(FIX / "rss_no_dates.xml").read_bytes())
    )
    result = fetch_feed(Feed(id="nd", url="https://nd.example/rss"))
    # item has a <link>, so guid falls back to link -> kept, published_at None
    assert len(result.articles) == 1
    assert result.articles[0].guid == result.articles[0].link
    assert result.articles[0].published_at is None


@respx.mock
def test_fetch_http_error_returns_error_result():
    respx.get("https://bad.example/rss").mock(return_value=httpx.Response(503))
    result = fetch_feed(Feed(id="bad", url="https://bad.example/rss"))
    assert result.articles == []
    assert result.error is not None and "503" in result.error or "fetch failed" in result.error


@respx.mock
def test_fetch_connect_error_returns_error_result():
    respx.get("https://down.example/rss").mock(side_effect=httpx.ConnectError("nope"))
    result = fetch_feed(Feed(id="down", url="https://down.example/rss"))
    assert result.error is not None
    assert result.articles == []
