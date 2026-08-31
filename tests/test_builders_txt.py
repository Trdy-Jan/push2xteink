from datetime import datetime, timezone

from push2xteink.builders.txt import build_txt, _strip_html
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
    assert "第一段0\n\n第二段0" in text
    assert "-" * 40 in text          # separator between the two entries
    assert text.endswith("\n")


def test_build_txt_no_summary_no_blank_summary_block(tmp_path):
    path = build_txt("t", [_a(0)], out_dir=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "标题0" in text and "第一段0" in text


def test_strip_html_unclosed_p_tags_split_lines():
    out = _strip_html("<p>alpha<p>beta<p>gamma")
    lines = out.splitlines()
    assert "alpha" in lines and "beta" in lines and "gamma" in lines
    assert out.index("alpha") < out.index("beta") < out.index("gamma")


def test_build_txt_meta_line_skips_empty_parts(tmp_path):
    a = Article(feed_id="f", guid="g", title="仅标题", link="", content_html="<p>正文</p>")
    text = build_txt("t", [a], out_dir=tmp_path).read_text(encoding="utf-8")
    assert "仅标题" in text
    # no leading/trailing " · " around an empty meta line
    assert " ·  · " not in text
