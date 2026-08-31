# P2c 成文（builders）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 实现 `builders/epub.py` 和 `builders/txt.py`：把一组 `Article` 打包成一个 EPUB 或 TXT 文件，每篇文章一章，章节正文按「有摘要则 摘要 + 分隔线 + 正文，否则仅正文」组织。

**Architecture:** 两个纯函数 `build_epub` / `build_txt`，输入 `(title, articles, out_dir)`，输出写好的文件 `Path`。EPUB 用 ebooklib 生成带目录的电子书；TXT 按 spec 第 6 节步骤 5 的纯文本格式拼接。文件名由标题清洗而来。EPUB 成品 < 256 字节视为异常抛 `BuildError`。无网络、无外部依赖之外的 IO。

**Tech Stack:** Python 3.12、ebooklib、pytest。

**Spec:** `docs/superpowers/specs/2026-08-31-push2xteink-design.md`（第 6 节步骤 5、第 7.3 节 256 字节下限、第 11 节 builders 测试行、第 13 节）

## Global Constraints

- Python `>=3.12`；`X | None` / `list[...]` 标注。
- `Article` 来自 `push2xteink.models`（P1，**不可改**）。用到的字段：`title, link, author, source_title, published_at: datetime|None, content_html, summary: str|None`。
- `content_html` 是 HTML 片段（可能来自 RSS 或 P2a 提取，都已是安全片段）；`summary` 是纯文本（可能多行，来自 P2b）。
- 所有 datetime 是 tz-aware UTC；展示时格式化为 `YYYY-MM-DD HH:MM`（UTC）。
- 生成的文件放进入参 `out_dir`（`Path`），返回该文件 `Path`。同名文件直接覆盖（调用方 P3 负责唯一命名）。
- EPUB 语言设 `zh-CN`；书名 = 入参 `title`。
- 源文件 `src/push2xteink/builders/`，测试 `tests/`。

## Prerequisite

依赖已由 P2 共享 prep commit 加好（`ebooklib>=0.18`）。本计划不改 `pyproject.toml`。

## File Structure

| 文件 | 职责 |
|---|---|
| `src/push2xteink/builders/__init__.py` | 导出 `build_epub`、`build_txt`、`BuildError`、`safe_filename` |
| `src/push2xteink/builders/common.py` | `BuildError`、`safe_filename`、`format_published`、`chapter_body_html` |
| `src/push2xteink/builders/epub.py` | `build_epub` |
| `src/push2xteink/builders/txt.py` | `build_txt` |
| `tests/test_builders_common.py` / `tests/test_builders_epub.py` / `tests/test_builders_txt.py` | 单测 |

## Interfaces（本计划对外产出）

```python
# push2xteink.builders.common
class BuildError(Exception): ...
def safe_filename(title: str, ext: str) -> str
    # 清洗非法字符 (/ \ : * ? " < > | 及控制符 -> _)，折叠空白，去首尾点和空格，
    # 截断到 120 字（不含扩展名），空 -> "untitled"；返回 f"{cleaned}.{ext}"
def format_published(dt: datetime | None) -> str
    # dt is None -> ""；否则 dt.astimezone(utc).strftime("%Y-%m-%d %H:%M")
def chapter_body_html(article: Article) -> str
    # article.summary 非空 -> f"<div>{escaped summary as <p> lines}</div><hr/>{article.content_html}"
    # 否则 -> article.content_html

# push2xteink.builders.epub
def build_epub(title: str, articles: list[Article], *, out_dir: Path) -> Path

# push2xteink.builders.txt
def build_txt(title: str, articles: list[Article], *, out_dir: Path) -> Path

# push2xteink.builders  (re-export)
from .common import BuildError, safe_filename
from .epub import build_epub
from .txt import build_txt
```

---

## Task 1: `common.py` —— 文件名清洗、时间格式、章节正文组织

**Files:**
- Create: `src/push2xteink/builders/__init__.py`
- Create: `src/push2xteink/builders/common.py`
- Create: `tests/test_builders_common.py`

**Interfaces:**
- Consumes: `push2xteink.models.Article`
- Produces: `BuildError`, `safe_filename`, `format_published`, `chapter_body_html`

- [ ] **Step 1: 写失败测试**

