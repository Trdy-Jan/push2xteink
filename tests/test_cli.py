import shutil
import sqlite3
import threading
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


def test_list_still_works_without_subcommand_required(tmp_path, capsys):
    rc = main(["--config", str(_cfg_file(tmp_path)), "--db", str(tmp_path / "s.db"), "list"])
    assert rc == 0


class FakeSched:
    instances = []

    def __init__(self, config, state, **kw):
        self.config = config
        self.state = state
        self.started = False
        self.shut = False
        self.reloads = []
        self.enabled_task_ids = ["brief"]
        FakeSched.instances.append(self)

    def start(self):
        self.started = True

    def shutdown(self, **kw):
        self.shut = True

    def reload(self, cfg):
        self.reloads.append(cfg)


@pytest.fixture
def _fake_sched(monkeypatch):
    FakeSched.instances.clear()
    monkeypatch.setattr("push2xteink.cli.Scheduler", FakeSched)
    return FakeSched


def test_serve_starts_and_shuts_down(tmp_path, capsys, _fake_sched):
    cfg = _cfg_file(tmp_path)
    stop = threading.Event()
    stop.set()
    rc = main(
        ["--config", str(cfg), "--db", str(tmp_path / "d" / "s.db"), "serve"],
        _serve_stop=stop,
    )
    assert rc == 0
    inst = _fake_sched.instances[0]
    assert inst.started and inst.shut
    out = capsys.readouterr().out
    assert "scheduler started: 1 task(s): brief" in out


def test_bare_invocation_defaults_to_serve(tmp_path, capsys, _fake_sched):
    cfg = _cfg_file(tmp_path)
    stop = threading.Event()
    stop.set()
    rc = main(["--config", str(cfg), "--db", str(tmp_path / "s.db")], _serve_stop=stop)
    assert rc == 0
    assert _fake_sched.instances[0].started
    assert _fake_sched.instances[0].shut


def test_serve_bad_config_returns_2(tmp_path, capsys, _fake_sched):
    bad = tmp_path / "bad.yaml"
    bad.write_text("x: [unclosed\n", encoding="utf-8")
    stop = threading.Event()
    stop.set()
    rc = main(
        ["--config", str(bad), "--db", str(tmp_path / "s.db"), "serve"],
        _serve_stop=stop,
    )
    assert rc == 2
    assert capsys.readouterr().err
    assert not _fake_sched.instances


def test_serve_unopenable_db_returns_2(tmp_path, capsys, monkeypatch, _fake_sched):
    monkeypatch.setattr(
        "push2xteink.cli.State",
        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("unable to open")),
    )
    stop = threading.Event()
    stop.set()
    rc = main(
        ["--config", str(_cfg_file(tmp_path)), "--db", str(tmp_path / "s.db"), "serve"],
        _serve_stop=stop,
    )
    assert rc == 2
    assert "database error" in capsys.readouterr().err


def test_serve_hot_reloads_on_config_change(tmp_path, capsys, monkeypatch, _fake_sched):
    cfg = _cfg_file(tmp_path)

    # A stop event whose wait() returns False the first time (run the loop body
    # once) then True (exit). No signal handlers touched because _stop is passed.
    calls = {"n": 0}
    real_event = threading.Event()

    class OneShotStop:
        def wait(self, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                # mutate the config file so mtime changes
                import os
                import time

                text = cfg.read_text(encoding="utf-8")
                cfg.write_text(text + "\n# touched\n", encoding="utf-8")
                past = time.time() + 5
                os.utime(cfg, (past, past))
                return False
            return True

        def set(self):
            real_event.set()

    rc = main(
        ["--config", str(cfg), "--db", str(tmp_path / "s.db"), "serve"],
        _serve_stop=OneShotStop(),
    )
    assert rc == 0
    inst = _fake_sched.instances[0]
    assert len(inst.reloads) == 1
