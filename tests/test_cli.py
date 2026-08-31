import shutil
from pathlib import Path

import pytest

from push2xteink.cli import main
from push2xteink.pipeline import RunOutcome

FIXTURE = Path(__file__).parent / "fixtures" / "config_valid.yaml"


def _cfg_file(tmp_path):
    dst = tmp_path / "config.yaml"
    shutil.copy(FIXTURE, dst)
    return dst


def test_list_prints_tasks(tmp_path, capsys):
    rc = main(["--config", str(_cfg_file(tmp_path)), "--db", str(tmp_path / "s.db"), "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "brief" in out and "0 7 * * *" in out


def test_run_success_returns_0(tmp_path, capsys, monkeypatch):
    fake = type("P", (), {
        "__enter__": lambda self: self, "__exit__": lambda *a: None,
        "run_task": lambda self, tid, **kw: RunOutcome(tid, "success", 3, file_name="f.epub", record_id="r"),
    })
    monkeypatch.setattr("push2xteink.cli.Pipeline",
                        type("F", (), {"from_config": staticmethod(lambda *a, **k: fake())}))
    rc = main(["--config", str(_cfg_file(tmp_path)), "--db", str(tmp_path / "s.db"), "run", "brief"])
    assert rc == 0
    assert "success" in capsys.readouterr().out


def test_run_failed_returns_1(tmp_path, capsys, monkeypatch):
    fake = type("P", (), {
        "__enter__": lambda self: self, "__exit__": lambda *a: None,
        "run_task": lambda self, tid, **kw: RunOutcome(tid, "failed", message="boom"),
    })
    monkeypatch.setattr("push2xteink.cli.Pipeline",
                        type("F", (), {"from_config": staticmethod(lambda *a, **k: fake())}))
    rc = main(["--config", str(_cfg_file(tmp_path)), "--db", str(tmp_path / "s.db"), "run", "brief"])
    assert rc == 1
    assert "boom" in capsys.readouterr().err


def test_bad_config_returns_2(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("xteink: [unclosed\n", encoding="utf-8")
    rc = main(["--config", str(bad), "--db", str(tmp_path / "s.db"), "list"])
    assert rc == 2
    assert capsys.readouterr().err


def test_env_var_config_path(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(_cfg_file(tmp_path)))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "s.db"))
    assert main(["list"]) == 0
