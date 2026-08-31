from pathlib import Path

import httpx
import respx

from push2xteink.extract import apply_full_text, extract_full_text
from push2xteink.models import Article

FIX = Path(__file__).parent / "fixtures"


@respx.mock
def test_extracts_long_article():
    respx.get("https://site.example/post").mock(
        return_value=httpx.Response(200, text=(FIX / "article_long.html").read_text(encoding="utf-8"))
    )
    out = extract_full_text("https://site.example/post")
    assert out is not None
    assert out.startswith("<p>")
    assert "<nav" not in out and "<script" not in out


@respx.mock
def test_thin_article_returns_none():
    respx.get("https://site.example/thin").mock(
        return_value=httpx.Response(200, text=(FIX / "article_thin.html").read_text(encoding="utf-8"))
    )
    assert extract_full_text("https://site.example/thin", min_chars=200) is None


@respx.mock
def test_http_error_returns_none():
    respx.get("https://site.example/500").mock(return_value=httpx.Response(500))
    assert extract_full_text("https://site.example/500") is None


def test_empty_url_returns_none():
    assert extract_full_text("") is None


@respx.mock
def test_trafilatura_raising_returns_none(monkeypatch):
    respx.get("https://site.example/boom").mock(
        return_value=httpx.Response(200, text="<html><body><article><p>x</p></article></body></html>")
    )

    def _boom(*a, **k):
        raise RuntimeError("malformed html")

    monkeypatch.setattr("push2xteink.extract.trafilatura.extract", _boom)
    assert extract_full_text("https://site.example/boom") is None


@respx.mock
def test_escapes_html_in_extracted_text():
    html = "<html><body><article>" + "<p>Safe &amp; sound. " * 40 + "x < y and a > b</article></body></html>"
    respx.get("https://site.example/esc").mock(return_value=httpx.Response(200, text=html))
    out = extract_full_text("https://site.example/esc")
    assert out is not None
    assert "<script" not in out
    assert "&lt;" in out or "&amp;" in out  # raw < / & were escaped


def _art():
    return Article(feed_id="f", guid="g", title="t", link="https://site.example/post",
                   content_html="<p>rss summary</p>", content_is_full_text=False)


def test_disabled_returns_original():
    a = _art()
    out = apply_full_text(a, enabled=False)
    assert out is a or out == a
    assert out.content_is_full_text is False
    assert out.content_html == "<p>rss summary</p>"


@respx.mock
def test_enabled_success_replaces_content(monkeypatch):
    monkeypatch.setattr("push2xteink.extract.extract_full_text", lambda *a, **k: "<p>full body text</p>")
    out = apply_full_text(_art(), enabled=True)
    assert out.content_is_full_text is True
    assert out.content_html == "<p>full body text</p>"
    assert out.guid == "g"  # other fields preserved


def test_enabled_failure_falls_back(monkeypatch):
    monkeypatch.setattr("push2xteink.extract.extract_full_text", lambda *a, **k: None)
    out = apply_full_text(_art(), enabled=True)
    assert out.content_is_full_text is False
    assert out.content_html == "<p>rss summary</p>"
