from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
from pathlib import Path

from ..models import Article
from .common import format_published, safe_filename

_SEP = "\n\n" + "-" * 40 + "\n\n"
_BREAK_TAGS = {"p", "br", "div", "li", "h1", "h2", "h3", "tr"}


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


def _strip_html(html: str) -> str:
    p = _Stripper()
    p.feed(html)
    return unescape(p.text())


def _entry(article: Article) -> str:
    meta = " · ".join(
        b for b in (article.source_title or "", article.link or "",
                    format_published(article.published_at)) if b
    )
    blocks = [f"# {article.title}", meta] if meta else [f"# {article.title}"]
    body = ""
    if article.summary and article.summary.strip():
        body += article.summary.strip() + "\n\n"
    body += _strip_html(article.content_html)
    return "\n".join(blocks) + "\n\n" + body


def build_txt(title: str, articles: list[Article], *, out_dir: Path) -> Path:
    doc = _SEP.join(_entry(a) for a in articles).rstrip() + "\n"
    path = Path(out_dir) / safe_filename(title, "txt")
    path.write_text(doc, encoding="utf-8")
    return path
