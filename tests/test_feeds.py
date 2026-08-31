from pathlib import Path
from datetime import datetime, timedelta, timezone

import httpx
import respx

from push2xteink.feeds import FeedResult, fetch_feed, select_new_articles
from push2xteink.models import Article, Feed
from push2xteink.state import State

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
    # one item has a <link> (guid falls back to link -> kept, published_at None);
    # the other has no id/guid/link at all and must be dropped.
    assert len(result.articles) == 1
    assert result.articles[0].guid == result.articles[0].link
    assert result.articles[0].published_at is None
    assert all(a.title != "Anonymous Item With No Identifiers" for a in result.articles)


@respx.mock
def test_fetch_http_error_returns_error_result():
    respx.get("https://bad.example/rss").mock(return_value=httpx.Response(503))
    result = fetch_feed(Feed(id="bad", url="https://bad.example/rss"))
    assert result.articles == []
    assert result.error is not None and ("503" in result.error or "fetch failed" in result.error)


@respx.mock
def test_fetch_connect_error_returns_error_result():
    respx.get("https://down.example/rss").mock(side_effect=httpx.ConnectError("nope"))
    result = fetch_feed(Feed(id="down", url="https://down.example/rss"))
    assert result.error is not None
    assert result.articles == []


def test_fetch_malformed_url_returns_error_result():
    result = fetch_feed(Feed(id="x", url="not a url"))
    assert isinstance(result, FeedResult)
    assert result.articles == []
    assert result.error is not None


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def _art(guid, published=None):
    return Article(feed_id="f", guid=guid, title="t", link=f"https://x/{guid}", published_at=published)


def test_all_new_on_non_first_run_are_kept_and_recorded(tmp_path):
    s = State(tmp_path / "s.db")
    arts = [_art("g1"), _art("g2")]
    kept = select_new_articles(s, "f", arts, first_run=False, lookback_hours=48, now=NOW)
    assert [a.guid for a in kept] == ["g1", "g2"]
    # recorded => no longer pushable after mark? still pushable (unpushed, in window) but seen
    assert s.is_item_pushable("f", "g1", 48, now=NOW) is True  # unpushed within window
    row = s._conn.execute("SELECT COUNT(*) c FROM seen_items").fetchone()
    assert row["c"] == 2
    s.close()


def test_already_seen_and_pushed_is_skipped(tmp_path):
    s = State(tmp_path / "s.db")
    s.record_seen("f", "g1", now=NOW - timedelta(hours=1))
    s.mark_pushed("f", ["g1"], now=NOW - timedelta(hours=1))
    kept = select_new_articles(s, "f", [_art("g1"), _art("g2")], first_run=False, lookback_hours=48, now=NOW)
    assert [a.guid for a in kept] == ["g2"]
    s.close()


def test_first_run_drops_old_and_undated(tmp_path):
    s = State(tmp_path / "s.db")
    arts = [
        _art("fresh", NOW - timedelta(hours=10)),
        _art("old", NOW - timedelta(hours=100)),
        _art("undated", None),
    ]
    kept = select_new_articles(s, "f", arts, first_run=True, lookback_hours=48, now=NOW)
    assert [a.guid for a in kept] == ["fresh"]
    s.close()


def test_naive_now_is_accepted(tmp_path):
    s = State(tmp_path / "s.db")
    kept = select_new_articles(s, "f", [_art("g1")], first_run=False, lookback_hours=48,
                               now=datetime(2026, 8, 31, 12, 0))
    assert [a.guid for a in kept] == ["g1"]
    s.close()
