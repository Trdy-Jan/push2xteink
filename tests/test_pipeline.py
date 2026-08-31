import sqlite3
from pathlib import Path

from push2xteink.pipeline import Pipeline, RunOutcome
from push2xteink.state import State


class FakeSummarizer:
    def __init__(self): self.closed = False
    def summarize(self, text): return f"summary of {text[:10]}"
    def close(self): self.closed = True


class FakeXteink:
    def __init__(self): self.closed = False; self.pushed = []
    def push_file(self, path, filename): self.pushed.append((Path(path).name, filename)); return "rec-1"
    def close(self): self.closed = True


def test_run_outcome_defaults():
    o = RunOutcome(task_id="x", status="skipped")
    assert o.item_count == 0 and o.file_name is None and o.message is None


def test_pipeline_close_closes_collaborators(pipeline_config, tmp_path):
    s = State(tmp_path / "s.db")
    fs, fx = FakeSummarizer(), FakeXteink()
    p = Pipeline(pipeline_config, s, summarizer=fs, xteink_client=fx)
    p.close()
    assert fs.closed and fx.closed
    s.close()


def test_pipeline_context_manager(pipeline_config, tmp_path):
    s = State(tmp_path / "s.db")
    fx = FakeXteink()
    with Pipeline(pipeline_config, s, summarizer=None, xteink_client=fx) as p:
        assert isinstance(p, Pipeline)
    assert fx.closed
    s.close()


def test_from_config_builds_real_collaborators(pipeline_config, tmp_path):
    s = State(tmp_path / "s.db")
    p = Pipeline.from_config(pipeline_config, s)
    from push2xteink.summarize import Summarizer
    from push2xteink.xteink import XteinkClient
    assert isinstance(p._summarizer, Summarizer)   # ai configured
    assert isinstance(p._xteink, XteinkClient)
    p.close()
    s.close()


def test_from_config_no_summarizer_when_ai_absent(pipeline_config, tmp_path):
    cfg = pipeline_config.model_copy(update={"ai": None})
    cfg = cfg.model_copy(update={"tasks": [t.model_copy(update={"summarize": False}) for t in cfg.tasks]})
    s = State(tmp_path / "s.db")
    p = Pipeline.from_config(cfg, s)
    assert p._summarizer is None
    p.close()
    s.close()


# --- Task 2: _gather ---------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from push2xteink.feeds import FeedResult  # noqa: E402
from push2xteink.models import Article  # noqa: E402

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def _art(feed_id, guid):
    # published_at pinned to NOW so first-run lookback filtering keeps these
    # test fixtures (P2 select_new_articles drops undated items on first run).
    return Article(feed_id=feed_id, guid=guid, title="t", link=f"https://x/{guid}",
                   content_html="<p>c</p>", published_at=NOW)


def _pipe(cfg, state):
    return Pipeline(cfg, state, summarizer=FakeSummarizer(), xteink_client=FakeXteink())


def test_gather_collects_new_from_all_feeds(pipeline_config, tmp_path, monkeypatch):
    s = State(tmp_path / "s.db")
    def fake_fetch(feed, **kw):
        return FeedResult(articles=[_art(feed.id, f"{feed.id}1"), _art(feed.id, f"{feed.id}2")])
    monkeypatch.setattr("push2xteink.pipeline.fetch_feed", fake_fetch)

    task = pipeline_config.tasks[0]
    warnings = []
    arts, guids = _pipe(pipeline_config, s)._gather(task, now=NOW, warnings=warnings)
    assert {a.guid for a in arts} == {"a1", "a2", "b1", "b2"}
    assert guids == {"a": ["a1", "a2"], "b": ["b1", "b2"]}
    assert warnings == []
    s.close()


