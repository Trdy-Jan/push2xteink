import httpx
import pytest
import respx

from push2xteink.models import AIConfig, AIProvider
from push2xteink.summarize import SummarizeError, Summarizer, build_messages


def _cfg(**over):
    base = dict(
        primary=AIProvider(base_url="https://api.primary/v1", api_key="k1", model="m1"),
        qps=1000.0,  # effectively no throttle in tests
        max_retries=1,
    )
    base.update(over)
    return AIConfig(**base)


def _chat_ok(text="- 要点一\n- 要点二"):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def test_build_messages_shape_and_truncation():
    msgs = build_messages("SYS", "x" * 20000, max_text_chars=100)
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[1]["role"] == "user" and len(msgs[1]["content"]) == 100


@respx.mock
def test_summarize_primary_success():
    route = respx.post("https://api.primary/v1/chat/completions").mock(return_value=_chat_ok())
    out = Summarizer(_cfg()).summarize("long article body")
    assert out == "- 要点一\n- 要点二"
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer k1"
    body = __import__("json").loads(route.calls.last.request.content)
    assert body["model"] == "m1"
    assert body["messages"][0]["role"] == "system"


@respx.mock
def test_summarize_no_fallback_raises_after_retries():
    respx.post("https://api.primary/v1/chat/completions").mock(return_value=httpx.Response(500))
    with pytest.raises(SummarizeError):
        Summarizer(_cfg(max_retries=2)).summarize("body")


@respx.mock
def test_summarize_strips_and_handles_empty_choice():
    respx.post("https://api.primary/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "  text  "}}]})
    )
    assert Summarizer(_cfg()).summarize("body") == "text"


@respx.mock
def test_summarize_malformed_response_raises():
    respx.post("https://api.primary/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"nope": 1})
    )
    with pytest.raises(SummarizeError):
        Summarizer(_cfg(max_retries=0)).summarize("body")


@respx.mock
def test_primary_fails_fallback_succeeds():
    respx.post("https://api.primary/v1/chat/completions").mock(return_value=httpx.Response(500))
    fb = respx.post("https://api.fallback/v1/chat/completions").mock(return_value=_chat_ok("摘要"))
    cfg = _cfg(
        fallback=AIProvider(base_url="https://api.fallback/v1", api_key="k2", model="m2"),
        max_retries=1,
    )
    assert Summarizer(cfg).summarize("body") == "摘要"
    assert fb.called


@respx.mock
def test_both_fail_raises_with_both_messages():
    respx.post("https://api.primary/v1/chat/completions").mock(return_value=httpx.Response(500))
    respx.post("https://api.fallback/v1/chat/completions").mock(side_effect=httpx.ConnectError("x"))
    cfg = _cfg(
        fallback=AIProvider(base_url="https://api.fallback/v1", api_key="k2", model="m2"),
        max_retries=0,
    )
    with pytest.raises(SummarizeError, match="primary failed.*fallback failed"):
        Summarizer(cfg).summarize("body")


@respx.mock
def test_retry_count_is_max_retries_plus_one():
    route = respx.post("https://api.primary/v1/chat/completions").mock(return_value=httpx.Response(503))
    with pytest.raises(SummarizeError):
        Summarizer(_cfg(max_retries=2)).summarize("body")
    assert route.call_count == 3


@respx.mock
def test_transient_then_success_within_retries():
    route = respx.post("https://api.primary/v1/chat/completions")
    route.side_effect = [httpx.Response(503), _chat_ok("ok")]
    assert Summarizer(_cfg(max_retries=2)).summarize("body") == "ok"
    assert route.call_count == 2


@respx.mock
def test_qps_throttles_sequential_calls(monkeypatch):
    respx.post("https://api.primary/v1/chat/completions").mock(return_value=_chat_ok("x"))
    sleeps: list[float] = []
    monkeypatch.setattr("push2xteink.summarize.time.sleep", lambda s: sleeps.append(s))

    clock = {"t": 0.0}
    monkeypatch.setattr("push2xteink.summarize.time.monotonic", lambda: clock["t"])

    s = Summarizer(_cfg(qps=2.0))  # min interval 0.5s
    s.summarize("a")               # first call: no wait
    s.summarize("b")               # second call at same clock -> must request 0.5s sleep
    assert sleeps and abs(sleeps[0] - 0.5) < 1e-6


@respx.mock
def test_first_call_not_throttled(monkeypatch):
    respx.post("https://api.primary/v1/chat/completions").mock(return_value=_chat_ok("x"))
    sleeps: list[float] = []
    monkeypatch.setattr("push2xteink.summarize.time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr("push2xteink.summarize.time.monotonic", lambda: 1000.0)
    Summarizer(_cfg(qps=1.0)).summarize("a")
    assert sleeps == []
