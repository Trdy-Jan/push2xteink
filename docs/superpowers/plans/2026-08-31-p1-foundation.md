# P1 基础层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现配置模型、配置文件读写、SQLite 状态存储三个基础模块，为后续所有模块提供类型、配置和持久化能力。

**Architecture:** `models.py` 用 pydantic v2 定义所有配置段模型和跨模块共享的 `Article` 类型，含跨字段校验。`config.py` 用 ruamel.yaml 读写 `config.yaml` 并尽量保留注释。`state.py` 用标准库 sqlite3 封装 `seen_items` / `runs` / `kv` 三张表的读写，所有时间戳存 ISO8601 UTC 字符串。

**Tech Stack:** Python 3.12、pydantic v2、ruamel.yaml、APScheduler（仅用于校验 cron 表达式）、pytest、标准库 sqlite3。

**Spec:** `docs/superpowers/specs/2026-08-31-push2xteink-design.md`（见其第 4、5 节）

## Global Constraints

- Python `>=3.12`。类型标注用 `X | None`、`list[...]`、`dict[...]`，不用 `Optional` / `List`。
- pydantic v2 API：`model_validator(mode="after")`、`Config.model_validate(...)`、`.model_dump(...)`。不用 v1 的 `@validator` / `.parse_obj` / `.dict()`。
- 所有写入 SQLite 的时间戳为 ISO8601 UTC 字符串（`datetime.now(timezone.utc).isoformat()`）。读回用 `datetime.fromisoformat(...)`。
- cron 表达式校验统一用 `from apscheduler.triggers.cron import CronTrigger` 的 `CronTrigger.from_crontab(expr)`，捕获 `ValueError`。
- 包源码目录 `src/push2xteink/`，测试目录 `tests/`。
- 每个源文件单一职责；不把无关逻辑塞进来。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `pyproject.toml` | 依赖、构建、pytest 配置（修改现有文件） |
| `src/push2xteink/__init__.py` | 空包标识 |
| `src/push2xteink/models.py` | 所有配置段 pydantic 模型 + `Article` + `DEFAULT_PROMPT` + 跨字段校验 |
| `src/push2xteink/config.py` | `load_config` / `write_config` / `ConfigError`，ruamel.yaml 读写 |
| `src/push2xteink/state.py` | `State` 类：schema 建表 + `seen_items` / `runs` / `kv` 读写 |
| `tests/conftest.py` | 共享 fixture：`valid_config_dict` |
| `tests/fixtures/config_valid.yaml` | 校验用的样例配置文件 |
| `tests/test_models.py` | models 单测 |
| `tests/test_config.py` | config 读写单测 |
| `tests/test_state.py` | state 单测 |

---

## Interfaces（本计划对外产出，后续计划依赖这些签名）

```python
# push2xteink.models
DEFAULT_PROMPT: str

class AIProvider(BaseModel):
    base_url: str
    api_key: str
    model: str

class AIConfig(BaseModel):
    primary: AIProvider
    fallback: AIProvider | None = None
    use_proxy: bool = False
    prompt: str = DEFAULT_PROMPT
    timeout_seconds: int = 60
    max_retries: int = 2
    qps: float = 1.0

class XteinkConfig(BaseModel):
    username: str
    password: str
    api_base: str = "https://api-prod.xteink.cn"

class ProxyConfig(BaseModel):
    url: str | None = None

class FetchConfig(BaseModel):
    timeout_seconds: int = 20
    concurrency: int = 5

class Feed(BaseModel):
    id: str
    url: str
    full_text: bool = True
    use_proxy: bool = False

class Task(BaseModel):
    id: str
    name: str
    feeds: list[str]
    schedule: str
    summarize: bool = False
    format: Literal["epub", "txt"] = "epub"
    enabled: bool = True
    first_run_lookback_hours: int = 48

class Config(BaseModel):
    xteink: XteinkConfig
    feeds: list[Feed]
    tasks: list[Task]
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    ai: AIConfig | None = None

class Article(BaseModel):
    feed_id: str
    guid: str
    title: str
    link: str
    published_at: datetime | None = None
    author: str | None = None
    source_title: str | None = None
    content_html: str = ""
    content_is_full_text: bool = False
    summary: str | None = None

# push2xteink.config
class ConfigError(Exception): ...
def load_config(path: pathlib.Path) -> Config: ...
def write_config(path: pathlib.Path, config: Config) -> None: ...

# push2xteink.state
class State:
    def __init__(self, db_path: pathlib.Path | str) -> None: ...
    def close(self) -> None: ...
    def kv_get(self, key: str) -> str | None: ...
    def kv_set(self, key: str, value: str) -> None: ...
    def record_seen(self, feed_id: str, guid: str, *, now: datetime | None = None) -> None: ...
    def is_item_pushable(self, feed_id: str, guid: str, lookback_hours: int, *, now: datetime | None = None) -> bool: ...
    def mark_pushed(self, feed_id: str, guids: list[str], *, now: datetime | None = None) -> None: ...
    def start_run(self, task_id: str, *, now: datetime | None = None) -> int: ...
    def finish_run(self, run_id: int, *, status: str, item_count: int | None = None,
                   file_name: str | None = None, message: str | None = None,
                   now: datetime | None = None) -> None: ...
    def task_has_successful_run(self, task_id: str) -> bool: ...
    def recent_runs(self, limit: int = 50) -> list[sqlite3.Row]: ...
```

