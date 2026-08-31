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

from datetime import datetime, timezone  # noqa: E402

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
