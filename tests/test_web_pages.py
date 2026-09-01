"""HTML page routes (Jinja2 + HTMX) — spec §8 页面 table."""
from __future__ import annotations

from push2xteink.config import load_config

# --------------------------------------------------------------------------- #
# Task 9 — task list + task edit
# --------------------------------------------------------------------------- #


def test_index_lists_tasks_and_badge(web_client):
    r = web_client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "早报" in r.text
    # no runs yet -> "未运行"
    assert "未运行" in r.text
    # row action buttons are HTMX-wired
    assert 'hx-post="/tasks/brief/run"' in r.text
    assert 'hx-get="/tasks/brief/edit"' in r.text
    assert 'hx-delete="/tasks/brief"' in r.text
    assert 'hx-post="/tasks/brief/toggle"' in r.text


def test_index_badge_reflects_last_run(web_client):
    db = web_client.app.state.db_state
    rid = db.start_run("brief")
    db.finish_run(rid, status="success", item_count=3, file_name="brief.epub")
    r = web_client.get("/")
    assert "成功" in r.text


def test_tasks_new_form(web_client):
    r = web_client.get("/tasks/new")
    assert r.status_code == 200
    assert 'name="name"' in r.text
    assert 'name="schedule"' in r.text
    # feed checkboxes
    assert 'name="feeds"' in r.text and "hn" in r.text
    # cron preview button
    assert 'hx-get="/api/cron/preview"' in r.text
    assert 'hx-post="/tasks"' in r.text


def test_tasks_edit_prefilled(web_client):
    r = web_client.get("/tasks/brief/edit")
    assert r.status_code == 200
    assert 'value="早报"' in r.text
    assert 'value="0 7 * * *"' in r.text
    assert 'hx-post="/tasks/brief"' in r.text
    # feed "hn" checkbox is checked
    assert "checked" in r.text


def test_tasks_edit_404(web_client):
    assert web_client.get("/tasks/ghost/edit").status_code == 404


def test_post_task_create_persists(web_client, web_env):
    r = web_client.post(
        "/tasks",
        data={
            "name": "午报",
            "feeds": ["hn"],
            "schedule": "0 12 * * *",
            "format": "txt",
            "first_run_lookback_hours": "24",
        },
    )
    assert r.status_code == 200
    assert "午报" in r.text
    cfg_path, _ = web_env
    names = [t.name for t in load_config(cfg_path).tasks]
    assert names == ["早报", "午报"]


def test_post_task_update_persists(web_client, web_env):
    r = web_client.post(
        "/tasks/brief",
        data={
            "name": "晚报",
            "feeds": ["hn"],
            "schedule": "0 20 * * *",
            "format": "epub",
            "first_run_lookback_hours": "48",
        },
    )
    assert r.status_code == 200
    assert "晚报" in r.text
    cfg_path, _ = web_env
    assert load_config(cfg_path).tasks[0].name == "晚报"


def test_post_task_bad_cron_returns_form_with_error(web_client, web_env):
    r = web_client.post(
        "/tasks/brief",
        data={
            "name": "早报",
            "feeds": ["hn"],
            "schedule": "not a cron",
            "format": "epub",
            "first_run_lookback_hours": "48",
        },
    )
    assert r.status_code == 200
    # form partial re-rendered with an error message
    assert 'name="schedule"' in r.text
    assert "cron" in r.text.lower() or "错误" in r.text or "invalid" in r.text.lower()
    # config untouched
    cfg_path, _ = web_env
    assert load_config(cfg_path).tasks[0].schedule == "0 7 * * *"


def test_toggle_flips_and_returns_row(web_client, web_env):
    r = web_client.post("/tasks/brief/toggle")
    assert r.status_code == 200
    assert 'id="task-row-brief"' in r.text
    cfg_path, _ = web_env
    assert load_config(cfg_path).tasks[0].enabled is False


def test_run_calls_submit_and_hints(web_client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        web_client.app.state.scheduler, "submit", lambda tid: calls.append(tid)
    )
    r = web_client.post("/tasks/brief/run")
    assert r.status_code == 200
    assert calls == ["brief"]
    assert "已触发" in r.text


def test_delete_task_removes_from_config(web_client, web_env):
    r = web_client.request("DELETE", "/tasks/brief")
    assert r.status_code == 200
    cfg_path, _ = web_env
    assert load_config(cfg_path).tasks == []


# --------------------------------------------------------------------------- #
# Task 10 — feeds / runs / settings
# --------------------------------------------------------------------------- #