---

## Task 1: 项目骨架与依赖

**Files:**
- Modify: `pyproject.toml`
- Create: `src/push2xteink/__init__.py`
- Create: `tests/__init__.py`

**Interfaces:**
- Consumes: 无
- Produces: 可运行的 `pytest`；包 `push2xteink` 可被 `tests/` 导入（通过 pytest `pythonpath`）

- [ ] **Step 1: 写 pyproject.toml**

覆盖现有 `pyproject.toml` 为：

```toml
[project]
name = "push2xteink"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "pydantic>=2.6",
    "ruamel.yaml>=0.18",
    "apscheduler>=3.10,<4",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/push2xteink"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

- [ ] **Step 2: 创建空包文件**

`src/push2xteink/__init__.py`：

```python
"""push2xteink - RSS 定时聚合并推送到 xteink 阅读器。"""
```

`tests/__init__.py`：空文件。

- [ ] **Step 3: 安装依赖并验证 pytest 可运行**

Run:
```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```
Expected: pytest 启动成功，输出 `no tests ran`（exit code 5）。不报导入或配置错误。

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml src/push2xteink/__init__.py tests/__init__.py
git commit -m "chore: project skeleton and dependencies for P1"
```

---

## Task 2: 配置段模型

**Files:**
- Create: `src/push2xteink/models.py`
- Create: `tests/conftest.py`
- Create: `tests/test_models.py`

**Interfaces:**
- Consumes: 无
- Produces: `AIProvider` `AIConfig` `XteinkConfig` `ProxyConfig` `FetchConfig` `Feed` `Task` `Config` `DEFAULT_PROMPT`（签名见上方 Interfaces 块）

- [ ] **Step 1: 写共享 fixture**

`tests/conftest.py`：

```python
import pytest


@pytest.fixture
def valid_config_dict() -> dict:
    return {
        "xteink": {"username": "15800000000", "password": "secret"},
        "ai": {
            "primary": {
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-test",
                "model": "gpt-4o-mini",
            }
        },
        "feeds": [
            {"id": "hn", "url": "https://news.ycombinator.com/rss"},
            {"id": "blog", "url": "https://example.com/atom.xml", "full_text": False},
        ],
        "tasks": [
            {
                "id": "brief",
                "name": "早报",
                "feeds": ["hn", "blog"],
                "schedule": "0 7 * * *",
                "summarize": True,
            }
        ],
    }
```

- [ ] **Step 2: 写失败测试**

`tests/test_models.py`：