def test_gather_feed_error_is_warned_not_raised(pipeline_config, tmp_path, monkeypatch):
    s = State(tmp_path / "s.db")
    def fake_fetch(feed, **kw):
        if feed.id == "a":
            return FeedResult(error="boom")
        return FeedResult(articles=[_art("b", "b1")])
    monkeypatch.setattr("push2xteink.pipeline.fetch_feed", fake_fetch)
    warnings = []
    arts, guids = _pipe(pipeline_config, s)._gather(pipeline_config.tasks[0], now=NOW, warnings=warnings)
    assert [a.guid for a in arts] == ["b1"]
    assert any("a" in w and "boom" in w for w in warnings)
    s.close()


def test_gather_dedups_across_runs(pipeline_config, tmp_path, monkeypatch):
    s = State(tmp_path / "s.db")
    monkeypatch.setattr("push2xteink.pipeline.fetch_feed",
                        lambda feed, **kw: FeedResult(articles=[_art(feed.id, f"{feed.id}1")]))
    p = _pipe(pipeline_config, s)
    task = pipeline_config.tasks[0]
    first, _ = p._gather(task, now=NOW, warnings=[])
    assert len(first) == 2
    # mark them pushed so they're not pushable again
    s.mark_pushed("a", ["a1"], now=NOW); s.mark_pushed("b", ["b1"], now=NOW)
    second, _ = p._gather(task, now=NOW, warnings=[])
    assert second == []
    s.close()


def test_gather_first_run_lookback_wired_into_select_new_articles(pipeline_config, tmp_path, monkeypatch):
    # _art pins published_at=NOW, so this is the one test that proves _gather
    # computes first_run and passes lookback_hours through to select_new_articles.
    s = State(tmp_path / "s.db")
    task = pipeline_config.tasks[1]  # "plain", feeds=["a"], first_run_lookback_hours=48 (default)

    def fetch_old_and_recent(feed, **kw):
        old = _art(feed.id, "old1").model_copy(update={"published_at": NOW - timedelta(hours=100)})
        recent = _art(feed.id, "recent1").model_copy(update={"published_at": NOW - timedelta(hours=2)})
        return FeedResult(articles=[old, recent])
    monkeypatch.setattr("push2xteink.pipeline.fetch_feed", fetch_old_and_recent)

    p = _pipe(pipeline_config, s)
    first, first_guids = p._gather(task, now=NOW, warnings=[])
    assert [a.guid for a in first] == ["recent1"]          # old one dropped: first run + outside 48h
    assert first_guids == {"a": ["recent1"]}

    # a prior successful run flips first_run to False
    rid = s.start_run(task.id, now=NOW)
    s.finish_run(rid, status="success", now=NOW)

    monkeypatch.setattr(
        "push2xteink.pipeline.fetch_feed",
        lambda feed, **kw: FeedResult(articles=[
            _art(feed.id, "old2").model_copy(update={"published_at": NOW - timedelta(hours=100)})
        ]),
    )
    second, _ = p._gather(task, now=NOW, warnings=[])
    assert [a.guid for a in second] == ["old2"]            # kept now: first_run is False
    s.close()


# --- Task 3: _prepare ------------------------------------------------------


def test_prepare_applies_full_text_concurrently_preserving_order(pipeline_config, tmp_path, monkeypatch):
    s = State(tmp_path / "s.db")
    seen = []
    def fake_aft(article, *, enabled, **kw):
        seen.append((article.guid, enabled))
        return article.model_copy(update={"content_html": f"<p>full {article.guid}</p>",
                                          "content_is_full_text": enabled})
    monkeypatch.setattr("push2xteink.pipeline.apply_full_text", fake_aft)
    arts = [_art("a", "a1"), _art("a", "a2"), _art("b", "b1")]
    out = _pipe(pipeline_config, s)._prepare(pipeline_config.tasks[0], arts, warnings=[])
    assert [a.guid for a in out] == ["a1", "a2", "b1"]           # order preserved
    assert out[0].content_html == "<p>full a1</p>"
    assert dict(seen)["a1"] is True and dict(seen)["b1"] is False  # feed.full_text honored
    s.close()