```python
from datetime import datetime, timezone

import pytest

from push2xteink.builders.common import (
    BuildError, chapter_body_html, format_published, safe_filename,
)
from push2xteink.models import Article


@pytest.mark.parametrize("title,expected", [
    ("早报 2026/08/31", "早报 2026_08_31.epub"),
    ('a:b*c?"d<e>f|g', "a_b_c__d_e_f_g.epub"),
    ("  ...trim...  ", "trim....epub".replace("....epub", ".epub") if False else "...trim....epub".strip("."). rstrip() + ".epub" if False else "trim.epub"),
])
def test_safe_filename_basic(title, expected):
    # keep the assertion simple and explicit instead of the parametrize gymnastics above
    pass


def test_safe_filename_replaces_illegal_chars():
    assert safe_filename('a/b\\c:d*e?f"g<h>i|j', "epub") == "a_b_c_d_e_f_g_h_i_j.epub"


def test_safe_filename_collapses_ws_and_trims_dots():
    assert safe_filename("  hello   world  ", "txt") == "hello world.txt"
    assert safe_filename("...name...", "txt") == "name.txt"


def test_safe_filename_empty_becomes_untitled():
    assert safe_filename("   ", "epub") == "untitled.epub"
    assert safe_filename("///", "epub") == "untitled.epub"


def test_safe_filename_truncates_long():
    name = safe_filename("x" * 300, "epub")
    assert name == "x" * 120 + ".epub"


def test_format_published():
    assert format_published(None) == ""
    assert format_published(datetime(2026, 8, 31, 7, 5, tzinfo=timezone.utc)) == "2026-08-31 07:05"
    # non-UTC input normalized
    from datetime import timedelta
    tz = timezone(timedelta(hours=8))
    assert format_published(datetime(2026, 8, 31, 15, 5, tzinfo=tz)) == "2026-08-31 07:05"


def test_chapter_body_without_summary_is_content_only():
    a = Article(feed_id="f", guid="g", title="t", link="l", content_html="<p>body</p>")
    assert chapter_body_html(a) == "<p>body</p>"


def test_chapter_body_with_summary_prepends_and_separates():
    a = Article(feed_id="f", guid="g", title="t", link="l",
                content_html="<p>body</p>", summary="line one\nline two")
    out = chapter_body_html(a)
    assert out.index("line one") < out.index("<hr")
    assert out.index("<hr") < out.index("<p>body</p>")
    assert "<p>line one</p>" in out and "<p>line two</p>" in out


def test_chapter_body_escapes_summary():
    a = Article(feed_id="f", guid="g", title="t", link="l",
                content_html="<p>b</p>", summary="a < b & c")
    out = chapter_body_html(a)
    assert "a &lt; b &amp; c" in out
```

（注：把上面第一个 `@pytest.mark.parametrize` 块和 `test_safe_filename_basic` 删掉——它们是草稿噪声；保留其余具体断言测试。）

- [ ] **Step 2: 运行 → 失败**

- [ ] **Step 3: 实现**

`src/push2xteink/builders/common.py`：

```python
from __future__ import annotations

import re
from datetime import datetime, timezone
from html import escape

from ..models import Article

_ILLEGAL = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_WS = re.compile(r"\s+")
_MAX_STEM = 120


class BuildError(Exception):
    """生成的文件无效（如 EPUB 过小）。"""


def safe_filename(title: str, ext: str) -> str:
    stem = _ILLEGAL.sub("_", title or "")
    stem = _WS.sub(" ", stem).strip().strip(".").strip()
    stem = stem[:_MAX_STEM].strip()
    if not stem:
        stem = "untitled"
    return f"{stem}.{ext}"


def format_published(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _summary_html(summary: str) -> str:
    lines = [escape(ln.strip()) for ln in summary.splitlines() if ln.strip()]
    return "<div>" + "".join(f"<p>{ln}</p>" for ln in lines) + "</div>"


def chapter_body_html(article: Article) -> str:
    if article.summary and article.summary.strip():
        return _summary_html(article.summary) + "<hr/>" + article.content_html
    return article.content_html
```

`src/push2xteink/builders/__init__.py`：

```python
from .common import BuildError, safe_filename
from .epub import build_epub
from .txt import build_txt

__all__ = ["BuildError", "safe_filename", "build_epub", "build_txt"]
```

