from pathlib import Path
from datetime import datetime, timezone

import pytest
from ebooklib import epub

from push2xteink.builders.epub import build_epub
from push2xteink.builders.common import BuildError
from push2xteink.models import Article


def _articles(n=2):
    return [
        Article(
            feed_id="f", guid=f"g{i}", title=f"文章 {i}", link=f"https://x/{i}",
            source_title="来源站", author="作者",
            published_at=datetime(2026, 8, 31, 7, 0, tzinfo=timezone.utc),
            content_html=f"<p>正文 {i} " + "内容很长。" * 20 + "</p>",
            summary=("摘要一\n摘要二" if i == 0 else None),
        )
        for i in range(n)
    ]


def test_build_epub_creates_file_with_chapters(tmp_path):
    path = build_epub("早报 20260831", _articles(3), out_dir=tmp_path)
    assert path.exists() and path.suffix == ".epub"
    assert path.name == "早报 20260831.epub"

    book = epub.read_epub(str(path))
    assert book.title == "早报 20260831"
    docs = [i for i in book.get_items() if isinstance(i, epub.EpubHtml) and i.file_name.startswith("ch")]
    assert len(docs) == 3
    first = docs[0].get_content().decode("utf-8")
    assert "文章 0" in first
    assert "摘要一" in first and "<hr" in first
    assert "来源站" in first


def test_build_epub_chapter_without_summary_has_no_hr(tmp_path):
    path = build_epub("t", _articles(2), out_dir=tmp_path)
    book = epub.read_epub(str(path))
    docs = sorted(
        (i for i in book.get_items() if isinstance(i, epub.EpubHtml) and i.file_name.startswith("ch")),
        key=lambda d: d.file_name,
    )
    assert "<hr" not in docs[1].get_content().decode("utf-8")


def test_build_epub_raises_when_too_small(tmp_path, monkeypatch):
    # force write_epub to produce a tiny file
    import push2xteink.builders.epub as mod
    def fake_write(path, book, *a, **k):
        Path(path).write_bytes(b"x")
    monkeypatch.setattr(mod.epub, "write_epub", fake_write)
    with pytest.raises(BuildError):
        build_epub("t", _articles(1), out_dir=tmp_path)


def test_build_epub_overwrites_existing(tmp_path):
    p1 = build_epub("dup", _articles(1), out_dir=tmp_path)
    p2 = build_epub("dup", _articles(2), out_dir=tmp_path)
    assert p1 == p2
    assert epub.read_epub(str(p2))
