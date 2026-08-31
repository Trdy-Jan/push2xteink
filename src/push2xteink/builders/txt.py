from __future__ import annotations

from html import escape
from pathlib import Path

from ..models import Article
from .common import format_published, html_to_text, safe_filename

_SEP = "\n\n" + "-" * 40 + "\n\n"


def _entry(article: Article) -> str:
    meta = " · ".join(
        b for b in (escape(article.source_title or ""), article.link or "",
                    format_published(article.published_at)) if b
    )
    blocks = [f"# {article.title}", meta] if meta else [f"# {article.title}"]
    body = ""
    if article.summary and article.summary.strip():
        body += article.summary.strip() + "\n\n"
    body += html_to_text(article.content_html)
    return "\n".join(blocks) + "\n\n" + body


def build_txt(title: str, articles: list[Article], *, out_dir: Path) -> Path:
    doc = _SEP.join(_entry(a) for a in articles).rstrip() + "\n"
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / safe_filename(title, "txt")
    path.write_text(doc, encoding="utf-8")
    return path