def test_feeds_page_lists_feeds(web_client):
    r = web_client.get("/feeds")
    assert r.status_code == 200
    assert "hn" in r.text
    assert "news.ycombinator.com/rss" in r.text
    assert 'hx-post="/api/feeds/hn/test"' in r.text
    assert 'hx-post="/feeds"' in r.text


def test_post_feed_create_persists(web_client, web_env):
    r = web_client.post(
        "/feeds",
        data={"url": "https://example.com/atom.xml", "full_text": "true"},
    )
    assert r.status_code == 200
    cfg_path, _ = web_env
    urls = [f.url for f in load_config(cfg_path).feeds]
    assert "https://example.com/atom.xml" in urls


def test_delete_referenced_feed_shows_conflict(web_client, web_env):
    r = web_client.request("DELETE", "/feeds/hn")
    assert r.status_code == 200
    assert "brief" in r.text  # names the referencing task
    cfg_path, _ = web_env
    assert [f.id for f in load_config(cfg_path).feeds] == ["hn"]


def test_delete_unreferenced_feed(web_client, web_env):
    web_client.post("/feeds", data={"url": "https://x.example/rss"})
    cfg_path, _ = web_env
    new_id = [f.id for f in load_config(cfg_path).feeds if f.id != "hn"][0]
    r = web_client.request("DELETE", f"/feeds/{new_id}")
    assert r.status_code == 200
    assert [f.id for f in load_config(cfg_path).feeds] == ["hn"]


def test_runs_page_lists_history(web_client):
    db = web_client.app.state.db_state
    rid = db.start_run("brief")
    db.finish_run(rid, status="failed", message="boom: something broke")
    r = web_client.get("/runs")
    assert r.status_code == 200
    assert "早报" in r.text
    assert "失败" in r.text
    assert "boom: something broke" in r.text
    assert f'hx-post="/api/runs/{rid}/rerun"' in r.text


def test_runs_page_autorefreshes_while_running(web_client):
    db = web_client.app.state.db_state
    db.start_run("brief")
    r = web_client.get("/runs")
    assert 'hx-trigger="every 10s"' in r.text


def test_settings_masks_secrets(web_client):
    r = web_client.get("/settings")
    assert r.status_code == 200
    assert "********" in r.text
    assert "secret" not in r.text  # xteink.password
    assert "sk-test" not in r.text  # ai.primary.api_key
    assert 'type="password"' in r.text
    assert 'hx-post="/api/test/xteink"' in r.text
    assert 'hx-post="/api/test/ai"' in r.text
    assert 'hx-post="/api/test/proxy"' in r.text


def test_post_settings_keeps_masked_password(web_client, web_env):
    r = web_client.post(
        "/settings",
        data={
            "xteink.username": "15900000000",
            "xteink.password": "********",
            "xteink.api_base": "https://api-prod.xteink.cn",
            "ai.primary.base_url": "https://api.example.com/v1",
            "ai.primary.api_key": "********",
            "ai.primary.model": "gpt-4o-mini",
            "ai.prompt": "x",
            "ai.timeout_seconds": "60",
            "ai.max_retries": "2",
            "ai.qps": "1.0",
            "fetch.timeout_seconds": "20",
            "fetch.concurrency": "5",
            "proxy.url": "",
        },
    )
    assert r.status_code == 200
    cfg_path, _ = web_env
    cfg = load_config(cfg_path)
    assert cfg.xteink.password == "secret"
    assert cfg.xteink.username == "15900000000"
    assert cfg.ai.primary.api_key == "sk-test"


def test_post_settings_updates_password(web_client, web_env):
    r = web_client.post(
        "/settings",
        data={
            "xteink.username": "15800000000",
            "xteink.password": "brandnew",
            "xteink.api_base": "https://api-prod.xteink.cn",
            "ai.primary.base_url": "https://api.example.com/v1",
            "ai.primary.api_key": "sk-test",
            "ai.primary.model": "gpt-4o-mini",
            "ai.prompt": "x",
            "ai.timeout_seconds": "60",
            "ai.max_retries": "2",
            "ai.qps": "1.0",
            "fetch.timeout_seconds": "20",
            "fetch.concurrency": "5",
            "proxy.url": "",
        },
    )
    assert r.status_code == 200
    assert "已保存" in r.text
    cfg_path, _ = web_env
    assert load_config(cfg_path).xteink.password == "brandnew"