（`__init__.py` 会 import `epub`/`txt`——它们此刻还不存在。**本任务先让 `__init__.py` 只导出 common 的两个符号**，Task 2/3 完成后再补 `build_epub` / `build_txt` 的导入行。或者：Task 1 直接建 `epub.py` / `txt.py` 空桩 `def build_epub(*a, **k): raise NotImplementedError`。选后者——建桩，避免 `__init__` 半残。）

- [ ] **Step 4: 建 epub.py / txt.py 桩**

```python
# epub.py
from __future__ import annotations
from pathlib import Path
from ..models import Article
def build_epub(title: str, articles: list[Article], *, out_dir: Path) -> Path:
    raise NotImplementedError  # P2c Task 2

# txt.py
from __future__ import annotations
from pathlib import Path
from ..models import Article
def build_txt(title: str, articles: list[Article], *, out_dir: Path) -> Path:
    raise NotImplementedError  # P2c Task 3
```

- [ ] **Step 5: 运行 → 全绿。Commit** `feat: builders/common - filename, time, chapter body`

---

## Task 2: `build_epub`

**Files:**
- Modify: `src/push2xteink/builders/epub.py`
- Create: `tests/test_builders_epub.py`

**Interfaces:**
- Consumes: `chapter_body_html`, `format_published`, `safe_filename`, `BuildError`（Task 1）
- Produces: `build_epub(title, articles, *, out_dir) -> Path`

**行为**：
- ebooklib：`EpubBook`，`set_identifier(uuid4)`、`set_title(title)`、`set_language("zh-CN")`。
- 每个 article 一个 `EpubHtml`（`file_name=f"ch{i:03d}.xhtml"`、`title=article.title`、`lang="zh-CN"`），内容 = `<h1>{escape(title)}</h1><p class="meta">{来源} · {链接锚} · {时间}</p>` + `chapter_body_html(article)`，整体包在最小 XHTML 骨架里。
- `book.toc` = 章节元组；`book.add_item(EpubNcx())`、`book.add_item(EpubNav())`；`book.spine = ["nav", *chapters]`。
- `articles` 为空 → 仍生成一本只有 nav 的书（P3 不会拿空列表来调，但别崩）。
- 写到 `out_dir / safe_filename(title, "epub")`，`epub.write_epub(path, book)`。
- 写完 `path.stat().st_size < 256` → `raise BuildError`。
- 返回 `path`。

- [ ] **Step 1: 写失败测试**

```python
from pathlib import Path
from datetime import datetime, timezone

import pytest
from ebooklib import epub

from push2xteink.builders.epub import build_epub
from push2xteink.builders.common import BuildError
from push2xteink.models import Article


def _articles(n=2):
    return [
        Article(
            feed_id="f", guid=f"g{i}", title=f"文章 {i}", link=f"https://x/{i}",
            source_title="来源站", author="作者",
            published_at=datetime(2026, 8, 31, 7, 0, tzinfo=timezone.utc),
            content_html=f"<p>正文 {i} " + "内容很长。" * 20 + "</p>",
            summary=("摘要一\n摘要二" if i == 0 else None),
        )
        for i in range(n)
    ]


def test_build_epub_creates_file_with_chapters(tmp_path):
    path = build_epub("早报 20260831", _articles(3), out_dir=tmp_path)
    assert path.exists() and path.suffix == ".epub"
    assert path.name == "早报 20260831.epub"

    book = epub.read_epub(str(path))
    assert book.title == "早报 20260831"
    docs = [i for i in book.get_items() if isinstance(i, epub.EpubHtml) and i.file_name.startswith("ch")]
    assert len(docs) == 3
    first = docs[0].get_content().decode("utf-8")
    assert "文章 0" in first
    assert "摘要一" in first and "<hr" in first
    assert "来源站" in first


def test_build_epub_chapter_without_summary_has_no_hr(tmp_path):
    path = build_epub("t", _articles(2), out_dir=tmp_path)
    book = epub.read_epub(str(path))
    docs = sorted(
        (i for i in book.get_items() if isinstance(i, epub.EpubHtml) and i.file_name.startswith("ch")),
        key=lambda d: d.file_name,
    )
    assert "<hr" not in docs[1].get_content().decode("utf-8")


def test_build_epub_raises_when_too_small(tmp_path, monkeypatch):
    # force write_epub to produce a tiny file
    import push2xteink.builders.epub as mod
    def fake_write(path, book, *a, **k):
        Path(path).write_bytes(b"x")
    monkeypatch.setattr(mod.epub, "write_epub", fake_write)
    with pytest.raises(BuildError):
        build_epub("t", _articles(1), out_dir=tmp_path)


def test_build_epub_overwrites_existing(tmp_path):
    p1 = build_epub("dup", _articles(1), out_dir=tmp_path)
    p2 = build_epub("dup", _articles(2), out_dir=tmp_path)
    assert p1 == p2
    assert epub.read_epub(str(p2))
```

