from push2xteink.config import load_config


def _new_task(**over):
    body = {
        "id": "t2",
        "name": "午报",
        "feeds": ["hn"],
        "schedule": "0 12 * * *",
        "summarize": False,
        "format": "txt",
    }
    body.update(over)
    return body


def test_list_tasks_shape(web_client):
    rows = web_client.get("/api/tasks").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "brief"
    assert row["next_run_time"] is not None  # enabled + scheduled
    assert row["last_run"] is None  # no runs yet


def test_list_tasks_last_run(web_client):
    db = web_client.app.state.db_state
    rid = db.start_run("brief")
    db.finish_run(rid, status="success", item_count=3, file_name="brief.epub")
    row = web_client.get("/api/tasks").json()[0]
    assert row["last_run"]["status"] == "success"
    assert row["last_run"]["item_count"] == 3


def test_create_task_201_and_persisted(web_client, web_env):
    r = web_client.post("/api/tasks", json=_new_task())
    assert r.status_code == 201
    assert r.json()["id"] == "t2"
    cfg_path, _ = web_env
    assert [t.id for t in load_config(cfg_path).tasks] == ["brief", "t2"]


def test_create_task_unknown_feed_400(web_client):
    r = web_client.post("/api/tasks", json=_new_task(feeds=["nope"]))
    assert r.status_code == 400


def test_create_task_bad_cron_400(web_client):
    r = web_client.post("/api/tasks", json=_new_task(schedule="not a cron"))
    assert r.status_code == 400


def test_update_task_200(web_client, web_env):
    r = web_client.put("/api/tasks/brief", json={"name": "晨报"})
    assert r.status_code == 200 and r.json()["name"] == "晨报"
    cfg_path, _ = web_env
    assert load_config(cfg_path).tasks[0].name == "晨报"


def test_update_task_404(web_client):
    assert web_client.put("/api/tasks/ghost", json={"name": "x"}).status_code == 404


def test_delete_task_204(web_client, web_env):
    assert web_client.delete("/api/tasks/brief").status_code == 204
    cfg_path, _ = web_env
    assert load_config(cfg_path).tasks == []


def test_delete_task_404(web_client):
    assert web_client.delete("/api/tasks/ghost").status_code == 404


def test_toggle_task(web_client):
    assert web_client.post("/api/tasks/brief/toggle").json() == {"enabled": False}
    assert web_client.post("/api/tasks/brief/toggle").json() == {"enabled": True}


def test_run_task_202_calls_submit(web_client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        web_client.app.state.scheduler, "submit", lambda tid: calls.append(tid)
    )
    r = web_client.post("/api/tasks/brief/run")
    assert r.status_code == 202 and r.json() == {"submitted": "brief"}
    assert calls == ["brief"]


def test_run_task_404(web_client):
    assert web_client.post("/api/tasks/ghost/run").status_code == 404
