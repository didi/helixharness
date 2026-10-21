"""Tests for the safe Case-document Markdown renderer."""

from web_ui.markdown import render_markdown


def test_render_markdown_preserves_structure_and_escapes_html() -> None:
    rendered = str(
        render_markdown(
            "# 需求\n\n实现 `totalAmount`。\n\n- 保留本金\n- 不改实名\n\n<script>alert(1)</script>"
        )
    )

    assert "<h1>需求</h1>" in rendered
    assert "<code>totalAmount</code>" in rendered
    assert "<li>保留本金</li>" in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
