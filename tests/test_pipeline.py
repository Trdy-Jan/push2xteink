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