```python
from push2xteink.models import Config, DEFAULT_PROMPT


def test_valid_config_parses(valid_config_dict):
    cfg = Config.model_validate(valid_config_dict)
    assert cfg.xteink.username == "15800000000"
    assert cfg.xteink.api_base == "https://api-prod.xteink.cn"
    assert len(cfg.feeds) == 2
    assert cfg.tasks[0].name == "早报"


def test_config_defaults(valid_config_dict):
    cfg = Config.model_validate(valid_config_dict)
    # feed 默认
    assert cfg.feeds[0].full_text is True
    assert cfg.feeds[0].use_proxy is False
    assert cfg.feeds[1].full_text is False
    # task 默认
    assert cfg.tasks[0].format == "epub"
    assert cfg.tasks[0].enabled is True
    assert cfg.tasks[0].first_run_lookback_hours == 48
    # 顶层默认段
    assert cfg.proxy.url is None
    assert cfg.fetch.timeout_seconds == 20
    assert cfg.fetch.concurrency == 5
    # ai 默认
    assert cfg.ai.fallback is None
    assert cfg.ai.use_proxy is False
    assert cfg.ai.prompt == DEFAULT_PROMPT
    assert cfg.ai.timeout_seconds == 60
    assert cfg.ai.max_retries == 2
    assert cfg.ai.qps == 1.0


def test_ai_optional_when_absent(valid_config_dict):
    valid_config_dict.pop("ai")
    valid_config_dict["tasks"][0]["summarize"] = False
    cfg = Config.model_validate(valid_config_dict)
    assert cfg.ai is None


def test_invalid_format_rejected(valid_config_dict):
    import pytest
    from pydantic import ValidationError

    valid_config_dict["tasks"][0]["format"] = "pdf"
    with pytest.raises(ValidationError):
        Config.model_validate(valid_config_dict)
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/test_models.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'push2xteink.models'`

- [ ] **Step 4: 写实现**

`src/push2xteink/models.py`：

```python
from __future__ import annotations

from datetime import datetime
from typing import Literal

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field, model_validator

DEFAULT_PROMPT = "用中文简洁总结以下文章的核心要点，输出 3-5 条要点，每条一行。"


class AIProvider(BaseModel):
    base_url: str
    api_key: str
    model: str


class AIConfig(BaseModel):
    primary: AIProvider
    fallback: AIProvider | None = None
    use_proxy: bool = False
    prompt: str = DEFAULT_PROMPT
    timeout_seconds: int = 60
    max_retries: int = 2
    qps: float = 1.0


class XteinkConfig(BaseModel):
    username: str
    password: str
    api_base: str = "https://api-prod.xteink.cn"


class ProxyConfig(BaseModel):
    url: str | None = None


class FetchConfig(BaseModel):
    timeout_seconds: int = 20
    concurrency: int = 5


class Feed(BaseModel):
    id: str
    url: str
    full_text: bool = True
    use_proxy: bool = False


class Task(BaseModel):
    id: str
    name: str
    feeds: list[str]
    schedule: str
    summarize: bool = False
    format: Literal["epub", "txt"] = "epub"
    enabled: bool = True
    first_run_lookback_hours: int = 48


class Config(BaseModel):
    xteink: XteinkConfig
    feeds: list[Feed]
    tasks: list[Task]
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    ai: AIConfig | None = None

    @model_validator(mode="after")
    def _cross_field_checks(self) -> "Config":
        feed_ids = [f.id for f in self.feeds]
        dupes = sorted({x for x in feed_ids if feed_ids.count(x) > 1})
        if dupes:
            raise ValueError(f"duplicate feed id(s): {dupes}")

        task_ids = [t.id for t in self.tasks]
        dupes = sorted({x for x in task_ids if task_ids.count(x) > 1})
        if dupes:
            raise ValueError(f"duplicate task id(s): {dupes}")

        known = set(feed_ids)
        for t in self.tasks:
            if not t.feeds:
                raise ValueError(f"task {t.id!r} has no feeds")
            missing = [fid for fid in t.feeds if fid not in known]
            if missing:
                raise ValueError(
                    f"task {t.id!r} references unknown feed(s): {missing}"
                )
            try:
                CronTrigger.from_crontab(t.schedule)
            except ValueError as exc:
                raise ValueError(
                    f"task {t.id!r} has invalid cron {t.schedule!r}: {exc}"
                ) from exc
            if t.summarize and self.ai is None:
                raise ValueError(
                    f"task {t.id!r} has summarize=true but [ai] is not configured"
                )
        return self


class Article(BaseModel):
    feed_id: str
    guid: str
    title: str
    link: str
    published_at: datetime | None = None
    author: str | None = None
    source_title: str | None = None
    content_html: str = ""
    content_is_full_text: bool = False
    summary: str | None = None
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_models.py -q`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add src/push2xteink/models.py tests/conftest.py tests/test_models.py
git commit -m "feat: config-section pydantic models"
```

---

## Task 3: 配置跨字段校验

**Files:**
- Modify: `tests/test_models.py`（追加测试）
- Modify: `src/push2xteink/models.py`（如实现已覆盖则无需改）

**Interfaces:**
- Consumes: Task 2 的 `Config`
- Produces: `Config.model_validate` 对以下情况抛 `pydantic.ValidationError`：重复 feed id、重复 task id、task 引用不存在的 feed、task.feeds 为空、非法 cron、summarize=true 但无 ai 段

- [ ] **Step 1: 追加失败测试**

在 `tests/test_models.py` 末尾追加：

```python
import pytest
from pydantic import ValidationError

