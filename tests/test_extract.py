from pathlib import Path

import httpx
import respx

from push2xteink.extract import extract_full_text

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
def test_escapes_html_in_extracted_text():
    html = "<html><body><article>" + "<p>Safe &amp; sound. " * 40 + "x < y and a > b</article></body></html>"
    respx.get("https://site.example/esc").mock(return_value=httpx.Response(200, text=html))
    out = extract_full_text("https://site.example/esc")
    assert out is not None
    assert "<script" not in out
    assert "&lt;" in out or "&amp;" in out  # raw < / & were escaped
