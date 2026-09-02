"""把任意来源的 HTML（RSS 正文 / trafilatura 输出）清洗成可安全嵌入 EPUB 的
合法 XHTML 片段，同时收集其中的图片 URL。

设备（墨水屏）用自己的重排引擎，只认语义结构，非法 XHTML 会让整章排版崩，
所以这里手写白名单遍历而不是依赖 lxml 的 Cleaner。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from lxml import etree, html as lh

# 保留的标签（其余未知标签一律拆壳，保留子节点/文本）。
_KEEP = {
    "p", "br", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "blockquote", "pre", "code",
    "em", "strong", "b", "i", "sub", "sup", "a", "img", "hr",
    "figure", "figcaption",
    "table", "thead", "tbody", "tr", "td", "th",
}
# 整棵子树删除（连内容一起）。
_DROP_TREE = (
    "script", "style", "iframe", "form", "noscript", "svg", "math",
    "video", "audio", "object", "embed", "button", "input", "textarea",
    "select", "head", "link", "meta", "ins", "title", "base",
)
_ATTR_KEEP = {"a": {"href"}, "img": {"src", "alt"}}
# 没有文字且不含图片时视为空、可丢弃的容器。
_DROPPABLE_WHEN_EMPTY = {"p", "li", "blockquote", "figure", "figcaption", "td", "th"}


@dataclass
class CleanResult:
    html: str
    image_urls: list[str] = field(default_factory=list)


def _has_scheme(url: str) -> bool:
    try:
        return bool(urlparse(url).scheme)
    except ValueError:
        return False


def _abs(url: str, base_url: str) -> str | None:
    url = (url or "").strip()
    if not url or url.startswith(("data:", "javascript:", "#")):
        return None
    if _has_scheme(url):
        return url
    if base_url:
        joined = urljoin(base_url, url)
        return joined if _has_scheme(joined) else None
    return None


def _tree_from(raw: str):
    raw = raw or ""
    try:
        root = lh.fromstring(f"<root>{raw}</root>")
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        return None
    # 完整文档时 (<html><body>…) 只取 body 内容；其余标签会在下面被拆壳。
    body = root.find(".//body")
    return body if body is not None else root


def _strip_subtrees(root) -> None:
    etree.strip_elements(root, *_DROP_TREE, with_tail=False)
    for el in root.xpath("//comment()"):
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)


def _unwrap_unknown(root) -> None:
    # h1 保留到 _clean_element 里降级为 h2；其余不在白名单的标签拆壳。
    known = _KEEP | {"h1", "root"}
    unknown = {
        el.tag
        for el in root.iter()
        if isinstance(el.tag, str) and el.tag not in known
    }
    if unknown:
        etree.strip_tags(root, *unknown)


def _clean_element(el, base_url: str) -> None:
    if el.tag == "h1":
        el.tag = "h2"

    keep = _ATTR_KEEP.get(el.tag, set())
    for name in list(el.attrib):
        if name not in keep:
            del el.attrib[name]

    if el.tag == "a" and "href" in el.attrib:
        resolved = _abs(el.attrib["href"], base_url)
        if resolved:
            el.attrib["href"] = resolved
        else:
            del el.attrib["href"]
    if el.tag == "img":
        resolved = _abs(el.attrib.get("src", ""), base_url)
        if resolved:
            el.attrib["src"] = resolved
        else:
            _remove_keeping_tail(el)


def _remove_keeping_tail(el) -> None:
    parent = el.getparent()
    if parent is None:
        return
    if el.tail:
        prev = el.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + el.tail
        else:
            parent.text = (parent.text or "") + el.tail
    parent.remove(el)


def _text_len(el) -> int:
    return len("".join(el.itertext()).strip())


def _drop_empty(root) -> None:
    # 反向遍历：先内层后外层，删空节点后外层可能也变空。
    for el in reversed(list(root.iter())):
        if el is root or not isinstance(el.tag, str):
            continue
        if el.tag in _DROPPABLE_WHEN_EMPTY and _text_len(el) == 0:
            if el.find(".//img") is None:
                _remove_keeping_tail(el)


def _serialize_children(root) -> str:
    parts: list[str] = []
    if root.text and root.text.strip():
        parts.append(root.text)
    for child in root:
        parts.append(etree.tostring(child, method="xml", encoding="unicode"))
    return "".join(parts).strip()


def normalize_html(raw: str, *, base_url: str = "") -> CleanResult:
    root = _tree_from(raw)
    if root is None:
        return CleanResult(html="")

    _strip_subtrees(root)
    _unwrap_unknown(root)
    for el in list(root.iter()):
        if el is not root and isinstance(el.tag, str):
            _clean_element(el, base_url)
    _drop_empty(root)

    seen: set[str] = set()
    image_urls: list[str] = []
    for img in root.iter("img"):
        src = img.get("src")
        if src and src not in seen:
            seen.add(src)
            image_urls.append(src)

    return CleanResult(html=_serialize_children(root), image_urls=image_urls)


def apply_image_map(html: str, url_map: dict[str, str]) -> str:
    """把 <img src> 的绝对 URL 重写成 EPUB 内的本地文件名；不在 map 里的 <img> 删掉。"""
    try:
        root = lh.fromstring(f"<root>{html or ''}</root>")
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        return html
    for img in list(root.iter("img")):
        local = url_map.get(img.get("src", ""))
        if local:
            img.attrib["src"] = local
        else:
            _remove_keeping_tail(img)
    return _serialize_children(root)