from push2xteink.models import Config


def test_duplicate_feed_id_rejected(valid_config_dict):
    valid_config_dict["feeds"].append(
        {"id": "hn", "url": "https://other.example/rss"}
    )
    with pytest.raises(ValidationError, match="duplicate feed id"):
        Config.model_validate(valid_config_dict)


def test_duplicate_task_id_rejected(valid_config_dict):
    valid_config_dict["tasks"].append(
        {
            "id": "brief",
            "name": "夜报",
            "feeds": ["hn"],
            "schedule": "0 22 * * *",
        }
    )
    with pytest.raises(ValidationError, match="duplicate task id"):
        Config.model_validate(valid_config_dict)


def test_task_unknown_feed_rejected(valid_config_dict):
    valid_config_dict["tasks"][0]["feeds"] = ["hn", "ghost"]
    with pytest.raises(ValidationError, match="unknown feed"):
        Config.model_validate(valid_config_dict)


def test_task_empty_feeds_rejected(valid_config_dict):
    valid_config_dict["tasks"][0]["feeds"] = []
    with pytest.raises(ValidationError, match="no feeds"):
        Config.model_validate(valid_config_dict)


def test_invalid_cron_rejected(valid_config_dict):
    valid_config_dict["tasks"][0]["schedule"] = "not a cron"
    with pytest.raises(ValidationError, match="invalid cron"):
        Config.model_validate(valid_config_dict)


def test_summarize_without_ai_rejected(valid_config_dict):
    valid_config_dict.pop("ai")
    # tasks[0].summarize 仍为 True
    with pytest.raises(ValidationError, match="summarize=true"):
        Config.model_validate(valid_config_dict)
```

- [ ] **Step 2: 运行测试**

Run: `python -m pytest tests/test_models.py -q`
Expected: 若 Task 2 的 `_cross_field_checks` 已按上方实现，全部 PASS（10 passed）。若有 FAIL，按报错补齐 `_cross_field_checks` 中对应分支后重跑至全绿。

- [ ] **Step 3: Commit**

```bash
git add tests/test_models.py src/push2xteink/models.py
git commit -m "test: cross-field config validation"
```

---

## Task 4: Article 模型

**Files:**
- Modify: `tests/test_models.py`（追加测试）
- Modify: `src/push2xteink/models.py`（Task 2 已含 `Article`，此处仅补测试；如缺失则补上）

**Interfaces:**
- Consumes: Task 2 的 `Article`
- Produces: `Article` 可用最小字段构造，其余字段有默认值（供 P2a/P2b/P2c/P3 使用）

- [ ] **Step 1: 追加测试**

在 `tests/test_models.py` 末尾追加：

```python
from datetime import datetime, timezone

from push2xteink.models import Article


def test_article_minimal():
    a = Article(feed_id="hn", guid="g1", title="T", link="https://x/1")
    assert a.published_at is None
    assert a.summary is None
    assert a.content_html == ""
    assert a.content_is_full_text is False


def test_article_full():
    a = Article(
        feed_id="hn",
        guid="g1",
        title="T",
        link="https://x/1",
        published_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        author="A",
        source_title="Hacker News",
        content_html="<p>body</p>",
        content_is_full_text=True,
        summary="- 要点",
    )
    assert a.content_is_full_text is True
    assert a.published_at.year == 2026
```

- [ ] **Step 2: 运行测试**

Run: `python -m pytest tests/test_models.py -q`
Expected: 12 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_models.py
git commit -m "test: Article model"
```

---

## Task 5: 配置文件加载

**Files:**
- Create: `src/push2xteink/config.py`
- Create: `tests/fixtures/config_valid.yaml`
- Create: `tests/test_config.py`

**Interfaces:**
- Consumes: Task 2 的 `Config`
- Produces: `load_config(path: Path) -> Config`、`ConfigError`（文件不存在 / YAML 语法错 / 空文件 / 校验失败 均抛 `ConfigError`）

- [ ] **Step 1: 写 fixture 配置文件**

`tests/fixtures/config_valid.yaml`：

