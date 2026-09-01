from pathlib import Path

import httpx
import respx

FIX = Path(__file__).parent / "fixtures"

_RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>T</title>
<item><title>One</title><link>https://art.example/1</link><guid>https://art.example/1</guid></item>
<item><title>Two</title><link>https://art.example/2</link><guid>https://art.example/2</guid></item>
</channel></rss>"""


def _chat_ok(content: str = "ok"):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


# --- /api/test/ai ---

@respx.mock
def test_ai_ok(web_client):
    respx.post("https://api.example.com/v1/chat/completions").mock(
        return_value=_chat_ok()
    )
    j = web_client.post("/api/test/ai").json()
    assert j == {"primary": {"ok": True, "error": None}, "fallback": None}


@respx.mock
def test_ai_downstream_failure_is_200(web_client):
    respx.post("https://api.example.com/v1/chat/completions").mock(
        return_value=httpx.Response(500)
    )
    r = web_client.post("/api/test/ai")
    assert r.status_code == 200
    j = r.json()
    assert j["primary"]["ok"] is False and j["primary"]["error"]
    assert j["fallback"] is None


# --- /api/test/xteink ---

@respx.mock
def test_xteink_ok(web_client):
    respx.post("https://api-prod.xteink.cn/auth/login").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1"})
    )
    assert web_client.post("/api/test/xteink").json() == {"ok": True, "error": None}


@respx.mock
def test_xteink_downstream_failure_is_200(web_client):
    respx.post("https://api-prod.xteink.cn/auth/login").mock(
        return_value=httpx.Response(401, json={"message": "bad"})
    )
    r = web_client.post("/api/test/xteink")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is False and j["error"]


# --- /api/test/proxy ---

def test_proxy_none_configured(web_client):
    assert web_client.post("/api/test/proxy").json() == {
        "ok": False,
        "error": "no proxy configured",
    }


@respx.mock
def test_proxy_ok(web_client):
    web_client.put("/api/settings", json={"proxy": {"url": "http://127.0.0.1:7890"}})
    respx.head("https://www.example.com").mock(return_value=httpx.Response(200))
    assert web_client.post("/api/test/proxy").json() == {"ok": True, "error": None}


@respx.mock
def test_proxy_downstream_failure_is_200(web_client):
    web_client.put("/api/settings", json={"proxy": {"url": "http://127.0.0.1:7890"}})
    respx.head("https://www.example.com").mock(side_effect=httpx.ConnectError("x"))
    r = web_client.post("/api/test/proxy")
    assert r.status_code == 200 and r.json()["ok"] is False


# --- /api/feeds/{id}/test ---

@respx.mock
def test_feed_probe_ok(web_client):
    respx.get("https://news.ycombinator.com/rss").mock(
        return_value=httpx.Response(200, content=_RSS)
    )
    long_html = (FIX / "article_long.html").read_bytes()
    respx.get("https://art.example/1").mock(
        return_value=httpx.Response(200, content=long_html)
    )
    respx.get("https://art.example/2").mock(
        return_value=httpx.Response(200, content=long_html)
    )
    j = web_client.post("/api/feeds/hn/test").json()
    assert j["error"] is None
    assert [e["title"] for e in j["entries"]] == ["One", "Two"]
    assert all(e["extracted"] is True for e in j["entries"])


@respx.mock
def test_feed_probe_fetch_failure_is_200(web_client):
    respx.get("https://news.ycombinator.com/rss").mock(
        return_value=httpx.Response(500)
    )
    r = web_client.post("/api/feeds/hn/test")
    assert r.status_code == 200
    j = r.json()
    assert j["error"] and j["entries"] == []


def test_feed_probe_unknown_404(web_client):
    assert web_client.post("/api/feeds/ghost/test").status_code == 404
