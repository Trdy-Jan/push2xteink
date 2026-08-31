from __future__ import annotations

import uuid
from html import escape
from pathlib import Path

from ebooklib import epub

from ..models import Article
from .common import BuildError, chapter_body_html, format_published, safe_filename

_MIN_BYTES = 256
_XHTML = (
    '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN">'
    "<head><title>{title}</title></head><body>{body}</body></html>"
)


def _chapter_html(article: Article) -> str:
    meta_bits = [b for b in (
        escape(article.source_title or ""),
        f'<a href="{escape(article.link)}">原文</a>' if article.link else "",
        format_published(article.published_at),
    ) if b]
    inner = (
        f"<h1>{escape(article.title)}</h1>"
        f'<p class="meta">{" · ".join(meta_bits)}</p>'
        f"{chapter_body_html(article)}"
    )
    return _XHTML.format(title=escape(article.title), body=inner)


def build_epub(title: str, articles: list[Article], *, out_dir: Path) -> Path:
    book = epub.EpubBook()
    book.set_identifier(f"urn:uuid:{uuid.uuid4()}")
    book.set_title(title)
    book.set_language("zh-CN")

    chapters: list[epub.EpubHtml] = []
    for i, article in enumerate(articles):
        ch = epub.EpubHtml(
            title=article.title or f"章节 {i + 1}",
            file_name=f"ch{i:03d}.xhtml",
            lang="zh-CN",
        )
        ch.content = _chapter_html(article)
        book.add_item(ch)
        chapters.append(ch)

    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *chapters]

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / safe_filename(title, "epub")
    epub.write_epub(str(path), book)
    if path.stat().st_size < _MIN_BYTES:
        raise BuildError(f"generated EPUB too small ({path.stat().st_size} bytes): {path}")
    return path