```yaml
# push2xteink config
xteink:
  username: "15800000000"
  password: "secret"

ai:
  primary:
    base_url: https://api.example.com/v1
    api_key: sk-test
    model: gpt-4o-mini

feeds:
  - id: hn
    url: https://news.ycombinator.com/rss
    full_text: true

tasks:
  - id: brief
    name: 早报
    feeds: [hn]
    schedule: "0 7 * * *"
    summarize: true
    format: epub
```

- [ ] **Step 2: 写失败测试**

`tests/test_config.py`：

```python
import shutil
from pathlib import Path

import pytest

from push2xteink.config import ConfigError, load_config

FIXTURE = Path(__file__).parent / "fixtures" / "config_valid.yaml"


def test_load_valid(tmp_path):
    dst = tmp_path / "config.yaml"
    shutil.copy(FIXTURE, dst)
    cfg = load_config(dst)
    assert cfg.tasks[0].id == "brief"
    assert cfg.feeds[0].url == "https://news.ycombinator.com/rss"


def test_load_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_load_empty_file(tmp_path):
    dst = tmp_path / "config.yaml"
    dst.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="empty"):
        load_config(dst)


def test_load_bad_yaml(tmp_path):
    dst = tmp_path / "config.yaml"
    dst.write_text("xteink: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(dst)


def test_load_invalid_config(tmp_path):
    dst = tmp_path / "config.yaml"
    dst.write_text(
        "xteink:\n  username: u\n  password: p\nfeeds: []\ntasks: []\n"
        "# task references missing feed below\n",
        encoding="utf-8",
    )
    # feeds/tasks 空是允许的；构造一个真正非法的：task 引用不存在 feed
    dst.write_text(
        "xteink:\n"
        "  username: u\n"
        "  password: p\n"
        "feeds:\n"
        "  - id: a\n"
        "    url: https://a.example/rss\n"
        "tasks:\n"
        "  - id: t\n"
        "    name: T\n"
        "    feeds: [missing]\n"
        "    schedule: '0 7 * * *'\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown feed"):
        load_config(dst)
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/test_config.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'push2xteink.config'`

- [ ] **Step 4: 写实现**

`src/push2xteink/config.py`：

```python
from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .models import Config


class ConfigError(Exception):
    """配置文件缺失、语法错误或校验失败。"""


_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)


def _load_raw(path: Path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    try:
        data = _yaml.load(text)
    except YAMLError as exc:
        raise ConfigError(f"failed to parse YAML {path}: {exc}") from exc
    if data is None:
        raise ConfigError(f"config file is empty: {path}")
    return data


def _parse(raw) -> Config:
    try:
        return Config.model_validate(dict(raw))
    except ValidationError as exc:
        raise ConfigError(f"invalid config:\n{exc}") from exc


def load_config(path: Path) -> Config:
    return _parse(_load_raw(path))


def write_config(path: Path, config: Config) -> None:
    raise NotImplementedError  # Task 6
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_config.py -q`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add src/push2xteink/config.py tests/fixtures/config_valid.yaml tests/test_config.py
git commit -m "feat: load_config with ruamel.yaml"
```

---

## Task 6: 配置文件写回

**Files:**
- Modify: `src/push2xteink/config.py`
- Modify: `tests/test_config.py`（追加测试）

**Interfaces:**
- Consumes: Task 2 的 `Config`、Task 5 的 `_load_raw`
- Produces: `write_config(path: Path, config: Config) -> None` —— 原子写入（写 `.tmp` 再 `replace`）；已存在文件时保留文件头注释；值往返一致

- [ ] **Step 1: 追加失败测试**

在 `tests/test_config.py` 末尾追加：

```python
from push2xteink.config import write_config


def test_write_roundtrip_values(tmp_path):
    dst = tmp_path / "config.yaml"
    shutil.copy(FIXTURE, dst)
    cfg = load_config(dst)
    cfg.tasks[0].name = "晨间简报"
    cfg.feeds.append(
        type(cfg.feeds[0])(id="lobsters", url="https://lobste.rs/rss")
    )
    write_config(dst, cfg)

    reloaded = load_config(dst)
    assert reloaded.tasks[0].name == "晨间简报"
    assert [f.id for f in reloaded.feeds] == ["hn", "lobsters"]
    assert reloaded.feeds[1].full_text is True  # 默认值回填


def test_write_preserves_header_comment(tmp_path):
    dst = tmp_path / "config.yaml"
    shutil.copy(FIXTURE, dst)
    cfg = load_config(dst)
    write_config(dst, cfg)
    assert "# push2xteink config" in dst.read_text(encoding="utf-8")


