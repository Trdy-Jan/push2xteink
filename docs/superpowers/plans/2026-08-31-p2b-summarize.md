# P2b 总结（summarize）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 实现 `summarize.py`：对一段文章文本调用 OpenAI 兼容的 chat completions 接口生成中文摘要，primary 失败自动切 fallback，两者都失败则抛 `SummarizeError`；内置 QPS 限流。

**Architecture:** 一个 `Summarizer` 类，构造时吃 `AIConfig` + 代理 URL。`summarize(text) -> str` 走「primary（重试 `max_retries` 次）→ fallback（重试一轮）→ 抛错」。HTTP 用 httpx，`respx` mock 测试。QPS 用一把基于 `threading.Lock` + 单调时钟的最小间隔限流，保证多线程调用也不超速。

**Tech Stack:** Python 3.12、httpx、pytest、respx。

**Spec:** `docs/superpowers/specs/2026-08-31-push2xteink-design.md`（第 4 节 `ai` 段、第 6 节步骤 4、第 13 节 DEFAULT_PROMPT 契约、第 11 节测试策略 summarize 行）

## Global Constraints

- Python `>=3.12`；`X | None` / `list[...]` 标注。
- HTTP 用 `httpx`，显式 `timeout=`；代理 `httpx.Client(proxy=proxy_url)`（`>=0.27` 参数名 `proxy`）。
- `AIConfig` / `AIProvider` 来自 `push2xteink.models`（P1，**不可改**）：
  `AIConfig(primary: AIProvider, fallback: AIProvider | None, use_proxy: bool, prompt: str, timeout_seconds: int, max_retries: int, qps: float)`；
  `AIProvider(base_url: str, api_key: str, model: str)`。P1 已保证 `qps > 0`、`timeout_seconds > 0`、`max_retries >= 0`。
- `DEFAULT_PROMPT` 来自 `push2xteink.models`。
- OpenAI 兼容协议：`POST {base_url}/chat/completions`，头 `Authorization: Bearer {api_key}`，体 `{"model", "messages":[{"role":"system","content":<prompt>},{"role":"user","content":<text>}], "temperature":0.3, "stream":false}`。响应取 `json()["choices"][0]["message"]["content"]`。`base_url` 末尾可能带或不带 `/`——用 `base_url.rstrip("/") + "/chat/completions"`。
- 测试不发真实网络请求（respx mock）。
- 源文件 `src/push2xteink/`，测试 `tests/`。

## Prerequisite

依赖已由 P2 共享 prep commit 加好（httpx、respx）。本计划不改 `pyproject.toml`。

## File Structure

| 文件 | 职责 |
|---|---|
| `src/push2xteink/summarize.py` | `SummarizeError`、`Summarizer`、`build_messages` |
| `tests/test_summarize.py` | 单测 |

## Interfaces（本计划对外产出）

```python
# push2xteink.summarize
class SummarizeError(Exception): ...

def build_messages(prompt: str, text: str, *, max_text_chars: int = 12000) -> list[dict]
    # [{"role":"system","content":prompt}, {"role":"user","content":text[:max_text_chars]}]

class Summarizer:
    def __init__(self, config: AIConfig, *, proxy_url: str | None = None) -> None
        # proxy_url 仅在 config.use_proxy 为 True 时生效；否则内部当作 None
    def summarize(self, text: str) -> str
        # 成功返回模型输出的摘要字符串（strip 后）
        # primary 全部重试失败 → 尝试 fallback（若配置）；再失败 → raise SummarizeError
```

---

## Task 1: `build_messages` + 单 provider 调用

**Files:**
- Create: `src/push2xteink/summarize.py`
- Create: `tests/test_summarize.py`

**Interfaces:**
- Consumes: `push2xteink.models.AIConfig`, `AIProvider`, `DEFAULT_PROMPT`
- Produces: `SummarizeError`、`build_messages(...)`、`Summarizer.__init__`、`Summarizer.summarize`（本任务只需 primary 成功路径 + 单次失败即抛）

- [ ] **Step 1: 写失败测试**

```python
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
```

- [ ] **Step 2: 运行 → 失败**

- [ ] **Step 3: 实现**

