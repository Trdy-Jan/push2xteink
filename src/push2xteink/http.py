from __future__ import annotations

import ssl

import certifi
import httpx

_CTX = ssl.create_default_context(cafile=certifi.where())

USER_AGENT = "push2xteink/0.1 (+https://github.com/push2xteink)"


def make_client(
    *,
    proxy: str | None = None,
    timeout: float,
    follow_redirects: bool = True,
) -> httpx.Client:
    """A configured httpx.Client. Never consults environment proxy settings --
    proxying is always explicit via the `proxy` arg."""
    return httpx.Client(
        proxy=proxy,
        timeout=timeout,
        verify=_CTX,
        trust_env=False,
        follow_redirects=follow_redirects,
        headers={"User-Agent": USER_AGENT},
    )
