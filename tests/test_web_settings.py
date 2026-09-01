from push2xteink.config import load_config

MASK = "********"


def test_get_settings_masks_secrets(web_client):
    s = web_client.get("/api/settings").json()
    assert s["xteink"]["password"] == MASK
    assert s["xteink"]["username"] == "15800000000"
    assert s["ai"]["primary"]["api_key"] == MASK
    assert s["proxy"] == {"url": None}


def test_put_settings_keeps_masked_password(web_client, web_env):
    r = web_client.put("/api/settings", json={"xteink": {"password": MASK}})
    assert r.status_code == 200
    cfg_path, _ = web_env
    assert load_config(cfg_path).xteink.password == "secret"


def test_put_settings_updates_real_value(web_client, web_env):
    r = web_client.put("/api/settings", json={"xteink": {"password": "newpass"}})
    assert r.status_code == 200 and r.json()["xteink"]["password"] == MASK
    cfg_path, _ = web_env
    assert load_config(cfg_path).xteink.password == "newpass"


def test_put_settings_bad_proxy_400(web_client, web_env):
    r = web_client.put("/api/settings", json={"proxy": {"url": "not-a-url"}})
    assert r.status_code == 400
    cfg_path, _ = web_env
    assert load_config(cfg_path).proxy.url is None  # not written


def test_put_settings_reload_failure_is_500_and_token_invalidated(
    web_client, web_env, monkeypatch
):
    sched = web_client.app.state.scheduler
    monkeypatch.setattr(sched, "reload", lambda cfg: False)

    r = web_client.put("/api/settings", json={"xteink": {"password": "newpass"}})
    assert r.status_code == 500
    # The token is primed before reload(), so a failed swap must clear it again;
    # otherwise the watcher would never retry the (valid) on-disk config.
    assert sched._config_token is None
    # config is valid, so it IS written to disk (operator sees the 500)
    cfg_path, _ = web_env
    assert load_config(cfg_path).xteink.password == "newpass"


def test_put_settings_sets_proxy(web_client, web_env):
    r = web_client.put(
        "/api/settings", json={"proxy": {"url": "http://127.0.0.1:7890"}}
    )
    assert r.status_code == 200
    cfg_path, _ = web_env
    assert load_config(cfg_path).proxy.url == "http://127.0.0.1:7890"
