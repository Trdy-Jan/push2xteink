import time

import httpx
import respx

from push2xteink.http import USER_AGENT, make_client


def test_make_client_never_trusts_env():
    c = make_client(timeout=5)
    assert c.trust_env is False
    assert c.headers["user-agent"] == USER_AGENT
    c.close()


@respx.mock
def test_env_proxy_is_ignored(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://x:1")
    monkeypatch.setenv("HTTP_PROXY", "http://x:1")
    route = respx.get("https://direct.example/ping").mock(
        return_value=httpx.Response(200, text="pong")
    )
    with make_client(proxy=None, timeout=5) as c:
        resp = c.get("https://direct.example/ping")
    assert resp.status_code == 200
    assert route.called
    assert route.calls.last.request.url.host == "direct.example"


def test_client_construction_is_fast():
    start = time.monotonic()
    for _ in range(10):
        make_client(timeout=5).close()
    assert time.monotonic() - start < 1.0


@respx.mock
def test_follow_redirects_default_on():
    respx.get("https://r.example/a").mock(
        return_value=httpx.Response(307, headers={"Location": "https://r.example/b"})
    )
    respx.get("https://r.example/b").mock(return_value=httpx.Response(200, text="ok"))
    with make_client(timeout=5) as c:
        resp = c.get("https://r.example/a")
    assert resp.status_code == 200 and resp.text == "ok"
