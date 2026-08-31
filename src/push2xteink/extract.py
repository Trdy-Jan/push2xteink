from __future__ import annotations

from html import escape

import httpx
import trafilatura

from .models import Article

_UA = {"User-Agent": "push2xteink/0.1 (+https://github.com/)"}


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
        with httpx.Client(
            proxy=proxy_url, timeout=timeout, follow_redirects=True, headers=_UA
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
        html = resp.text
    except httpx.HTTPError:
        return None

    text = trafilatura.extract(
        html, include_comments=False, include_tables=False, url=url or None
    )
    if not text or len(text) < min_chars:
        return None
    paras = [escape(p.strip()) for p in text.split("\n") if p.strip()]
    return "".join(f"<p>{p}</p>" for p in paras)


def apply_full_text(*args, **kwargs):  # Task 4
    raise NotImplementedError
