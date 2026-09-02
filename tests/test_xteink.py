import json
import time

import httpx
import pytest
import respx

from push2xteink.models import XteinkConfig
from push2xteink.state import State
from push2xteink.xteink import XteinkClient, XteinkUploadError

API = "https://api-prod.xteink.cn"


def _client(tmp_path):
    return XteinkClient(XteinkConfig(username="u", password="p"), State(tmp_path / "s.db"))


@respx.mock
def test_login_caches_token(tmp_path):
    route = respx.post(f"{API}/auth/login").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-abc"})
    )
    c = _client(tmp_path)
    assert c._access_token() == "tok-abc"
    # second call: no new login
    assert c._access_token() == "tok-abc"
    assert route.call_count == 1
    body = json.loads(route.calls.last.request.content)
    assert body == {"username": "u", "password": "p"}


@respx.mock
def test_expired_token_triggers_relogin(tmp_path):
    respx.post(f"{API}/auth/login").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "old"}),
            httpx.Response(200, json={"access_token": "new"}),
        ]
    )
    c = _client(tmp_path)
    assert c._access_token() == "old"
    c._state.kv_set("xteink_token_obtained_at", str(time.time() - 26 * 24 * 3600))
    assert c._access_token() == "new"


@respx.mock
def test_force_refresh_relogins(tmp_path):
    respx.post(f"{API}/auth/login").mock(
        side_effect=[httpx.Response(200, json={"access_token": "a"}),
                     httpx.Response(200, json={"access_token": "b"})]
    )
    c = _client(tmp_path)
    assert c._access_token() == "a"
    assert c._access_token(force_refresh=True) == "b"


@respx.mock
def test_login_failure_raises(tmp_path):
    respx.post(f"{API}/auth/login").mock(return_value=httpx.Response(401, json={"message": "bad creds"}))
    with pytest.raises(XteinkUploadError, match="login"):
        _client(tmp_path)._access_token()


@respx.mock
def test_login_missing_token_raises(tmp_path):
    respx.post(f"{API}/auth/login").mock(return_value=httpx.Response(200, json={"message": None}))
    with pytest.raises(XteinkUploadError):
        _client(tmp_path)._access_token()


SIG_RESP = {
    "success": True, "instant_upload": False,
    "host": "https://oss.example.com",
    "key": "uploads/book/2026/x.epub",
    "policy": "POLICY", "signature": "SIG", "access_key_id": "AK",
}

BINDING_RESP = {
    "success": True,
    "data": [
        {"device_id": "DEV-OLD", "selected": False, "nickname": "old"},
        {"device_id": "DEV-123", "selected": True, "nickname": "阅星曈 X4"},
    ],
}
TASK_RESP = {
    "success": True,
    "task": {"task_id": "task-1", "status": "processing", "task_type": "epub_xtg_push"},
}


def _mock_device_push():
    """Bound-device lookup + push-to-device task, both happy path."""
    binding = respx.get(f"{API}/api/v1/device/binding").mock(
        return_value=httpx.Response(200, json=BINDING_RESP)
    )
    task = respx.post(f"{API}/api/v1/device/tasks").mock(
        return_value=httpx.Response(201, json=TASK_RESP)
    )
    return binding, task


@respx.mock
def test_request_signature_sends_expected_body(tmp_path):
    route = respx.post(f"{API}/api/v1/upload/signature").mock(
        return_value=httpx.Response(200, json=SIG_RESP)
    )
    c = _client(tmp_path)
    sig = c._request_signature("tok", "早报.epub", "application/epub+zip", "md5hex", 12345)
    assert sig["key"] == "uploads/book/2026/x.epub"
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer tok"
    body = json.loads(req.content)
    assert body == {
        "filename": "早报.epub", "content_type": "application/epub+zip",
        "file_md5": "md5hex", "file_size": 12345, "prefix": "uploads/book",
    }


@respx.mock
def test_signature_non_200_raises(tmp_path):
    respx.post(f"{API}/api/v1/upload/signature").mock(return_value=httpx.Response(500))
    with pytest.raises(XteinkUploadError, match="signature"):
        _client(tmp_path)._request_signature("t", "f.epub", "application/epub+zip", "m", 1)


