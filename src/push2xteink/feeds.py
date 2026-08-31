from __future__ import annotations

import calendar
import dataclasses
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from .models import Article, Feed
from .state import State

_UA = {"User-Agent": "push2xteink/0.1 (+https://github.com/)"}


@dataclasses.dataclass
class FeedResult:
    articles: list[Article] = dataclasses.field(default_factory=list)
    error: str | None = None


def _struct_to_utc(st) -> datetime | None:
    if not st:
        return None
    return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)


def _guid(entry) -> str:
    return entry.get("id") or entry.get("guid") or entry.get("link") or ""


def _content_html(entry) -> str:
    contents = entry.get("content")
    if contents:
        return contents[0].get("value", "") or ""
    return entry.get("summary", "") or ""


def fetch_feed(
    feed: Feed, *, proxy_url: str | None = None, timeout: float = 20.0
) -> FeedResult:
    try:
        with httpx.Client(
            proxy=proxy_url, timeout=timeout, follow_redirects=True, headers=_UA
        ) as client:
            resp = client.get(feed.url)
            resp.raise_for_status()
        raw = resp.content
    except httpx.HTTPError as exc:
        return FeedResult(error=f"fetch failed: {exc!s}")

    parsed = feedparser.parse(raw)
    if parsed.bozo and not parsed.entries:
        return FeedResult(error=f"parse failed: {parsed.get('bozo_exception')!s}")

    source_title = parsed.feed.get("title")
    articles: list[Article] = []
    for entry in parsed.entries:
        guid = _guid(entry)
        if not guid:
            continue
        articles.append(
            Article(
                feed_id=feed.id,
                guid=guid,
                title=entry.get("title") or "(untitled)",
                link=entry.get("link") or "",
                published_at=_struct_to_utc(
                    entry.get("published_parsed") or entry.get("updated_parsed")
                ),
                author=entry.get("author"),
                source_title=source_title,
                content_html=_content_html(entry),
                content_is_full_text=False,
            )
        )
    return FeedResult(articles=articles)


def select_new_articles(
    state: State,
    feed_id: str,
    articles: list[Article],
    *,
    first_run: bool,
    lookback_hours: int,
    now: datetime | None = None,
) -> list[Article]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(hours=lookback_hours)

    kept: list[Article] = []
    for art in articles:
        if not state.is_item_pushable(feed_id, art.guid, lookback_hours, now=now):
            continue
        if first_run and (art.published_at is None or art.published_at < cutoff):
            continue
        state.record_seen(feed_id, art.guid, now=now)
        kept.append(art)
    return kept
