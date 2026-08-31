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
