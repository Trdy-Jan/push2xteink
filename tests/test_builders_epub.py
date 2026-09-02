from pathlib import Path
from datetime import datetime, timezone

import pytest
from ebooklib import epub

from push2xteink.builders.epub import build_epub
from push2xteink.builders.common import BuildError
from push2xteink.models import Article, EmbeddedImage

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def _articles(n=2, *, summaries=True):
    return [
        Article(
            feed_id="f", guid=f"g{i}", title=f"文章 {i}", link=f"https://x/{i}",
            source_title="来源站", author="作者",
            published_at=datetime(2026, 8, 31, 7, 0, tzinfo=timezone.utc),
            content_html=f"<p>正文 {i} " + "内容很长。" * 20 + "</p>",
            summary=("摘要一\n摘要二" if (summaries and i == 0) else None),
        )
        for i in range(n)
    ]


def _chapter_text(book, suffix):
    return next(
        i for i in book.get_items()
        if isinstance(i, epub.EpubHtml) and i.file_name == suffix
    ).get_content().decode("utf-8")


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
    assert "来源站" in first


def test_build_epub_article_chapter_does_not_inline_summary(tmp_path):
    path = build_epub("t", _articles(2), out_dir=tmp_path)
    book = epub.read_epub(str(path))
    ch0 = _chapter_text(book, "ch000.xhtml")
    assert "摘要一" not in ch0
    assert "<hr" not in ch0


def test_build_epub_adds_ai_summary_chapter_when_any_article_has_summary(tmp_path):
    path = build_epub("t", _articles(3), out_dir=tmp_path)
    book = epub.read_epub(str(path))
    digest = _chapter_text(book, "summary.xhtml")
    assert "AI 总结" in digest
    assert "摘要一" in digest and "摘要二" in digest
    assert "文章 0" in digest
    assert 'href="ch000.xhtml"' in digest
    # only article 0 has a summary
    assert "文章 1" not in digest
    # digest leads the reading order (before the first article chapter)
    spine_names = [
        it.file_name
        for it in (book.get_item_with_id(idref) for idref, _ in book.spine)
        if it is not None
    ]
    assert spine_names.index("summary.xhtml") < spine_names.index("ch000.xhtml")


def test_build_epub_no_summary_chapter_when_no_summaries(tmp_path):
    path = build_epub("t", _articles(2, summaries=False), out_dir=tmp_path)
    book = epub.read_epub(str(path))
    assert not [i for i in book.get_items() if i.file_name == "summary.xhtml"]


def test_build_epub_raises_when_too_small(tmp_path, monkeypatch):
    # force write_epub to produce a tiny file
    import push2xteink.builders.epub as mod
    def fake_write(path, book, *a, **k):
        Path(path).write_bytes(b"x")
    monkeypatch.setattr(mod.epub, "write_epub", fake_write)
    with pytest.raises(BuildError):
        build_epub("t", _articles(1), out_dir=tmp_path)


def test_build_epub_escapes_source_title_in_meta(tmp_path):
    art = Article(
        feed_id="f", guid="g0", title="文章", link="https://x/0",
        source_title="Tom & Jerry <b>x",
        published_at=datetime(2026, 8, 31, 7, 0, tzinfo=timezone.utc),
        content_html="<p>正文 " + "内容很长。" * 20 + "</p>",
    )
    path = build_epub("t", [art], out_dir=tmp_path)
    book = epub.read_epub(str(path))
    doc = next(
        i for i in book.get_items()
        if isinstance(i, epub.EpubHtml) and i.file_name.startswith("ch")
    ).get_content().decode("utf-8")
    assert "Tom &amp; Jerry &lt;b&gt;x" in doc
    assert '<a href="https://x/0">原文</a>' in doc
    assert doc.index("Tom &amp; Jerry") < doc.index("原文</a>")


def test_build_epub_creates_missing_out_dir(tmp_path):
    nested = tmp_path / "a" / "b"
    path = build_epub("t", _articles(1), out_dir=nested)
    assert path.exists() and path.parent == nested


def _zip_text(path, suffix):
    import zipfile

    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.endswith(suffix))
        return z.read(name).decode("utf-8")


def test_build_epub_includes_and_links_stylesheet(tmp_path):
    path = build_epub("t", _articles(1), out_dir=tmp_path)
    css = _zip_text(path, "style/main.css")
    assert "line-height" in css and "img {" in css
    chapter = _zip_text(path, "ch000.xhtml")
    assert '<link href="style/main.css" rel="stylesheet" type="text/css"/>' in chapter


def test_build_epub_embeds_article_images(tmp_path):
    art = Article(
        feed_id="f", guid="g0", title="带图", link="https://x/0",
        content_html='<p>正文</p><img src="img/pic.png" alt="p"/>',
        images=[EmbeddedImage(filename="img/pic.png", media_type="image/png", data=PNG)],
    )
    path = build_epub("t", [art], out_dir=tmp_path)
    book = epub.read_epub(str(path))
    imgs = [i for i in book.get_items() if i.file_name == "img/pic.png"]
    assert len(imgs) == 1
    assert imgs[0].get_content() == PNG
    chapter = next(
        i for i in book.get_items()
        if isinstance(i, epub.EpubHtml) and i.file_name.startswith("ch")
    ).get_content().decode("utf-8")
    assert 'src="img/pic.png"' in chapter


def test_build_epub_dedupes_image_shared_by_two_articles(tmp_path):
    img = EmbeddedImage(filename="img/pic.png", media_type="image/png", data=PNG)
    arts = [
        Article(feed_id="f", guid=f"g{i}", title=f"a{i}", link=f"https://x/{i}",
                content_html='<img src="img/pic.png"/>', images=[img])
        for i in range(2)
    ]
    path = build_epub("t", arts, out_dir=tmp_path)
    book = epub.read_epub(str(path))
    assert len([i for i in book.get_items() if i.file_name == "img/pic.png"]) == 1


def test_build_epub_chapter_body_is_well_formed_xml(tmp_path):
    from xml.etree import ElementTree

    path = build_epub("t", _articles(2), out_dir=tmp_path)
    book = epub.read_epub(str(path))
    for doc in book.get_items():
        if isinstance(doc, epub.EpubHtml) and (
            doc.file_name.startswith("ch") or doc.file_name == "summary.xhtml"
        ):
            ElementTree.fromstring(doc.get_content())  # raises on malformed XML


def test_build_epub_overwrites_existing(tmp_path):
    p1 = build_epub("dup", _articles(1), out_dir=tmp_path)
    p2 = build_epub("dup", _articles(2), out_dir=tmp_path)
    assert p1 == p2
    assert epub.read_epub(str(p2))