def test_write_creates_new_file(tmp_path):
    dst = tmp_path / "fresh.yaml"
    src_cfg = load_config_fixture(tmp_path)
    write_config(dst, src_cfg)
    assert dst.exists()
    assert load_config(dst).tasks[0].id == "brief"


def load_config_fixture(tmp_path):
    tmp = tmp_path / "_src.yaml"
    shutil.copy(FIXTURE, tmp)
    return load_config(tmp)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_config.py -q`
Expected: FAIL —— `write_config` 抛 `NotImplementedError`

- [ ] **Step 3: 写实现**

替换 `config.py` 中 `write_config` 的 body：

```python
def write_config(path: Path, config: Config) -> None:
    path = Path(path)
    if path.exists():
        raw = _load_raw(path)
    else:
        from ruamel.yaml.comments import CommentedMap

        raw = CommentedMap()

    payload = config.model_dump(mode="python", exclude_none=True)
    if payload.get("proxy") in ({}, {"url": None}):
        payload.pop("proxy", None)

    for key, value in payload.items():
        raw[key] = value
    for key in [k for k in raw.keys() if k not in payload]:
        del raw[key]

    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        _yaml.dump(raw, fh)
    tmp.replace(path)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_config.py -q`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/push2xteink/config.py tests/test_config.py
git commit -m "feat: write_config atomic round-trip preserving header comments"
```

---

## Task 7: 状态存储 schema 与 kv 表

**Files:**
- Create: `src/push2xteink/state.py`
- Create: `tests/test_state.py`

**Interfaces:**
- Consumes: 无
- Produces: `State(db_path)` 构造时建表（幂等）；`kv_get` / `kv_set`（upsert）；`close`

- [ ] **Step 1: 写失败测试**

`tests/test_state.py`：

```python
from push2xteink.state import State


def test_creates_tables_idempotently(tmp_path):
    db = tmp_path / "state.db"
    State(db).close()
    # 再次打开不报错
    s = State(db)
    s.close()


def test_kv_roundtrip(tmp_path):
    s = State(tmp_path / "state.db")
    assert s.kv_get("token") is None
    s.kv_set("token", "abc")
    assert s.kv_get("token") == "abc"
    s.kv_set("token", "def")
    assert s.kv_get("token") == "def"
    s.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_state.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'push2xteink.state'`

- [ ] **Step 3: 写实现**

`src/push2xteink/state.py`：

```python
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_items (
  feed_id       TEXT NOT NULL,
  item_guid     TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  pushed_at     TEXT,
  PRIMARY KEY (feed_id, item_guid)
);
CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  status      TEXT NOT NULL,
  item_count  INTEGER,
  file_name   TEXT,
  message     TEXT
);
CREATE TABLE IF NOT EXISTS kv (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  updated_at TEXT
);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class State:
    def __init__(self, db_path: Path | str) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- kv ---
    def kv_get(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM kv WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, _iso(_utcnow())),
        )
        self._conn.commit()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_state.py -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/push2xteink/state.py tests/test_state.py
git commit -m "feat: State schema and kv store"
```

---

## Task 8: seen_items 去重逻辑

**Files:**
- Modify: `src/push2xteink/state.py`
- Modify: `tests/test_state.py`（追加测试）

**Interfaces:**
- Consumes: Task 7 的 `State`
- Produces:
  - `record_seen(feed_id, guid, *, now=None)` —— `INSERT OR IGNORE`
  - `is_item_pushable(feed_id, guid, lookback_hours, *, now=None) -> bool` —— guid 不存在 → True；已存在且 `pushed_at` 非空 → False；已存在且 `pushed_at` 为空且 `first_seen_at >= now - lookback_hours` → True，否则 False
  - `mark_pushed(feed_id, guids, *, now=None)` —— 批量设 `pushed_at`

- [ ] **Step 1: 追加失败测试**

在 `tests/test_state.py` 末尾追加：