@respx.mock
def test_upload_to_oss_posts_multipart(tmp_path):
    route = respx.post("https://oss.example.com").mock(return_value=httpx.Response(204))
    _client(tmp_path)._upload_to_oss(SIG_RESP, "application/epub+zip", b"EPUBDATA")
    req = route.calls.last.request
    assert "authorization" not in {k.lower() for k in req.headers}
    assert b'name="key"' in req.content and b"uploads/book/2026/x.epub" in req.content
    assert b'name="OSSAccessKeyId"' in req.content and b"AK" in req.content
    assert b"EPUBDATA" in req.content


@respx.mock
def test_upload_to_oss_non_204_raises(tmp_path):
    respx.post("https://oss.example.com").mock(return_value=httpx.Response(403, text="denied"))
    with pytest.raises(XteinkUploadError, match="OSS"):
        _client(tmp_path)._upload_to_oss(SIG_RESP, "text/plain", b"x")


@respx.mock
def test_callback_returns_record_id(tmp_path):
    route = respx.post(f"{API}/api/v1/upload/callback").mock(
        return_value=httpx.Response(200, json={"success": True, "record_id": "rec-1"})
    )
    rid = _client(tmp_path)._callback("tok", SIG_RESP, "早报.epub", "md5", 99, "application/epub+zip")
    assert rid == "rec-1"
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "oss_key": "uploads/book/2026/x.epub", "filename": "早报.epub",
        "file_size": 99, "file_md5": "md5", "content_type": "application/epub+zip",
    }


@respx.mock
def test_callback_without_record_id_raises(tmp_path):
    respx.post(f"{API}/api/v1/upload/callback").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    with pytest.raises(XteinkUploadError, match="callback"):
        _client(tmp_path)._callback("t", SIG_RESP, "f.epub", "m", 1, "application/epub+zip")


@respx.mock
def test_push_file_happy_path(tmp_path):
    f = tmp_path / "早报_20260831.epub"
    f.write_bytes(b"EPUB" * 100)
    respx.post(f"{API}/auth/login").mock(return_value=httpx.Response(200, json={"access_token": "tok"}))
    sig_route = respx.post(f"{API}/api/v1/upload/signature").mock(
        return_value=httpx.Response(200, json=SIG_RESP)
    )
    oss_route = respx.post("https://oss.example.com").mock(return_value=httpx.Response(204))
    cb_route = respx.post(f"{API}/api/v1/upload/callback").mock(
        return_value=httpx.Response(200, json={"record_id": "rec-9"})
    )
    _, task_route = _mock_device_push()
    rid = XteinkClient(
        XteinkConfig(username="u", password="p"), State(tmp_path / "s2.db")
    ).push_file(f, "早报_20260831.epub")
    assert rid == "rec-9"
    sent = json.loads(sig_route.calls.last.request.content)
    assert sent["file_size"] == 400
    assert sent["file_md5"] == __import__("hashlib").md5(b"EPUB" * 100).hexdigest()
    assert oss_route.called and cb_route.called
    # the "push to device" task targets the selected binding + built OSS url
    task_body = json.loads(task_route.calls.last.request.content)
    assert task_body == {
        "device_id": "DEV-123",
        "file_url": "https://oss.example.com/uploads/book/2026/x.epub",
        "save_path": "/Pushed Books/早报_20260831.epub",
        "points_source": "playmethod",
        "func_code": "h5-file-upload",
    }


@respx.mock
def test_push_file_rejects_unknown_extension(tmp_path):
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    with pytest.raises(XteinkUploadError, match="content type|extension"):
        _client(tmp_path).push_file(f, "x.pdf")


@respx.mock
def test_push_file_instant_upload_skips_oss(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"hello world")
    respx.post(f"{API}/auth/login").mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    respx.post(f"{API}/api/v1/upload/signature").mock(
        return_value=httpx.Response(200, json={**SIG_RESP, "instant_upload": True})
    )
    oss = respx.post("https://oss.example.com").mock(return_value=httpx.Response(204))
    respx.post(f"{API}/api/v1/upload/callback").mock(
        return_value=httpx.Response(200, json={"record_id": "r"})
    )
    _mock_device_push()
    assert _client(tmp_path).push_file(f, "a.txt") == "r"
    assert not oss.called


