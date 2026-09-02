from push2xteink.builders.htmlclean import apply_image_map, normalize_html


def _xhtml_parses(fragment: str) -> bool:
    from xml.etree import ElementTree

    try:
        ElementTree.fromstring(f"<root>{fragment}</root>")
        return True
    except ElementTree.ParseError:
        return False


def test_drops_script_and_style_subtrees():
    r = normalize_html(
        "<div><p>keep me</p><script>evil()</script><style>p{color:red}</style></div>"
    )
    assert "keep me" in r.html
    assert "evil" not in r.html and "color:red" not in r.html


def test_unwraps_unknown_tags_keeping_text():
    r = normalize_html('<section><div><span>hello</span> world</div></section>')
    assert "<div" not in r.html and "<span" not in r.html and "<section" not in r.html
    assert "hello" in r.html and "world" in r.html


def test_strips_disallowed_attributes():
    r = normalize_html(
        '<p class="x" style="color:red" onclick="hack()">text</p>'
    )
    assert "class" not in r.html and "style" not in r.html and "onclick" not in r.html
    assert "<p>text</p>" in r.html


def test_keeps_href_on_anchor_and_src_alt_on_img():
    r = normalize_html(
        '<p><a href="https://e.com/x" rel="nofollow">link</a></p>'
        '<img src="https://e.com/a.jpg" alt="pic" width="800">'
    )
    assert 'href="https://e.com/x"' in r.html and "rel=" not in r.html
    assert 'src="https://e.com/a.jpg"' in r.html and 'alt="pic"' in r.html
    assert "width" not in r.html


def test_absolutizes_relative_urls_against_base():
    r = normalize_html(
        '<p><a href="/rel/page">l</a></p><img src="pic.jpg">',
        base_url="https://site.example/blog/post.html",
    )
    assert 'href="https://site.example/rel/page"' in r.html
    assert 'src="https://site.example/blog/pic.jpg"' in r.html


def test_collects_image_urls_in_order_deduped():
    r = normalize_html(
        '<img src="https://e.com/1.png">'
        '<p>x</p><img src="https://e.com/2.png">'
        '<img src="https://e.com/1.png">',
    )
    assert r.image_urls == ["https://e.com/1.png", "https://e.com/2.png"]


def test_drops_image_with_unresolvable_src():
    r = normalize_html('<img src="pic.jpg"><p>body</p>', base_url="")
    assert "<img" not in r.html
    assert r.image_urls == []


def test_demotes_h1_to_h2():
    r = normalize_html("<h1>Title</h1><h2>Sub</h2>")
    assert "<h1" not in r.html
    assert "<h2>Title</h2>" in r.html and "<h2>Sub</h2>" in r.html


def test_output_is_well_formed_xhtml_from_messy_input():
    messy = "<p>para<br>line<img src=https://e.com/a.jpg>unclosed <b>bold</p><li>orphan"
    r = normalize_html(messy)
    assert _xhtml_parses(r.html)
    assert "<br/>" in r.html or "<br />" in r.html


def test_accepts_full_html_document_and_returns_body_content():
    r = normalize_html(
        "<html><head><title>T</title><script>x</script></head>"
        "<body><p>real content</p></body></html>"
    )
    assert "real content" in r.html
    assert "<title" not in r.html and "<html" not in r.html and "<body" not in r.html
    assert "T" not in r.html.replace("real content", "")


def test_empty_and_garbage_input_does_not_crash():
    assert normalize_html("").html == ""
    assert normalize_html("   ").html.strip() == ""
    assert normalize_html("<<>>not html").image_urls == []


def test_drops_empty_paragraphs_and_containers():
    r = normalize_html("<p>real</p><p></p><p>   </p><blockquote></blockquote>")
    assert r.html.count("<p>") == 1
    assert "<blockquote" not in r.html


def test_apply_image_map_rewrites_known_and_drops_unknown():
    html = (
        '<p>a</p><img src="https://e.com/1.png" alt="one"/>'
        '<img src="https://e.com/2.png"/>'
    )
    out = apply_image_map(html, {"https://e.com/1.png": "img/abc.png"})
    assert 'src="img/abc.png"' in out and 'alt="one"' in out
    assert "https://e.com/2.png" not in out and out.count("<img") == 1


def test_apply_image_map_empty_map_drops_all_images():
    out = apply_image_map('<p>x</p><img src="https://e.com/1.png"/>', {})
    assert "<img" not in out and "<p>x</p>" in out
