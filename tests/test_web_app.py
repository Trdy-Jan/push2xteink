import shutil
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from push2xteink.config import load_config
from push2xteink.state import State
from push2xteink.web.app import create_app

FIXTURE = Path("tests/fixtures/config_valid.yaml")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    cfg = tmp_path / "config.yaml"
    shutil.copy(FIXTURE, cfg)
    app = create_app(cfg, tmp_path / "state.db")
    with TestClient(app) as c:  # triggers lifespan (scheduler start/stop)
        yield c


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_index_renders_task_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "早报" in r.text


def test_static_htmx_served(client):
    assert client.get("/static/htmx.min.js").status_code == 200


def test_basic_auth_enforced_when_password_set(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_PASSWORD", "s3cret")
    cfg = tmp_path / "config.yaml"
    shutil.copy(FIXTURE, cfg)
    with TestClient(create_app(cfg, tmp_path / "s.db")) as c:
        assert c.get("/").status_code == 401
        assert c.get("/", auth=("x", "wrong")).status_code == 401
        assert c.get("/", auth=("x", "s3cret")).status_code == 200
        # API routes are guarded too
        assert c.get("/api/tasks").status_code == 401
        assert c.get("/api/tasks", auth=("x", "s3cret")).status_code == 200


def test_basic_auth_non_ascii_password_is_401_not_500(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_PASSWORD", "s3cret")
    cfg = tmp_path / "config.yaml"
    shutil.copy(FIXTURE, cfg)
    with TestClient(create_app(cfg, tmp_path / "s.db")) as c:
        assert c.get("/", auth=("x", "wrong-üñî")).status_code == 401


# --- C2: auth must cover the non-APIRoute surface (docs, schema, mounts) ---

_GUARDED = ("/openapi.json", "/docs", "/redoc", "/static/htmx.min.js", "/api/tasks", "/")


def test_basic_auth_covers_docs_schema_and_static(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_PASSWORD", "s3cret")
    cfg = tmp_path / "config.yaml"
    shutil.copy(FIXTURE, cfg)
    with TestClient(create_app(cfg, tmp_path / "s.db")) as c:
        for path in _GUARDED:
            assert c.get(path).status_code == 401, f"{path} unauthenticated!"
            assert c.get(path, auth=("x", "s3cret")).status_code == 200, path


def test_healthz_is_exempt_from_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_PASSWORD", "s3cret")
    cfg = tmp_path / "config.yaml"
    shutil.copy(FIXTURE, cfg)
    with TestClient(create_app(cfg, tmp_path / "s.db")) as c:
        r = c.get("/healthz")
        assert r.status_code == 200 and r.json() == {"ok": True}


def test_malformed_authorization_header_is_401(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_PASSWORD", "s3cret")
    cfg = tmp_path / "config.yaml"
    shutil.copy(FIXTURE, cfg)
    with TestClient(create_app(cfg, tmp_path / "s.db")) as c:
        for hdr in ("Basic !!!not-base64!!!", "Basic ", "Bearer s3cret", "Basic eA=="):
            assert c.get("/", headers={"Authorization": hdr}).status_code == 401, hdr


# --- I6: CSRF / same-origin guard on mutating requests ---


def test_cross_origin_post_is_refused(client):
    r = client.post(
        "/api/tasks",
        json={"name": "x", "feeds": ["hn"], "schedule": "0 7 * * *"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert "cross-origin" in r.text


def test_cross_origin_delete_is_refused(client):
    r = client.delete("/tasks/brief", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_same_origin_post_proceeds(client):
    host = client.get("/healthz").request.headers["host"]
    r = client.post(
        "/tasks/brief/toggle", headers={"Origin": f"http://{host}"}
    )
    assert r.status_code == 200


def test_cross_origin_get_is_allowed(client):
    # Reads are not state-changing; a cross-origin GET must not break embedding.
    r = client.get("/", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200


# --- I2/I3: config-change serialization and token priming ---


def test_lifespan_uses_shorter_web_drain_timeout(client):
    assert client.app.state.scheduler._drain_timeout == 25.0


def test_lifespan_installs_config_lock(client):
    assert isinstance(client.app.state.config_lock, type(threading.Lock()))


def test_lifespan_closes_partial_startup_on_failure(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    shutil.copy(FIXTURE, cfg)
    closed = []
    real_state_close = State.close

    def spy_close(self):
        closed.append(self)
        real_state_close(self)

    monkeypatch.setattr(State, "close", spy_close)
    monkeypatch.setattr(
        "push2xteink.web.app.Scheduler",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    app = create_app(cfg, tmp_path / "s.db")
    with pytest.raises(RuntimeError, match="boom"):
        with TestClient(app):
            pass
    assert closed, "State was left open after a failed startup"


def test_concurrent_config_changes_do_not_lose_updates(web_client, web_env):
    cfg_path, _ = web_env
    barrier = threading.Barrier(2)
    results = []

    def put(section_body):
        barrier.wait(timeout=10)
        results.append(web_client.put("/api/settings", json=section_body).status_code)

    threads = [
        threading.Thread(target=put, args=({"xteink": {"username": "aaa"}},)),
        threading.Thread(target=put, args=({"fetch": {"concurrency": 9}},)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    assert results == [200, 200]
    on_disk = load_config(cfg_path)
    live = web_client.app.state.scheduler.config
    # No lost update: both writes landed.
    assert on_disk.xteink.username == "aaa"
    assert on_disk.fetch.concurrency == 9
    # And no desync between the file and the running scheduler.
    assert live.model_dump() == on_disk.model_dump()
