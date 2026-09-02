from __future__ import annotations

import httpx
import trafilatura

from .builders.common import html_to_text
from .builders.htmlclean import normalize_html
from .http import make_client
from .models import Article


def extract_full_text(
    url: str,
    *,
    proxy_url: str | None = None,
    timeout: float = 20.0,
    min_chars: int = 200,
) -> str | None:
    if not url:
        return None
    try:
        with make_client(proxy=proxy_url, timeout=timeout) as client:
            resp = client.get(url)
            resp.raise_for_status()
        html = resp.text
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        return None

    try:
        extracted = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            include_images=True,
            include_formatting=True,
            output_format="html",
            url=url or None,
        )
    except Exception:
        return None
    if not extracted:
        return None

    clean = normalize_html(extracted, base_url=url)
    if not clean.html or len(html_to_text(clean.html)) < min_chars:
        return None
    return clean.html


def apply_full_text(
    article: Article,
    *,
    enabled: bool,
    proxy_url: str | None = None,
    timeout: float = 20.0,
) -> Article:
    if not enabled:
        return article
    extracted = extract_full_text(article.link, proxy_url=proxy_url, timeout=timeout)
    if extracted is None:
        return article
    return article.model_copy(
        update={"content_html": extracted, "content_is_full_text": True}
    )