def test_prepare_summarizes_when_task_wants_it(pipeline_config, tmp_path, monkeypatch):
    s = State(tmp_path / "s.db")
    monkeypatch.setattr("push2xteink.pipeline.apply_full_text", lambda a, **kw: a)
    fs = FakeSummarizer()
    p = Pipeline(pipeline_config, s, summarizer=fs, xteink_client=FakeXteink())
    out = p._prepare(pipeline_config.tasks[0], [_art("a", "a1")], warnings=[])   # tasks[0].summarize=True
    assert out[0].summary and out[0].summary.startswith("summary of")
    s.close()


def test_prepare_no_summary_when_task_opts_out(pipeline_config, tmp_path, monkeypatch):
    s = State(tmp_path / "s.db")
    monkeypatch.setattr("push2xteink.pipeline.apply_full_text", lambda a, **kw: a)
    p = Pipeline(pipeline_config, s, summarizer=FakeSummarizer(), xteink_client=FakeXteink())
    out = p._prepare(pipeline_config.tasks[1], [_art("a", "a1")], warnings=[])   # tasks[1].summarize=False
    assert out[0].summary is None
    s.close()


def test_prepare_warns_when_summarize_wanted_but_no_summarizer(pipeline_config, tmp_path, monkeypatch):
    s = State(tmp_path / "s.db")
    monkeypatch.setattr("push2xteink.pipeline.apply_full_text", lambda a, **kw: a)
    p = Pipeline(pipeline_config, s, summarizer=None, xteink_client=FakeXteink())
    warnings = []
    out = p._prepare(pipeline_config.tasks[0], [_art("a", "a1")], warnings=warnings)  # tasks[0].summarize=True
    assert out[0].summary is None
    assert any("no AI summarizer" in w for w in warnings)
    s.close()


def test_prepare_summary_failure_is_warned_and_skipped(pipeline_config, tmp_path, monkeypatch):
    from push2xteink.summarize import SummarizeError
    s = State(tmp_path / "s.db")
    monkeypatch.setattr("push2xteink.pipeline.apply_full_text", lambda a, **kw: a)
    class BadSummarizer:
        def summarize(self, text): raise SummarizeError("nope")
        def close(self): pass
    p = Pipeline(pipeline_config, s, summarizer=BadSummarizer(), xteink_client=FakeXteink())
    warnings = []
    out = p._prepare(pipeline_config.tasks[0], [_art("a", "a1"), _art("a", "a2")], warnings=warnings)
    assert all(a.summary is None for a in out)
    assert len(warnings) == 2 and all("summary failed" in w for w in warnings)
    s.close()


# --- Task 4: _build -------------------------------------------------------

from push2xteink.builders.common import BuildError  # noqa: E402


def test_build_epub_default_title(pipeline_config, tmp_path):
    s = State(tmp_path / "s.db")
    arts = [_art("a", "a1")]
    # give the article real body so EPUB clears the 256B floor
    arts[0].content_html = "<p>" + "长正文内容。" * 50 + "</p>"
    p = _pipe(pipeline_config, s)
    path = p._build(pipeline_config.tasks[0], arts, now=NOW, out_dir=tmp_path)
    assert path.name == "早报_20260831.epub"
    s.close()


def test_build_txt_for_txt_task(pipeline_config, tmp_path):
    s = State(tmp_path / "s.db")
    p = _pipe(pipeline_config, s)
    path = p._build(pipeline_config.tasks[1], [_art("a", "a1")], now=NOW, out_dir=tmp_path)
    assert path.name == "纯文_20260831.txt" and path.read_text(encoding="utf-8")
    s.close()


