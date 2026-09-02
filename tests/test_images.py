import httpx
import respx

from push2xteink.images import download_images

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 40


@respx.mock
def test_downloads_and_maps_images():
    respx.get("https://e.com/a.png").mock(
        return_value=httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    )
    respx.get("https://e.com/b.jpg").mock(
        return_value=httpx.Response(200, content=JPG, headers={"content-type": "image/jpeg"})
    )
    images, url_map = download_images(["https://e.com/a.png", "https://e.com/b.jpg"])

    assert len(images) == 2
    assert set(url_map) == {"https://e.com/a.png", "https://e.com/b.jpg"}
    a = next(i for i in images if i.filename == url_map["https://e.com/a.png"])
    assert a.filename.startswith("img/") and a.filename.endswith(".png")
    assert a.media_type == "image/png" and a.data == PNG


@respx.mock
def test_skips_non_image_content_type():
    respx.get("https://e.com/x").mock(
        return_value=httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
    )
    images, url_map = download_images(["https://e.com/x"])
    assert images == [] and url_map == {}


@respx.mock
def test_skips_oversized_image():
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * 5000
    respx.get("https://e.com/big.png").mock(
        return_value=httpx.Response(200, content=big, headers={"content-type": "image/png"})
    )
    images, _ = download_images(["https://e.com/big.png"], max_bytes=1000)
    assert images == []


@respx.mock
def test_skips_failed_download_but_keeps_others():
    respx.get("https://e.com/ok.png").mock(
        return_value=httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    )
    respx.get("https://e.com/500.png").mock(return_value=httpx.Response(500))
    images, url_map = download_images(["https://e.com/500.png", "https://e.com/ok.png"])
    assert list(url_map) == ["https://e.com/ok.png"] and len(images) == 1


@respx.mock
def test_respects_max_count():
    for n in range(5):
        respx.get(f"https://e.com/{n}.png").mock(
            return_value=httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        )
    urls = [f"https://e.com/{n}.png" for n in range(5)]
    images, _ = download_images(urls, max_count=2)
    assert len(images) == 2


@respx.mock
def test_derives_extension_from_content_type_not_url():
    respx.get("https://e.com/pic").mock(
        return_value=httpx.Response(200, content=JPG, headers={"content-type": "image/jpeg"})
    )
    _, url_map = download_images(["https://e.com/pic"])
    assert url_map["https://e.com/pic"].endswith(".jpg")


@respx.mock
def test_same_url_twice_downloaded_once():
    route = respx.get("https://e.com/a.png").mock(
        return_value=httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    )
    images, _ = download_images(["https://e.com/a.png", "https://e.com/a.png"])
    assert len(images) == 1 and route.call_count == 1


@respx.mock
def test_sends_referer_when_given():
    captured = {}

    def handler(request):
        captured["referer"] = request.headers.get("referer")
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    respx.get("https://e.com/a.png").mock(side_effect=handler)
    download_images(["https://e.com/a.png"], referer="https://blog.example/post")
    assert captured["referer"] == "https://blog.example/post"


def test_empty_list_returns_empty():
    assert download_images([]) == ([], {})