@respx.mock
def test_push_file_401_on_signature_triggers_relogin(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"data")
    # cached "stale" token is still within max-age but the server rejects it (401);
    # push_file must force one relogin and retry with the fresh token.
    login_route = respx.post(f"{API}/auth/login").mock(
        side_effect=[httpx.Response(200, json={"access_token": "fresh"})]
    )
    sig = respx.post(f"{API}/api/v1/upload/signature")
    sig.side_effect = [httpx.Response(401), httpx.Response(200, json=SIG_RESP)]
    respx.post("https://oss.example.com").mock(return_value=httpx.Response(204))
    respx.post(f"{API}/api/v1/upload/callback").mock(
        return_value=httpx.Response(200, json={"record_id": "ok"})
    )
    _mock_device_push()
    c = _client(tmp_path)
    c._state.kv_set("xteink_access_token", "stale")
    c._state.kv_set("xteink_token_obtained_at", str(time.time()))
    assert c.push_file(f, "a.txt") == "ok"
    assert sig.call_count == 2
    assert login_route.call_count == 1  # exactly one relogin
    # first attempt used the stale cache, retry used the fresh token
    assert sig.calls[0].request.headers["authorization"] == "Bearer stale"
    assert sig.calls[-1].request.headers["authorization"] == "Bearer fresh"


@respx.mock
def test_push_file_signature_200_but_unsuccessful_raises(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"data")
    respx.post(f"{API}/auth/login").mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    respx.post(f"{API}/api/v1/upload/signature").mock(
        return_value=httpx.Response(200, json={"success": False, "message": "quota exceeded"})
    )
    with pytest.raises(XteinkUploadError, match="signature response incomplete"):
        _client(tmp_path).push_file(f, "a.txt")


@respx.mock
def test_login_non_json_body_raises_upload_error(tmp_path):
    respx.post(f"{API}/auth/login").mock(return_value=httpx.Response(200, text="<html>err</html>"))
    with pytest.raises(XteinkUploadError):
        _client(tmp_path)._access_token()


@respx.mock
def test_login_non_object_json_raises_upload_error(tmp_path):
    respx.post(f"{API}/auth/login").mock(return_value=httpx.Response(200, json=["not", "a", "dict"]))
    with pytest.raises(XteinkUploadError):
        _client(tmp_path)._access_token()


@respx.mock
def test_signature_non_json_body_raises_upload_error(tmp_path):
    respx.post(f"{API}/api/v1/upload/signature").mock(
        return_value=httpx.Response(200, text="<html>err</html>")
    )
    with pytest.raises(XteinkUploadError):
        _client(tmp_path)._request_signature("t", "f.epub", "application/epub+zip", "m", 1)


@respx.mock
def test_signature_non_object_json_raises_upload_error(tmp_path):
    respx.post(f"{API}/api/v1/upload/signature").mock(
        return_value=httpx.Response(200, json=["not", "a", "dict"])
    )
    with pytest.raises(XteinkUploadError):
        _client(tmp_path)._request_signature("t", "f.epub", "application/epub+zip", "m", 1)


@respx.mock
def test_callback_non_json_body_raises_upload_error(tmp_path):
    respx.post(f"{API}/api/v1/upload/callback").mock(
        return_value=httpx.Response(200, text="<html>err</html>")
    )
    with pytest.raises(XteinkUploadError):
        _client(tmp_path)._callback("t", SIG_RESP, "f.epub", "m", 1, "application/epub+zip")


@respx.mock
def test_callback_non_object_json_raises_upload_error(tmp_path):
    respx.post(f"{API}/api/v1/upload/callback").mock(
        return_value=httpx.Response(200, json=["not", "a", "dict"])
    )
    with pytest.raises(XteinkUploadError):
        _client(tmp_path)._callback("t", SIG_RESP, "f.epub", "m", 1, "application/epub+zip")


@respx.mock
def test_push_file_unreadable_file_raises_upload_error(tmp_path):
    with pytest.raises(XteinkUploadError):
        _client(tmp_path).push_file(tmp_path / "missing.epub", "missing.epub")


@respx.mock
def test_corrupt_token_timestamp_self_heals(tmp_path):
    route = respx.post(f"{API}/auth/login").mock(
        return_value=httpx.Response(200, json={"access_token": "healed"})
    )
    c = _client(tmp_path)
    c._state.kv_set("xteink_access_token", "stale")
    c._state.kv_set("xteink_token_obtained_at", "garbage")
    assert c._access_token() == "healed"
    assert route.called


@respx.mock
def test_client_used_as_context_manager(tmp_path):
    respx.post(f"{API}/auth/login").mock(return_value=httpx.Response(200, json={"access_token": "tok"}))
    with XteinkClient(XteinkConfig(username="u", password="p"), State(tmp_path / "s.db")) as c:
        assert c._access_token() == "tok"