```python
from datetime import datetime, timedelta, timezone

import pytest

from push2xteink.state import State

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def test_new_guid_is_pushable(tmp_path):
    s = State(tmp_path / "s.db")
    assert s.is_item_pushable("hn", "g1", 48, now=NOW) is True
    s.close()


def test_record_seen_is_idempotent(tmp_path):
    s = State(tmp_path / "s.db")
    s.record_seen("hn", "g1", now=NOW)
    s.record_seen("hn", "g1", now=NOW + timedelta(hours=1))
    row = s._conn.execute(
        "SELECT first_seen_at FROM seen_items WHERE feed_id='hn' AND item_guid='g1'"
    ).fetchone()
    assert row["first_seen_at"] == NOW.isoformat()
    s.close()


def test_seen_unpushed_within_window_is_pushable(tmp_path):
    s = State(tmp_path / "s.db")
    s.record_seen("hn", "g1", now=NOW - timedelta(hours=10))
    assert s.is_item_pushable("hn", "g1", 48, now=NOW) is True
    s.close()


def test_seen_unpushed_outside_window_not_pushable(tmp_path):
    s = State(tmp_path / "s.db")
    s.record_seen("hn", "g1", now=NOW - timedelta(hours=60))
    assert s.is_item_pushable("hn", "g1", 48, now=NOW) is False
    s.close()


def test_pushed_item_not_pushable(tmp_path):
    s = State(tmp_path / "s.db")
    s.record_seen("hn", "g1", now=NOW - timedelta(hours=1))
    s.mark_pushed("hn", ["g1"], now=NOW)
    assert s.is_item_pushable("hn", "g1", 48, now=NOW + timedelta(hours=1)) is False
    s.close()


def test_mark_pushed_only_affects_listed_guids(tmp_path):
    s = State(tmp_path / "s.db")
    s.record_seen("hn", "g1", now=NOW)
    s.record_seen("hn", "g2", now=NOW)
    s.mark_pushed("hn", ["g1"], now=NOW)
    assert s.is_item_pushable("hn", "g1", 48, now=NOW) is False
    assert s.is_item_pushable("hn", "g2", 48, now=NOW) is True
    s.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_state.py -q`
Expected: FAIL —— `AttributeError: 'State' object has no attribute 'record_seen'`

- [ ] **Step 3: 写实现**

在 `State` 类中追加方法：

```python
    # --- seen_items ---
    def record_seen(
        self, feed_id: str, guid: str, *, now: datetime | None = None
    ) -> None:
        ts = _iso(now or _utcnow())
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_items(feed_id, item_guid, first_seen_at) "
            "VALUES(?, ?, ?)",
            (feed_id, guid, ts),
        )
        self._conn.commit()

    def is_item_pushable(
        self,
        feed_id: str,
        guid: str,
        lookback_hours: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        now = now or _utcnow()
        row = self._conn.execute(
            "SELECT first_seen_at, pushed_at FROM seen_items "
            "WHERE feed_id = ? AND item_guid = ?",
            (feed_id, guid),
        ).fetchone()
        if row is None:
            return True
        if row["pushed_at"] is not None:
            return False
        first_seen = datetime.fromisoformat(row["first_seen_at"])
        return first_seen >= now - timedelta(hours=lookback_hours)

    def mark_pushed(
        self, feed_id: str, guids: list[str], *, now: datetime | None = None
    ) -> None:
        ts = _iso(now or _utcnow())
        self._conn.executemany(
            "UPDATE seen_items SET pushed_at = ? "
            "WHERE feed_id = ? AND item_guid = ?",
            [(ts, feed_id, g) for g in guids],
        )
        self._conn.commit()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_state.py -q`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/push2xteink/state.py tests/test_state.py
git commit -m "feat: seen_items dedup with retry window"
```

---

## Task 9: runs 执行记录

**Files:**
- Modify: `src/push2xteink/state.py`
- Modify: `tests/test_state.py`（追加测试）

**Interfaces:**
- Consumes: Task 7 的 `State`
- Produces:
  - `start_run(task_id, *, now=None) -> int` —— 插入 `status='running'` 行，返回 `id`
  - `finish_run(run_id, *, status, item_count=None, file_name=None, message=None, now=None)`
  - `task_has_successful_run(task_id) -> bool` —— 是否存在 `status='success'` 的 run
  - `recent_runs(limit=50) -> list[sqlite3.Row]` —— 按 `id` 倒序

- [ ] **Step 1: 追加失败测试**

在 `tests/test_state.py` 末尾追加：

```python
def test_run_lifecycle(tmp_path):
    s = State(tmp_path / "s.db")
    rid = s.start_run("brief", now=NOW)
    assert isinstance(rid, int)
    assert s.task_has_successful_run("brief") is False

    s.finish_run(
        rid, status="success", item_count=3, file_name="早报_20260831.epub",
        now=NOW + timedelta(minutes=2),
    )
    assert s.task_has_successful_run("brief") is True

    row = s.recent_runs(10)[0]
    assert row["status"] == "success"
    assert row["item_count"] == 3
    assert row["file_name"] == "早报_20260831.epub"
    assert row["finished_at"] == (NOW + timedelta(minutes=2)).isoformat()
    s.close()