def test_build_appends_time_on_same_day_rerun(pipeline_config, tmp_path):
    s = State(tmp_path / "s.db")
    rid = s.start_run("plain", now=NOW)
    s.finish_run(rid, status="success", now=NOW)
    p = _pipe(pipeline_config, s)
    later = NOW.replace(hour=15, minute=30, second=45)
    path = p._build(pipeline_config.tasks[1], [_art("a", "a1")], now=later, out_dir=tmp_path)
    assert path.name == "纯文_20260831_153045.txt"
    s.close()


def test_build_error_propagates(pipeline_config, tmp_path, monkeypatch):
    s = State(tmp_path / "s.db")
    monkeypatch.setattr("push2xteink.pipeline.build_epub",
                        lambda *a, **k: (_ for _ in ()).throw(BuildError("too small")))
    p = _pipe(pipeline_config, s)
    import pytest
    with pytest.raises(BuildError):
        p._build(pipeline_config.tasks[0], [_art("a", "a1")], now=NOW, out_dir=tmp_path)
    s.close()


# --- Task 5: run_task ----------------------------------------------------

import pytest  # noqa: E402

from push2xteink.xteink import XteinkUploadError  # noqa: E402


def _fetch_two(feed, **kw):
    return FeedResult(articles=[_art(feed.id, f"{feed.id}1")])


def _full_pipe(cfg, s, monkeypatch, *, xteink=None):
    monkeypatch.setattr("push2xteink.pipeline.fetch_feed", _fetch_two)
    monkeypatch.setattr("push2xteink.pipeline.apply_full_text",
                        lambda a, **kw: a.model_copy(update={"content_html": "<p>" + "x" * 400 + "</p>"}))
    return Pipeline(cfg, s, summarizer=FakeSummarizer(), xteink_client=xteink or FakeXteink())


def test_run_task_success_marks_pushed_and_writes_run(pipeline_config, tmp_path, monkeypatch):
    s = State(tmp_path / "s.db")
    fx = FakeXteink()
    p = _full_pipe(pipeline_config, s, monkeypatch, xteink=fx)
    out = p.run_task("brief", now=NOW)
    assert out.status == "success" and out.item_count == 2 and out.record_id == "rec-1"
    assert out.file_name == "早报_20260831.epub"
    assert fx.pushed == [("早报_20260831.epub", "早报_20260831.epub")]
    assert s.is_item_pushable("a", "a1", 48, now=NOW) is False  # marked pushed
    assert s.task_has_successful_run("brief") is True
    row = s.recent_runs(1)[0]
    assert row["status"] == "success" and row["item_count"] == 2
    s.close()


def test_run_task_no_new_items_is_skipped(pipeline_config, tmp_path, monkeypatch):
    s = State(tmp_path / "s.db")
    monkeypatch.setattr("push2xteink.pipeline.fetch_feed", lambda feed, **kw: FeedResult(articles=[]))
    p = Pipeline(pipeline_config, s, summarizer=FakeSummarizer(), xteink_client=FakeXteink())
    out = p.run_task("brief", now=NOW)
    assert out.status == "skipped" and out.item_count == 0
    assert s.recent_runs(1)[0]["status"] == "skipped"
    s.close()


def test_run_task_upload_failure_does_not_mark_pushed(pipeline_config, tmp_path, monkeypatch):
    s = State(tmp_path / "s.db")
    class FailXteink:
        def push_file(self, p, f): raise XteinkUploadError("500")
        def close(self): pass
    p = _full_pipe(pipeline_config, s, monkeypatch, xteink=FailXteink())
    out = p.run_task("brief", now=NOW)
    assert out.status == "failed" and "XteinkUploadError" in out.message
    assert s.is_item_pushable("a", "a1", 48, now=NOW) is True   # NOT marked -> retried later
    assert s.recent_runs(1)[0]["status"] == "failed"
    s.close()


def test_run_task_build_failure_is_failed(pipeline_config, tmp_path, monkeypatch):
    s = State(tmp_path / "s.db")
    p = _full_pipe(pipeline_config, s, monkeypatch)
    monkeypatch.setattr("push2xteink.pipeline.build_epub",
                        lambda *a, **k: (_ for _ in ()).throw(BuildError("too small")))
    out = p.run_task("brief", now=NOW)
    assert out.status == "failed" and "BuildError" in out.message
    s.close()


