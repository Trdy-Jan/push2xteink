from __future__ import annotations

import uuid
from html import escape
from pathlib import Path

from ebooklib import epub

from ..models import Article
from .common import BuildError, format_published, safe_filename, summary_paragraphs

_MIN_BYTES = 256
_CSS_NAME = "style/main.css"
_XHTML = (
    '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN">'
    '<head><title>{title}</title></head>'
    "<body>{body}</body></html>"
)

# 只写墨水屏重排引擎大概率认的属性；字体/字号交给设备。
_CSS = """\
body { margin: 0; padding: 0; line-height: 1.6; }
h1, h2, h3 { line-height: 1.3; margin: 0.8em 0 0.4em; }
h2 { font-size: 1.25em; }
h3 { font-size: 1.1em; }
p { margin: 0.5em 0; text-align: justify; }
p.meta { font-size: 0.85em; color: #555; margin-bottom: 1em; }
a { color: inherit; text-decoration: underline; }
img { max-width: 100%; height: auto; display: block; margin: 0.6em auto; }
figure { margin: 0.6em 0; }
figcaption { font-size: 0.85em; color: #555; text-align: center; }
blockquote { margin: 0.6em 1em; padding-left: 0.6em; border-left: 3px solid #999; }
pre { white-space: pre-wrap; font-size: 0.9em; }
code { font-size: 0.9em; }
hr { border: 0; border-top: 1px solid #999; margin: 1.2em 0; }
ul, ol { margin: 0.5em 0; padding-left: 1.4em; }
table { border-collapse: collapse; }
td, th { border: 1px solid #999; padding: 0.2em 0.4em; }
"""


_DIGEST_FILE = "summary.xhtml"


def _chapter_html(article: Article) -> str:
    meta_bits = [b for b in (
        escape(article.source_title or ""),
        f'<a href="{escape(article.link)}">原文</a>' if article.link else "",
        format_published(article.published_at),
    ) if b]
    inner = (
        f"<h1>{escape(article.title)}</h1>"
        f'<p class="meta">{" · ".join(meta_bits)}</p>'
        f"{article.content_html}"
    )
    return _XHTML.format(title=escape(article.title), body=inner)


def _has_summary(article: Article) -> bool:
    return bool(article.summary and article.summary.strip())


def _digest_html(articles: list[Article], chapter_names: list[str]) -> str:
    blocks = ["<h1>AI 总结</h1>"]
    for article, name in zip(articles, chapter_names):
        if not _has_summary(article):
            continue
        blocks.append(
            f'<h2><a href="{name}">{escape(article.title)}</a></h2>'
            f"{summary_paragraphs(article.summary)}"
        )
    return _XHTML.format(title="AI 总结", body="".join(blocks))


def _add_images(book: epub.EpubBook, articles: list[Article]) -> None:
    seen: set[str] = set()
    for article in articles:
        for image in article.images:
            if image.filename in seen:
                continue
            seen.add(image.filename)
            book.add_item(
                epub.EpubItem(
                    uid=f"img_{len(seen)}",
                    file_name=image.filename,
                    media_type=image.media_type,
                    content=image.data,
                )
            )


def build_epub(title: str, articles: list[Article], *, out_dir: Path) -> Path:
    book = epub.EpubBook()
    book.set_identifier(f"urn:uuid:{uuid.uuid4()}")
    book.set_title(title)
    book.set_language("zh-CN")

    css = epub.EpubItem(
        uid="style_main",
        file_name=_CSS_NAME,
        media_type="text/css",
        content=_CSS.encode("utf-8"),
    )
    book.add_item(css)

    chapters: list[epub.EpubHtml] = []
    chapter_names = [f"ch{i:03d}.xhtml" for i in range(len(articles))]
    for i, article in enumerate(articles):
        ch = epub.EpubHtml(
            title=article.title or f"章节 {i + 1}",
            file_name=chapter_names[i],
            lang="zh-CN",
        )
        ch.content = _chapter_html(article)
        ch.add_item(css)  # registers the <link rel="stylesheet"> in the head
        book.add_item(ch)
        chapters.append(ch)

    _add_images(book, articles)

    lead: list[epub.EpubHtml] = []
    if any(_has_summary(a) for a in articles):
        digest = epub.EpubHtml(title="AI 总结", file_name=_DIGEST_FILE, lang="zh-CN")
        digest.content = _digest_html(articles, chapter_names)
        digest.add_item(css)
        book.add_item(digest)
        lead.append(digest)

    book.toc = tuple(lead + chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *lead, *chapters]

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / safe_filename(title, "epub")
    epub.write_epub(str(path), book)
    if path.stat().st_size < _MIN_BYTES:
        raise BuildError(f"generated EPUB too small ({path.stat().st_size} bytes): {path}")
    return path
