from push2xteink.config import load_config


def test_list_feeds(web_client):
    rows = web_client.get("/api/feeds").json()
    assert [f["id"] for f in rows] == ["hn"]


def test_create_feed_201_persisted(web_client, web_env):
    r = web_client.post(
        "/api/feeds", json={"id": "lob", "url": "https://lobste.rs/rss"}
    )
    assert r.status_code == 201 and r.json()["id"] == "lob"
    cfg_path, _ = web_env
    assert [f.id for f in load_config(cfg_path).feeds] == ["hn", "lob"]


def test_create_feed_duplicate_409(web_client):
    r = web_client.post(
        "/api/feeds", json={"id": "hn", "url": "https://news.ycombinator.com/rss"}
    )
    assert r.status_code == 409


def test_update_feed_200(web_client, web_env):
    r = web_client.put("/api/feeds/hn", json={"full_text": False})
    assert r.status_code == 200 and r.json()["full_text"] is False
    cfg_path, _ = web_env
    assert load_config(cfg_path).feeds[0].full_text is False


def test_update_feed_404(web_client):
    assert web_client.put("/api/feeds/ghost", json={"full_text": False}).status_code == 404


def test_delete_feed_referenced_409(web_client):
    r = web_client.delete("/api/feeds/hn")
    assert r.status_code == 409
    assert "brief" in r.json()["detail"]


def test_delete_feed_204(web_client, web_env):
    web_client.post("/api/feeds", json={"id": "lob", "url": "https://lobste.rs/rss"})
    assert web_client.delete("/api/feeds/lob").status_code == 204
    cfg_path, _ = web_env
    assert [f.id for f in load_config(cfg_path).feeds] == ["hn"]


def test_delete_feed_404(web_client):
    assert web_client.delete("/api/feeds/ghost").status_code == 404


# --- I5: a blank / schemeless URL must not be savable from the feeds table ---


def test_update_feed_blank_url_rejected(web_client, web_env):
    r = web_client.put("/api/feeds/hn", json={"url": ""})
    assert r.status_code == 400
    from push2xteink.config import load_config

    cfg_path, _ = web_env
    assert load_config(cfg_path).feeds[0].url == "https://news.ycombinator.com/rss"


def test_page_feed_update_blank_url_shows_error_not_500(web_client, web_env):
    r = web_client.post(
        "/feeds/hn", data={"url": "", "full_text": "true", "use_proxy": "false"}
    )
    assert r.status_code == 200
    assert "url" in r.text  # the feeds table re-renders with an error banner
    from push2xteink.config import load_config

    cfg_path, _ = web_env
    assert load_config(cfg_path).feeds[0].url == "https://news.ycombinator.com/rss"