@respx.mock
def test_push_file_401_twice_raises(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"data")
    respx.post(f"{API}/auth/login").mock(return_value=httpx.Response(200, json={"access_token": "x"}))
    respx.post(f"{API}/api/v1/upload/signature").mock(return_value=httpx.Response(401))
    with pytest.raises(XteinkUploadError):
        _client(tmp_path).push_file(f, "a.txt")


def _sig_with_download_url():
    return {**SIG_RESP, "download_url": "https://cdn.example.com/served/x.epub"}


@respx.mock
def test_device_id_prefers_selected_binding(tmp_path):
    respx.get(f"{API}/api/v1/device/binding").mock(
        return_value=httpx.Response(200, json=BINDING_RESP)
    )
    assert _client(tmp_path)._device_id("tok") == "DEV-123"


@respx.mock
def test_device_id_falls_back_to_first_when_none_selected(tmp_path):
    respx.get(f"{API}/api/v1/device/binding").mock(
        return_value=httpx.Response(200, json={"success": True, "data": [
            {"device_id": "DEV-A"}, {"device_id": "DEV-B"},
        ]})
    )
    assert _client(tmp_path)._device_id("tok") == "DEV-A"


@respx.mock
def test_device_id_no_bound_device_raises(tmp_path):
    respx.get(f"{API}/api/v1/device/binding").mock(
        return_value=httpx.Response(200, json={"success": True, "data": []})
    )
    with pytest.raises(XteinkUploadError, match="no bound device"):
        _client(tmp_path)._device_id("tok")


@respx.mock
def test_create_device_task_sends_expected_body(tmp_path):
    route = respx.post(f"{API}/api/v1/device/tasks").mock(
        return_value=httpx.Response(201, json=TASK_RESP)
    )
    tid = _client(tmp_path)._create_device_task(
        "tok", "DEV-9", "https://cdn.example.com/x.epub", "早报_20260901.epub"
    )
    assert tid == "task-1"
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer tok"
    assert json.loads(req.content) == {
        "device_id": "DEV-9",
        "file_url": "https://cdn.example.com/x.epub",
        "save_path": "/Pushed Books/早报_20260901.epub",
        "points_source": "playmethod",
        "func_code": "h5-file-upload",
    }


@respx.mock
def test_create_device_task_without_task_id_raises(tmp_path):
    respx.post(f"{API}/api/v1/device/tasks").mock(
        return_value=httpx.Response(201, json={"success": True, "task": {}})
    )
    with pytest.raises(XteinkUploadError, match="device task"):
        _client(tmp_path)._create_device_task("t", "d", "u", "f.epub")


@respx.mock
def test_create_device_task_non_2xx_raises(tmp_path):
    respx.post(f"{API}/api/v1/device/tasks").mock(
        return_value=httpx.Response(500, text="boom")
    )
    with pytest.raises(XteinkUploadError, match="device task"):
        _client(tmp_path)._create_device_task("t", "d", "u", "f.epub")


@respx.mock
def test_push_file_prefers_signature_download_url(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"data")
    respx.post(f"{API}/auth/login").mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    respx.post(f"{API}/api/v1/upload/signature").mock(
        return_value=httpx.Response(200, json=_sig_with_download_url())
    )
    respx.post("https://oss.example.com").mock(return_value=httpx.Response(204))
    respx.post(f"{API}/api/v1/upload/callback").mock(
        return_value=httpx.Response(200, json={"record_id": "r"})
    )
    _, task_route = _mock_device_push()
    _client(tmp_path).push_file(f, "a.txt")
    body = json.loads(task_route.calls.last.request.content)
    assert body["file_url"] == "https://cdn.example.com/served/x.epub"


@respx.mock
def test_push_file_fails_when_device_push_fails(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"data")
    respx.post(f"{API}/auth/login").mock(return_value=httpx.Response(200, json={"access_token": "t"}))
    respx.post(f"{API}/api/v1/upload/signature").mock(
        return_value=httpx.Response(200, json=SIG_RESP)
    )
    respx.post("https://oss.example.com").mock(return_value=httpx.Response(204))
    respx.post(f"{API}/api/v1/upload/callback").mock(
        return_value=httpx.Response(200, json={"record_id": "r"})
    )
    respx.get(f"{API}/api/v1/device/binding").mock(
        return_value=httpx.Response(200, json=BINDING_RESP)
    )
    respx.post(f"{API}/api/v1/device/tasks").mock(return_value=httpx.Response(502))
    with pytest.raises(XteinkUploadError, match="device task"):
        _client(tmp_path).push_file(f, "a.txt")