def test_run_task_unexpected_error_is_caught(pipeline_config, tmp_path, monkeypatch):
    s = State(tmp_path / "s.db")
    monkeypatch.setattr("push2xteink.pipeline.fetch_feed",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    p = Pipeline(pipeline_config, s, summarizer=FakeSummarizer(), xteink_client=FakeXteink())
    out = p.run_task("brief", now=NOW)
    assert out.status == "failed" and "unexpected" in out.message
    assert s.recent_runs(1)[0]["status"] == "failed"
    s.close()


def test_run_task_unknown_task_no_run_row(pipeline_config, tmp_path):
    s = State(tmp_path / "s.db")
    p = Pipeline(pipeline_config, s, summarizer=None, xteink_client=FakeXteink())
    out = p.run_task("ghost", now=NOW)
    assert out.status == "failed" and "unknown task" in out.message
    assert s.recent_runs(10) == []
    s.close()


def test_run_task_feed_warning_recorded_on_success(pipeline_config, tmp_path, monkeypatch):
    s = State(tmp_path / "s.db")
    def fetch(feed, **kw):
        return FeedResult(error="down") if feed.id == "b" else FeedResult(articles=[_art("a", "a1")])
    monkeypatch.setattr("push2xteink.pipeline.fetch_feed", fetch)
    monkeypatch.setattr("push2xteink.pipeline.apply_full_text",
                        lambda a, **kw: a.model_copy(update={"content_html": "<p>" + "x" * 400 + "</p>"}))
    p = Pipeline(pipeline_config, s, summarizer=FakeSummarizer(), xteink_client=FakeXteink())
    out = p.run_task("brief", now=NOW)
    assert out.status == "success" and out.item_count == 1
    assert "feed b" in (s.recent_runs(1)[0]["message"] or "")
    s.close()


def test_run_task_naive_now_accepted(pipeline_config, tmp_path, monkeypatch):
    s = State(tmp_path / "s.db")
    p = _full_pipe(pipeline_config, s, monkeypatch)
    out = p.run_task("brief", now=datetime(2026, 8, 31, 12, 0))  # naive
    assert out.status == "success"
    s.close()


# --- C1: run_task must never raise -------------------------------------------


def test_run_task_start_run_failure_returns_failed_no_row(pipeline_config, tmp_path, monkeypatch):
    s = State(tmp_path / "s.db")
    p = _full_pipe(pipeline_config, s, monkeypatch)
    monkeypatch.setattr(s, "start_run",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    out = p.run_task("brief", now=NOW)
    assert out.status == "failed" and "could not start run" in out.message
    assert s.recent_runs(10) == []  # no row written
    s.close()


def test_run_task_finish_run_failure_on_success_does_not_propagate(pipeline_config, tmp_path, monkeypatch):
    s = State(tmp_path / "s.db")
    fx = FakeXteink()
    p = _full_pipe(pipeline_config, s, monkeypatch, xteink=fx)
    monkeypatch.setattr(s, "finish_run",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("boom")))
    out = p.run_task("brief", now=NOW)
    assert out.status == "success" and out.item_count == 2
    assert fx.pushed  # upload still happened
    s.close()


def test_run_task_finish_run_failure_on_failure_path_does_not_propagate(pipeline_config, tmp_path, monkeypatch):
    s = State(tmp_path / "s.db")
    class FailXteink:
        def push_file(self, p, f): raise XteinkUploadError("500")
        def close(self): pass
    p = _full_pipe(pipeline_config, s, monkeypatch, xteink=FailXteink())
    monkeypatch.setattr(s, "finish_run",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("boom")))
    out = p.run_task("brief", now=NOW)
    assert out.status == "failed" and "XteinkUploadError" in out.message
    s.close()
