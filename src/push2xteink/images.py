"""下载文章正文里的图片，供 EPUB 内嵌。失败一律静默跳过——图片缺失不该让整个推送失败。"""

from __future__ import annotations

import hashlib

import httpx

from .http import make_client
from .models import EmbeddedImage

_EXT_BY_TYPE = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}
_DEFAULT_MAX_BYTES = 2_000_000
_DEFAULT_MAX_COUNT = 20


def _filename(url: str, ext: str) -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return f"img/{digest}.{ext}"


def _fetch_one(
    client: httpx.Client, url: str, *, max_bytes: int, referer: str | None
) -> EmbeddedImage | None:
    headers = {"Referer": referer} if referer else None
    try:
        with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()

            ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            ext = _EXT_BY_TYPE.get(ctype)
            if ext is None:
                return None

            declared = resp.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                return None

            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    return None
                chunks.append(chunk)
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        return None

    data = b"".join(chunks)
    if not data:
        return None
    return EmbeddedImage(filename=_filename(url, ext), media_type=ctype, data=data)


def download_images(
    urls: list[str],
    *,
    proxy_url: str | None = None,
    timeout: float = 20.0,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    max_count: int = _DEFAULT_MAX_COUNT,
    referer: str | None = None,
) -> tuple[list[EmbeddedImage], dict[str, str]]:
    """返回 (图片列表, {原始URL: EPUB内文件名})。保持输入顺序，重复 URL 只下一次。"""
    ordered: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            ordered.append(url)
        if len(ordered) >= max_count:
            break

    if not ordered:
        return [], {}

    images: list[EmbeddedImage] = []
    url_map: dict[str, str] = {}
    with make_client(proxy=proxy_url, timeout=timeout) as client:
        for url in ordered:
            img = _fetch_one(client, url, max_bytes=max_bytes, referer=referer)
            if img is not None:
                images.append(img)
                url_map[url] = img.filename
    return images, url_map
