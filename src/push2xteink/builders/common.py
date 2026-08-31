from __future__ import annotations

import re
from datetime import datetime, timezone
from html import escape

from ..models import Article

_ILLEGAL = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_WS = re.compile(r"\s+")
_MAX_STEM = 120


class BuildError(Exception):
    """生成的文件无效（如 EPUB 过小）。"""


def safe_filename(title: str, ext: str) -> str:
    stem = _ILLEGAL.sub("_", title or "")
    stem = _WS.sub(" ", stem).strip().strip(".").strip()
    stem = stem[:_MAX_STEM].strip()
    if not stem.strip("_"):
        # title was empty or consisted only of illegal chars (e.g. "///")
        stem = ""
    if not stem:
        stem = "untitled"
    return f"{stem}.{ext}"


def format_published(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _summary_html(summary: str) -> str:
    lines = [escape(ln.strip()) for ln in summary.splitlines() if ln.strip()]
    return "<div>" + "".join(f"<p>{ln}</p>" for ln in lines) + "</div>"


def chapter_body_html(article: Article) -> str:
    if article.summary and article.summary.strip():
        return _summary_html(article.summary) + "<hr/>" + article.content_html
    return article.content_html
