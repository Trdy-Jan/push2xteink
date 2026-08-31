from __future__ import annotations

import re
from datetime import datetime, timezone
from html import escape, unescape
from html.parser import HTMLParser

from ..models import Article

_ILLEGAL = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_WS = re.compile(r"\s+")
_MAX_STEM = 120
_BREAK_TAGS = {"p", "br", "div", "li", "h1", "h2", "h3", "tr"}


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


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _BREAK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _BREAK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _BREAK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        out: list[str] = []
        for ln in (line.strip() for line in raw.splitlines()):
            if ln:
                out.append(ln)
            elif out and out[-1] != "":
                out.append("")  # collapse runs of blank lines to exactly one
        while out and out[-1] == "":
            out.pop()
        return "\n".join(out)


def html_to_text(html: str) -> str:
    p = _Stripper()
    p.feed(html)
    return unescape(p.text())


def chapter_body_html(article: Article) -> str:
    if article.summary and article.summary.strip():
        return _summary_html(article.summary) + "<hr/>" + article.content_html
    return article.content_html