def test_failed_run_does_not_count_as_success(tmp_path):
    s = State(tmp_path / "s.db")
    rid = s.start_run("brief", now=NOW)
    s.finish_run(rid, status="failed", message="upload 500", now=NOW)
    assert s.task_has_successful_run("brief") is False
    s.close()


def test_recent_runs_ordered_desc(tmp_path):
    s = State(tmp_path / "s.db")
    r1 = s.start_run("a", now=NOW)
    r2 = s.start_run("b", now=NOW + timedelta(minutes=1))
    ids = [row["id"] for row in s.recent_runs(10)]
    assert ids == [r2, r1]
    s.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_state.py -q`
Expected: FAIL —— `AttributeError: 'State' object has no attribute 'start_run'`

- [ ] **Step 3: 写实现**

在 `State` 类中追加方法：

```python
    # --- runs ---
    def start_run(self, task_id: str, *, now: datetime | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO runs(task_id, started_at, status) VALUES(?, ?, 'running')",
            (task_id, _iso(now or _utcnow())),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        item_count: int | None = None,
        file_name: str | None = None,
        message: str | None = None,
        now: datetime | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, item_count = ?, "
            "file_name = ?, message = ? WHERE id = ?",
            (_iso(now or _utcnow()), status, item_count, file_name, message, run_id),
        )
        self._conn.commit()

    def task_has_successful_run(self, task_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM runs WHERE task_id = ? AND status = 'success' LIMIT 1",
            (task_id,),
        ).fetchone()
        return row is not None

    def recent_runs(self, limit: int = 50) -> list:
        return self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_state.py -q`
Expected: 11 passed

- [ ] **Step 5: 跑完整测试套件**

Run: `python -m pytest -q`
Expected: 全部 PASS（models 12 + config 8 + state 11 = 31 passed）

- [ ] **Step 6: Commit**

```bash
git add src/push2xteink/state.py tests/test_state.py
git commit -m "feat: runs tracking in State"
```

---

## Self-Review

**1. Spec coverage（第 4、5 节）：**
- 第 4 节 config.yaml 所有段（xteink / proxy / ai.primary / ai.fallback / ai.use_proxy / ai.prompt / ai.timeout / ai.retries / ai.qps / fetch / feeds[].full_text / feeds[].use_proxy / tasks[].*）→ Task 2 全部建模。
- 第 4 节校验规则（feed id 唯一、task id 唯一、feeds 引用存在、cron 合法、summarize 需 ai、format 枚举）→ Task 2 + Task 3。
- 第 4 节「Web 与手工编辑读写同一文件、保留注释」→ Task 5（load）+ Task 6（write，保留文件头注释；列表项内联注释不保证，已在 spec 第 8 节接受此限制）。
- 第 5 节三张表 DDL（seen_items / runs / kv）→ Task 7。
- 第 5 节去重逻辑（第 6 节精化版：未推送条目窗口内可重试）→ Task 8 `is_item_pushable`。
- 第 5 节 token 缓存放 kv 表 → Task 7 `kv_get`/`kv_set`（P2d 使用 `xteink_access_token` 等 key）。
- 第 5 节 runs 字段与「首次执行」判定 → Task 9 `task_has_successful_run`。

**2. Placeholder scan：** 无 TBD / TODO / “适当处理”。Task 5 的 `write_config` 临时 body 为 `raise NotImplementedError` 并明确标注「Task 6」，Task 6 Step 3 给出完整替换实现——非占位符。

**3. Type consistency：** `is_item_pushable` / `record_seen` / `mark_pushed` / `start_run` / `finish_run` 签名在 Interfaces 块、各 Task 的 Produces、实现代码、测试调用四处一致。`Config` / `Article` 字段名在 Task 2 实现与 Task 3/4 测试一致。`load_config` / `write_config` / `ConfigError` 名称一致。

**4. 依赖顺序：** Task 1 → 2 → (3,4) → 5 → 6；Task 7 → 8 → 9。Task 2 引入 `apscheduler` 仅为 cron 校验，已在 Task 1 依赖中列出。