```python
from __future__ import annotations

import json
import threading
import time

import httpx

from .models import AIConfig, AIProvider

_UA = {"User-Agent": "push2xteink/0.1"}


class SummarizeError(Exception):
    """primary 与 fallback 均无法产出摘要。"""


def build_messages(prompt: str, text: str, *, max_text_chars: int = 12000) -> list[dict]:
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": text[:max_text_chars]},
    ]


def _call_provider(
    provider: AIProvider, messages: list[dict], *, timeout: float, proxy_url: str | None
) -> str:
    url = provider.base_url.rstrip("/") + "/chat/completions"
    with httpx.Client(proxy=proxy_url, timeout=timeout, headers=_UA) as client:
        resp = client.post(
            url,
            headers={"Authorization": f"Bearer {provider.api_key}"},
            json={
                "model": provider.model,
                "messages": messages,
                "temperature": 0.3,
                "stream": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SummarizeError(f"malformed response: {data!r}") from exc
    if not isinstance(content, str) or not content.strip():
        raise SummarizeError("empty completion content")
    return content.strip()


class Summarizer:
    def __init__(self, config: AIConfig, *, proxy_url: str | None = None) -> None:
        self._cfg = config
        self._proxy = proxy_url if config.use_proxy else None
        self._min_interval = 1.0 / config.qps
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval

    def _try_provider(self, provider: AIProvider, messages: list[dict]) -> str:
        attempts = self._cfg.max_retries + 1
        last: Exception | None = None
        for _ in range(attempts):
            self._throttle()
            try:
                return _call_provider(
                    provider, messages,
                    timeout=self._cfg.timeout_seconds, proxy_url=self._proxy,
                )
            except (httpx.HTTPError, SummarizeError) as exc:
                last = exc
        raise SummarizeError(str(last)) from last

    def summarize(self, text: str) -> str:
        messages = build_messages(self._cfg.prompt, text)
        try:
            return self._try_provider(self._cfg.primary, messages)
        except SummarizeError as primary_exc:
            if self._cfg.fallback is None:
                raise
            try:
                return self._try_provider(self._cfg.fallback, messages)
            except SummarizeError as fb_exc:
                raise SummarizeError(
                    f"primary failed ({primary_exc}); fallback failed ({fb_exc})"
                ) from fb_exc
```

- [ ] **Step 4: 运行 → 全绿。Commit** `feat: Summarizer single-provider path + build_messages`

---

## Task 2: fallback 切换 + 重试计数

**Files:**
- Modify: `tests/test_summarize.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `Summarizer`
- Produces: 无新符号——验证 Task 1 实现的 fallback / 重试语义

- [ ] **Step 1: 追加测试**

```python
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
```

- [ ] **Step 2: 运行 → 全绿**（Task 1 实现应已覆盖；若 `test_retry_count` 或 fallback 失败，按语义修 `_try_provider` / `summarize`）

- [ ] **Step 3: Commit** `test: fallback switching and retry semantics`

---

## Task 3: QPS 限流

**Files:**
- Modify: `tests/test_summarize.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `Summarizer`
- Produces: 无新符号——验证 `qps` 限流

- [ ] **Step 1: 追加测试**

```python
import time


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
```

- [ ] **Step 2: 运行 → 全绿**（Task 1 的 `_throttle` 应已满足；注意 `_next_allowed` 初始 0.0，首次 `time.monotonic()` 通常 >> 0 所以 `wait <= 0` 不 sleep——`test_first_call_not_throttled` 锁定这点）

- [ ] **Step 3: 运行完整套件 → 全绿。Commit** `test: qps throttling`

---

## Self-Review

**Spec coverage:** 第 6 节步骤 4：「调 primary（遵守 qps/timeout/max_retries）→ 异常耗尽重试 → 切 fallback 再试一轮 → 仍失败抛（由 P3 pipeline 捕获后跳过总结）」——Task 1（primary + 抛错）、Task 2（fallback + 重试计数 = max_retries+1）、Task 3（qps）。第 13 节 DEFAULT_PROMPT 契约：`build_messages` 明确 system=prompt、user=正文（截断 12000 字），把「拼装方式」固定下来。第 11 节测试策略三条路径（primary 成功 / primary→fallback / 都失败）均有测试。

**Placeholder scan:** 无桩、无 TODO。`Summarizer.summarize` 在 Task 1 即完整实现（含 fallback），Task 2/3 仅追加测试。

**Type consistency:** `Summarizer.__init__(config, *, proxy_url=None)`、`summarize(text) -> str`、`build_messages(prompt, text, *, max_text_chars=12000)`、`SummarizeError` 在 Interfaces / 实现 / 测试一致。`_call_provider` 内部函数不对外。`time.sleep` / `time.monotonic` 通过模块属性调用（`push2xteink.summarize.time.*`），测试可 monkeypatch。

**并发:** `_throttle` 用一把锁串行化「读时钟→算等待→更新 next_allowed」，多线程下不会突破 qps。`Summarizer` 实例可安全跨线程共享。
