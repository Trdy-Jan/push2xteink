from __future__ import annotations

import hashlib
import time
from pathlib import Path

import httpx

from .models import XteinkConfig
from .state import State

_UA = {"User-Agent": "push2xteink/0.1"}
_CONTENT_TYPES = {".epub": "application/epub+zip", ".txt": "text/plain"}
_TOKEN_KEY = "xteink_access_token"
_TOKEN_TS_KEY = "xteink_token_obtained_at"
_TOKEN_MAX_AGE = 25 * 24 * 3600


class XteinkUploadError(Exception):
    """xteink 上传流程任一步失败。"""


class XteinkClient:
    def __init__(self, config: XteinkConfig, state: State, *, timeout: float = 30.0) -> None:
        self._cfg = config
        self._state = state
        self._timeout = timeout
        self._api = config.api_base.rstrip("/")

    # --- token ---
    def _login(self) -> str:
        try:
            with httpx.Client(timeout=self._timeout, headers=_UA) as client:
                resp = client.post(
                    f"{self._api}/auth/login",
                    json={"username": self._cfg.username, "password": self._cfg.password},
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise XteinkUploadError(f"login request failed: {exc}") from exc
        token = data.get("access_token")
        if not token:
            raise XteinkUploadError(f"login response had no access_token: {data!r}")
        return token

    def _access_token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh:
            cached = self._state.kv_get(_TOKEN_KEY)
            ts = float(self._state.kv_get(_TOKEN_TS_KEY) or 0.0)
            if cached and (time.time() - ts) <= _TOKEN_MAX_AGE:
                return cached
        token = self._login()
        self._state.kv_set(_TOKEN_KEY, token)
        self._state.kv_set(_TOKEN_TS_KEY, str(time.time()))
        return token
