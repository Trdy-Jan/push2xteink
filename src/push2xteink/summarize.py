from __future__ import annotations

import threading
import time

import httpx

from .models import AIConfig, AIProvider

_UA = {"User-Agent": "push2xteink/0.1"}


class SummarizeError(Exception):
    """primary 与 fallback 均无法产出摘要。"""


def build_messages(prompt: str, text: str, *, max_text_chars: int = 12000) -> list[dict]:
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": text[:max_text_chars]},
    ]


def _call_provider(
    provider: AIProvider, messages: list[dict], *, timeout: float, proxy_url: str | None
) -> str:
    url = provider.base_url.rstrip("/") + "/chat/completions"
    with httpx.Client(proxy=proxy_url, timeout=timeout, headers=_UA) as client:
        resp = client.post(
            url,
            headers={"Authorization": f"Bearer {provider.api_key}"},
            json={
                "model": provider.model,
                "messages": messages,
                "temperature": 0.3,
                "stream": False,
            },
        )
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError as exc:
            raise SummarizeError(f"non-JSON response body: {resp.text[:200]!r}") from exc
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SummarizeError(f"malformed response: {data!r}") from exc
    if not isinstance(content, str) or not content.strip():
        raise SummarizeError("empty completion content")
    return content.strip()


class Summarizer:
    def __init__(self, config: AIConfig, *, proxy_url: str | None = None) -> None:
        self._cfg = config
        self._proxy = proxy_url if config.use_proxy else None
        self._min_interval = 1.0 / config.qps
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval

    def _try_provider(self, provider: AIProvider, messages: list[dict]) -> str:
        attempts = self._cfg.max_retries + 1
        last: Exception | None = None
        for _ in range(attempts):
            self._throttle()
            try:
                return _call_provider(
                    provider,
                    messages,
                    timeout=self._cfg.timeout_seconds,
                    proxy_url=self._proxy,
                )
            except (httpx.HTTPError, SummarizeError) as exc:
                last = exc
        raise SummarizeError(" ".join(str(last).split())) from last

    def summarize(self, text: str) -> str:
        messages = build_messages(self._cfg.prompt, text)
        try:
            return self._try_provider(self._cfg.primary, messages)
        except SummarizeError as primary_exc:
            if self._cfg.fallback is None:
                raise
            try:
                return self._try_provider(self._cfg.fallback, messages)
            except SummarizeError as fb_exc:
                raise SummarizeError(
                    f"primary failed ({primary_exc}); fallback failed ({fb_exc})"
                ) from fb_exc
