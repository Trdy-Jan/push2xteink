def _seed(db):
    ids = {}
    for tid, status in [("brief", "success"), ("brief", "failed"), ("other", "success")]:
        rid = db.start_run(tid)
        db.finish_run(rid, status=status, item_count=1)
        ids.setdefault(tid, []).append(rid)
    return ids


def test_list_runs_desc(web_client):
    _seed(web_client.app.state.db_state)
    rows = web_client.get("/api/runs").json()
    assert [r["id"] for r in rows] == sorted((r["id"] for r in rows), reverse=True)
    assert len(rows) == 3


def test_list_runs_filter_and_limit(web_client):
    _seed(web_client.app.state.db_state)
    rows = web_client.get("/api/runs", params={"task_id": "brief"}).json()
    assert {r["task_id"] for r in rows} == {"brief"}
    assert len(web_client.get("/api/runs", params={"limit": 1}).json()) == 1


def test_rerun_202_calls_submit(web_client, monkeypatch):
    ids = _seed(web_client.app.state.db_state)
    calls = []
    monkeypatch.setattr(
        web_client.app.state.scheduler, "submit", lambda tid: calls.append(tid)
    )
    run_id = ids["brief"][0]
    r = web_client.post(f"/api/runs/{run_id}/rerun")
    assert r.status_code == 202 and r.json() == {"submitted": "brief"}
    assert calls == ["brief"]


def test_rerun_unknown_404(web_client):
    assert web_client.post("/api/runs/99999/rerun").status_code == 404


def test_cron_preview_valid(web_client):
    j = web_client.get("/api/cron/preview", params={"expr": "0 7 * * *"}).json()
    assert j["valid"] is True and len(j["next"]) == 3 and j["error"] is None
    # the 3 previewed fire times must be distinct and ascending
    assert j["next"] == sorted(j["next"])
    assert len(set(j["next"])) == 3


def test_cron_preview_invalid(web_client):
    j = web_client.get("/api/cron/preview", params={"expr": "nonsense"}).json()
    assert j["valid"] is False and j["next"] == [] and j["error"]
