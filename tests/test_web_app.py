import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
