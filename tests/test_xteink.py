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