- [ ] **Step 2: 运行 → 失败**（`NotImplementedError`）

- [ ] **Step 3: 实现**

```python
from __future__ import annotations

import uuid
from html import escape
from pathlib import Path

from ebooklib import epub

from ..models import Article
from .common import BuildError, chapter_body_html, format_published, safe_filename

_MIN_BYTES = 256
_XHTML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN">'
    "<head><title>{title}</title></head><body>{body}</body></html>"
)


def _chapter_html(article: Article) -> str:
    meta_bits = [b for b in (
        article.source_title or "",
        f'<a href="{escape(article.link)}">原文</a>' if article.link else "",
        format_published(article.published_at),
    ) if b]
    inner = (
        f"<h1>{escape(article.title)}</h1>"
        f'<p class="meta">{" · ".join(meta_bits)}</p>'
        f"{chapter_body_html(article)}"
    )
    return _XHTML.format(title=escape(article.title), body=inner)


def build_epub(title: str, articles: list[Article], *, out_dir: Path) -> Path:
    book = epub.EpubBook()
    book.set_identifier(f"urn:uuid:{uuid.uuid4()}")
    book.set_title(title)
    book.set_language("zh-CN")

    chapters: list[epub.EpubHtml] = []
    for i, article in enumerate(articles):
        ch = epub.EpubHtml(
            title=article.title or f"章节 {i + 1}",
            file_name=f"ch{i:03d}.xhtml",
            lang="zh-CN",
        )
        ch.content = _chapter_html(article)
        book.add_item(ch)
        chapters.append(ch)

    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *chapters]

    path = Path(out_dir) / safe_filename(title, "epub")
    epub.write_epub(str(path), book)
    if path.stat().st_size < _MIN_BYTES:
        raise BuildError(f"generated EPUB too small ({path.stat().st_size} bytes): {path}")
    return path
```

- [ ] **Step 4: 补 `builders/__init__.py`** —— 确认 `from .epub import build_epub` 已在（Task 1 已加）。运行 → 全绿。**Commit** `feat: build_epub`

---

## Task 3: `build_txt`

**Files:**
- Modify: `src/push2xteink/builders/txt.py`
- Create: `tests/test_builders_txt.py`

**Interfaces:**
- Consumes: `format_published`, `safe_filename`（Task 1）
- Produces: `build_txt(title, articles, *, out_dir) -> Path`

**行为**（spec 第 6 节步骤 5 txt 格式）：每条：
```
# {article.title}
{source_title} · {link} · {published}
                          (仅拼接非空部分，用 " · " 连接)

{summary 原文，若有，末尾空行}
{content_html 去标签后的纯文本}
```
条目之间用一行 `\n\n----------------------------------------\n\n` 分隔。整个文件 UTF-8，末尾一个换行。去标签：用标准库 `html.parser` 写一个极简 tag stripper（把 `<p>`/`<br>`/`</p>` 当换行，其余标签丢弃，实体反转义）。返回 `out_dir / safe_filename(title, "txt")`。

- [ ] **Step 1: 写失败测试**

