from datetime import datetime, timezone

from push2xteink.builders.common import (
    chapter_body_html, format_published, safe_filename,
)
from push2xteink.models import Article


def test_safe_filename_replaces_illegal_chars():
    assert safe_filename('a/b\\c:d*e?f"g<h>i|j', "epub") == "a_b_c_d_e_f_g_h_i_j.epub"


def test_safe_filename_collapses_ws_and_trims_dots():
    assert safe_filename("  hello   world  ", "txt") == "hello world.txt"
    assert safe_filename("...name...", "txt") == "name.txt"


def test_safe_filename_empty_becomes_untitled():
    assert safe_filename("   ", "epub") == "untitled.epub"
    assert safe_filename("///", "epub") == "untitled.epub"


def test_safe_filename_truncates_long():
    name = safe_filename("x" * 300, "epub")
    assert name == "x" * 120 + ".epub"


def test_format_published():
    assert format_published(None) == ""
    assert format_published(datetime(2026, 8, 31, 7, 5, tzinfo=timezone.utc)) == "2026-08-31 07:05"
    # non-UTC input normalized
    from datetime import timedelta
    tz = timezone(timedelta(hours=8))
    assert format_published(datetime(2026, 8, 31, 15, 5, tzinfo=tz)) == "2026-08-31 07:05"


def test_chapter_body_without_summary_is_content_only():
    a = Article(feed_id="f", guid="g", title="t", link="l", content_html="<p>body</p>")
    assert chapter_body_html(a) == "<p>body</p>"


def test_chapter_body_with_summary_prepends_and_separates():
    a = Article(feed_id="f", guid="g", title="t", link="l",
                content_html="<p>body</p>", summary="line one\nline two")
    out = chapter_body_html(a)
    assert out.index("line one") < out.index("<hr")
    assert out.index("<hr") < out.index("<p>body</p>")
    assert "<p>line one</p>" in out and "<p>line two</p>" in out


def test_chapter_body_escapes_summary():
    a = Article(feed_id="f", guid="g", title="t", link="l",
                content_html="<p>b</p>", summary="a < b & c")
    out = chapter_body_html(a)
    assert "a &lt; b &amp; c" in out
