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


class _XteinkAuthError(XteinkUploadError):
    """401 —— token 失效，可重登重试（内部用，不属于对外契约）。"""


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

    @staticmethod
    def _check_status(resp: httpx.Response, step: str) -> None:
        if resp.status_code == 401:
            raise _XteinkAuthError(f"{step} returned 401 unauthorized")
        if resp.status_code >= 400:
            raise XteinkUploadError(
                f"{step} returned {resp.status_code}: {resp.text[:200]}"
            )

    def _auth_retry(self, fn):
        token = self._access_token()
        try:
            return fn(token)
        except _XteinkAuthError:
            token = self._access_token(force_refresh=True)
            return fn(token)

    # --- upload steps ---
    def _request_signature(
        self, token: str, filename: str, content_type: str, file_md5: str, file_size: int
    ) -> dict:
        try:
            with httpx.Client(timeout=self._timeout, headers=_UA) as client:
                resp = client.post(
                    f"{self._api}/api/v1/upload/signature",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "filename": filename,
                        "content_type": content_type,
                        "file_md5": file_md5,
                        "file_size": file_size,
                        "prefix": "uploads/book",
                    },
                )
        except httpx.HTTPError as exc:
            raise XteinkUploadError(f"signature request failed: {exc}") from exc
        self._check_status(resp, "signature")
        return resp.json()

    def _upload_to_oss(self, sig: dict, content_type: str, data: bytes) -> None:
        files = {
            "key": (None, sig["key"]),
            "policy": (None, sig["policy"]),
            "OSSAccessKeyId": (None, sig["access_key_id"]),
            "signature": (None, sig["signature"]),
            "Content-Type": (None, content_type),
            "file": ("file", data, content_type),
        }
        try:
            with httpx.Client(timeout=self._timeout, headers=_UA) as client:
                resp = client.post(sig["host"], files=files)
        except httpx.HTTPError as exc:
            raise XteinkUploadError(f"OSS upload failed: {exc}") from exc
        if resp.status_code != 204:
            raise XteinkUploadError(
                f"OSS upload returned {resp.status_code}: {resp.text[:200]}"
            )

    def _callback(
        self, token: str, sig: dict, filename: str, file_md5: str,
        file_size: int, content_type: str,
    ) -> str:
        try:
            with httpx.Client(timeout=self._timeout, headers=_UA) as client:
                resp = client.post(
                    f"{self._api}/api/v1/upload/callback",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "oss_key": sig["key"],
                        "filename": filename,
                        "file_size": file_size,
                        "file_md5": file_md5,
                        "content_type": content_type,
                    },
                )
        except httpx.HTTPError as exc:
            raise XteinkUploadError(f"callback request failed: {exc}") from exc
        self._check_status(resp, "callback")
        data = resp.json()
        record_id = data.get("record_id")
        if not record_id:
            raise XteinkUploadError(f"callback response had no record_id: {data!r}")
        return record_id

    # --- orchestration ---
    def push_file(self, path: Path, filename: str) -> str:
        ext = path.suffix.lower()
        content_type = _CONTENT_TYPES.get(ext)
        if content_type is None:
            raise XteinkUploadError(f"unsupported file extension {ext!r} (no content type)")

        data = path.read_bytes()
        file_md5 = hashlib.md5(data).hexdigest()
        file_size = len(data)

        sig = self._auth_retry(
            lambda tok: self._request_signature(tok, filename, content_type, file_md5, file_size)
        )
        if not sig.get("instant_upload"):
            self._upload_to_oss(sig, content_type, data)
        return self._auth_retry(
            lambda tok: self._callback(tok, sig, filename, file_md5, file_size, content_type)
        )