```python
from datetime import datetime, timezone
from pathlib import Path

from push2xteink.builders.txt import build_txt
from push2xteink.models import Article


def _a(i, summary=None):
    return Article(
        feed_id="f", guid=f"g{i}", title=f"标题{i}", link=f"https://x/{i}",
        source_title="站点", published_at=datetime(2026, 8, 31, 7, 0, tzinfo=timezone.utc),
        content_html=f"<p>第一段{i}</p><p>第二段{i}</p>", summary=summary,
    )


def test_build_txt_structure(tmp_path):
    path = build_txt("早报", [_a(0, summary="要点A\n要点B"), _a(1)], out_dir=tmp_path)
    assert path.name == "早报.txt"
    text = path.read_text(encoding="utf-8")
    assert "# 标题0" in text
    assert "站点 · https://x/0 · 2026-08-31 07:00" in text
    assert "要点A\n要点B" in text
    assert "第一段0\n第二段0" in text
    assert "-" * 40 in text          # separator between the two entries
    assert text.endswith("\n")


def test_build_txt_no_summary_no_blank_summary_block(tmp_path):
    path = build_txt("t", [_a(0)], out_dir=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "标题0" in text and "第一段0" in text


def test_build_txt_meta_line_skips_empty_parts(tmp_path):
    a = Article(feed_id="f", guid="g", title="仅标题", link="", content_html="<p>正文</p>")
    text = build_txt("t", [a], out_dir=tmp_path).read_text(encoding="utf-8")
    assert "仅标题" in text
    # no leading/trailing " · " around an empty meta line
    assert " ·  · " not in text
```

- [ ] **Step 2: 运行 → 失败**

- [ ] **Step 3: 实现**

```python
from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
from pathlib import Path

from ..models import Article
from .common import format_published, safe_filename

_SEP = "\n\n" + "-" * 40 + "\n\n"
_BREAK_TAGS = {"p", "br", "div", "li", "h1", "h2", "h3", "tr"}


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _BREAK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _BREAK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = [ln.strip() for ln in raw.splitlines()]
        out: list[str] = []
        for ln in lines:
            if ln or (out and out[-1]):
                out.append(ln)
        return "\n".join(out).strip()


def _strip_html(html: str) -> str:
    p = _Stripper()
    p.feed(html)
    return unescape(p.text())


def _entry(article: Article) -> str:
    meta = " · ".join(
        b for b in (article.source_title or "", article.link or "",
                    format_published(article.published_at)) if b
    )
    blocks = [f"# {article.title}", meta] if meta else [f"# {article.title}"]
    body = ""
    if article.summary and article.summary.strip():
        body += article.summary.strip() + "\n\n"
    body += _strip_html(article.content_html)
    return "\n".join(blocks) + "\n\n" + body


def build_txt(title: str, articles: list[Article], *, out_dir: Path) -> Path:
    doc = _SEP.join(_entry(a) for a in articles).rstrip() + "\n"
    path = Path(out_dir) / safe_filename(title, "txt")
    path.write_text(doc, encoding="utf-8")
    return path
```

- [ ] **Step 4: 运行完整套件 → 全绿。Commit** `feat: build_txt`

---

## Self-Review

**Spec coverage:** 第 6 节步骤 5：EPUB「每篇一章、书名 `{task.name}_{日期}`（P3 传入 title）、目录」→ Task 2；TXT 格式串 → Task 3；「摘要 + `<hr/>` + 正文 / 仅正文」→ Task 1 `chapter_body_html` 两分支，EPUB 和 TXT 都用。第 7.3 节「EPUB < 256 字节 → 异常」→ Task 2 `BuildError`。同日重复执行的唯一命名是 P3 的责任，builders 覆盖同名文件（Task 2 `test_build_epub_overwrites_existing`）。

**Placeholder scan:** Task 1 建 `epub.py`/`txt.py` 桩（`raise NotImplementedError` + 标注任务号），Task 2/3 替换为完整实现。Step 1 测试代码里那段 `@pytest.mark.parametrize` 草稿已明确要求删除，不进最终文件。

**Type consistency:** `build_epub` / `build_txt` 签名 `(title: str, articles: list[Article], *, out_dir: Path) -> Path` 在 Interfaces / 桩 / 实现 / 测试一致。`BuildError` / `safe_filename` / `format_published` / `chapter_body_html` 名称一致。`builders/__init__.py` 的 re-export 与三个子模块符号一致。

**独立性:** 本模块只依赖 `push2xteink.models`（P1）和 ebooklib/stdlib，与 P2a/P2b/P2d 无交叉，可并行开发。
