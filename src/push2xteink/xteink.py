from __future__ import annotations

import hashlib
import time
from pathlib import Path

import httpx

from .http import make_client
from .models import XteinkConfig
from .state import State

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
        self._api_client = make_client(timeout=timeout)
        self._oss_client = make_client(timeout=timeout)

    def close(self) -> None:
        for attr in ("_api_client", "_oss_client"):
            client = getattr(self, attr, None)
            if client is not None:
                client.close()

    def __enter__(self) -> XteinkClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @staticmethod
    def _json_dict(resp: httpx.Response, step: str) -> dict:
        try:
            data = resp.json()
        except ValueError as exc:
            raise XteinkUploadError(
                f"{step} returned non-JSON body: {resp.text[:200]!r}"
            ) from exc
        if not isinstance(data, dict):
            raise XteinkUploadError(f"{step} returned non-object JSON: {data!r}")
        return data

    # --- token ---
    def _login(self) -> str:
        try:
            resp = self._api_client.post(
                f"{self._api}/auth/login",
                json={"username": self._cfg.username, "password": self._cfg.password},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise XteinkUploadError(f"login request failed: {exc}") from exc
        data = self._json_dict(resp, "login")
        token = data.get("access_token")
        if not token:
            raise XteinkUploadError(f"login response had no access_token: {data!r}")
        return token

    def _access_token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh:
            cached = self._state.kv_get(_TOKEN_KEY)
            try:
                ts = float(self._state.kv_get(_TOKEN_TS_KEY) or 0.0)
            except (TypeError, ValueError):
                ts = 0.0  # unreadable -> treat as expired, relogin repairs it
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
        if resp.status_code >= 300:
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
            resp = self._api_client.post(
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
        sig = self._json_dict(resp, "signature")
        if sig.get("success") is False or any(
            k not in sig for k in ("host", "key", "policy", "signature", "access_key_id")
        ):
            raise XteinkUploadError(f"signature response incomplete: {sig!r}")
        return sig

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
            resp = self._oss_client.post(sig["host"], files=files)
        except httpx.HTTPError as exc:
            raise XteinkUploadError(f"OSS upload failed: {exc}") from exc
        if resp.status_code != 204:
            raise XteinkUploadError(
                f"OSS upload returned {resp.status_code}: {resp.text[:200]}"
            )

    def _device_id(self, token: str) -> str:
        try:
            resp = self._api_client.get(
                f"{self._api}/api/v1/device/binding",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise XteinkUploadError(f"device binding request failed: {exc}") from exc
        self._check_status(resp, "device binding")
        data = self._json_dict(resp, "device binding")
        devices = data.get("data")
        if not isinstance(devices, list) or not devices:
            raise XteinkUploadError(f"no bound device: {data!r}")
        chosen = next(
            (d for d in devices if isinstance(d, dict) and d.get("selected")), None
        )
        if chosen is None and isinstance(devices[0], dict):
            chosen = devices[0]
        device_id = chosen.get("device_id") if isinstance(chosen, dict) else None
        if not device_id:
            raise XteinkUploadError(f"bound device has no device_id: {data!r}")
        return device_id

    def _create_device_task(
        self, token: str, device_id: str, file_url: str, save_path: str
    ) -> str:
        try:
            resp = self._api_client.post(
                f"{self._api}/api/v1/device/tasks",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "device_id": device_id,
                    "file_url": file_url,
                    "save_path": save_path,
                    "points_source": "playmethod",
                    "func_code": "h5-file-upload",
                },
            )
        except httpx.HTTPError as exc:
            raise XteinkUploadError(f"device task request failed: {exc}") from exc
        self._check_status(resp, "device task")
        data = self._json_dict(resp, "device task")
        task = data.get("task")
        task_id = task.get("task_id") if isinstance(task, dict) else None
        if not task_id:
            raise XteinkUploadError(f"device task response had no task_id: {data!r}")
        return task_id

    def _callback(
        self, token: str, sig: dict, filename: str, file_md5: str,
        file_size: int, content_type: str,
    ) -> str:
        try:
            resp = self._api_client.post(
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
        data = self._json_dict(resp, "callback")
        record_id = data.get("record_id")
        if not record_id:
            raise XteinkUploadError(f"callback response had no record_id: {data!r}")
        return record_id

    # --- orchestration ---
    def push_file(
        self, path: Path, filename: str, *, save_path: str | None = None
    ) -> str:
        ext = path.suffix.lower()
        content_type = _CONTENT_TYPES.get(ext)
        if content_type is None:
            raise XteinkUploadError(f"unsupported file extension {ext!r} (no content type)")

        try:
            data = path.read_bytes()
        except OSError as exc:
            raise XteinkUploadError(f"cannot read {path}: {exc}") from exc
        file_md5 = hashlib.md5(data).hexdigest()
        file_size = len(data)

        sig = self._auth_retry(
            lambda tok: self._request_signature(tok, filename, content_type, file_md5, file_size)
        )
        if not sig.get("instant_upload"):
            self._upload_to_oss(sig, content_type, data)
        record_id = self._auth_retry(
            lambda tok: self._callback(tok, sig, filename, file_md5, file_size, content_type)
        )

        # Steps A-C only stage the file in OSS. Without this the file never
        # reaches the account or the bound reader -- the upload/callback protocol
        # captured in the spec was missing this final "push to device" call.
        file_url = sig.get("download_url") or (
            f"{sig['host'].rstrip('/')}/{sig['key'].lstrip('/')}"
        )
        if save_path is None:
            save_path = f"/Pushed Books/{filename}"
        device_id = self._auth_retry(self._device_id)
        self._auth_retry(
            lambda tok: self._create_device_task(tok, device_id, file_url, save_path)
        )
        return record_id
