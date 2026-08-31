import shutil
import sqlite3
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


def test_list_does_not_touch_db_when_parent_missing(tmp_path, capsys):
    db = tmp_path / "nope" / "s.db"
    rc = main(["--config", str(_cfg_file(tmp_path)), "--db", str(db), "list"])
    assert rc == 0
    assert "brief" in capsys.readouterr().out
    assert not db.exists() and not db.parent.exists()  # no traceback, nothing created


def test_run_unopenable_db_returns_2(tmp_path, capsys, monkeypatch):
    # make State() raise as if the db could not be opened
    monkeypatch.setattr("push2xteink.cli.State",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("unable to open")))
    rc = main(["--config", str(_cfg_file(tmp_path)), "--db", str(tmp_path / "s.db"), "run", "brief"])
    assert rc == 2
    assert "database error" in capsys.readouterr().err


def test_run_unknown_task_returns_2(tmp_path, capsys):
    rc = main(["--config", str(_cfg_file(tmp_path)), "--db", str(tmp_path / "s.db"), "run", "ghost"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown task" in err and "brief" in err


def test_pipeline_init_failure_returns_2(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("push2xteink.cli.Pipeline",
                        type("F", (), {"from_config": staticmethod(
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad proxy")))}))
    rc = main(["--config", str(_cfg_file(tmp_path)), "--db", str(tmp_path / "s.db"), "run", "brief"])
    assert rc == 2
    assert "pipeline init failed" in capsys.readouterr().err


def test_run_outcome_none_fields_print_empty(tmp_path, capsys, monkeypatch):
    fake = type("P", (), {
        "__enter__": lambda self: self, "__exit__": lambda *a: None,
        "run_task": lambda self, tid, **kw: RunOutcome(tid, "skipped", 0),
    })
    monkeypatch.setattr("push2xteink.cli.Pipeline",
                        type("F", (), {"from_config": staticmethod(lambda *a, **k: fake())}))
    rc = main(["--config", str(_cfg_file(tmp_path)), "--db", str(tmp_path / "s.db"), "run", "brief"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "file= record=" in out and "None" not in out


def test_no_subcommand_returns_2(capsys):
    rc = main([])
    assert rc == 2
    assert "serve" in capsys.readouterr().err


def test_list_still_works_without_subcommand_required(tmp_path, capsys):
    rc = main(["--config", str(_cfg_file(tmp_path)), "--db", str(tmp_path / "s.db"), "list"])
    assert rc == 0
