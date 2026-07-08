"""Unit tests for the stdlib escape-first markdown renderer (CP6-4).

Pins the section 1.3 contract of quant_forge.apps.web.markdown:

- escape-first pipeline: raw HTML in source always renders as escaped
  literal text, single-escaped, never double-unescaped;
- link dispatch: external URLs become plain-text spans (never anchors),
  relative in-root ``.md`` targets become ``#docs-doc-`` app-router anchors,
  everything else stays escaped literal source;
- images render alt text only (``<img>`` never appears, src dropped);
- fenced/inline code contents are frozen (no inline transforms inside);
- the exhaustive output tag/attribute whitelist;
- ``extract_markdown_title`` heading-rule cases.
"""

from __future__ import annotations

import re

from quant_forge.apps.web.markdown import extract_markdown_title, render_markdown_html


def _render(text: str, relpath: str = "guide/current.md") -> str:
    return render_markdown_html(text, current_relpath=relpath)


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------


def test_script_tag_renders_as_escaped_literal_text() -> None:
    html_out = _render("<script>alert(1)</script>")
    assert html_out == "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"
    assert "<script" not in html_out


def test_raw_img_with_event_handler_is_escaped() -> None:
    html_out = _render("<img src=x onerror=x>")
    assert html_out == "<p>&lt;img src=x onerror=x&gt;</p>"
    assert "<img" not in html_out


def test_ampersand_is_escaped_exactly_once() -> None:
    assert _render("a & b") == "<p>a &amp; b</p>"


def test_pre_escaped_entity_is_not_double_unescaped() -> None:
    # "&amp;" in source stays literal text: escaped once more, never decoded.
    assert _render("&amp;") == "<p>&amp;amp;</p>"


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def test_javascript_mailto_absolute_and_non_md_targets_stay_literal() -> None:
    for target in (
        "javascript:void0",
        "mailto:someone@example.invalid",
        "/abs/path.md",
        "notes.txt",
    ):
        html_out = _render(f"[label]({target})")
        assert "<a" not in html_out, target
        assert f"[label]({target})" in html_out, target


def test_external_link_renders_text_plus_url_span_never_anchor() -> None:
    html_out = _render("[docs site](https://example.invalid/page)")
    assert html_out == (
        '<p>docs site <span class="docs-external-url">(https://example.invalid/page)</span></p>'
    )
    assert "<a" not in html_out
    assert 'href="http' not in html_out


def test_internal_md_link_resolves_against_current_relpath() -> None:
    html_out = render_markdown_html(
        "[decisions](../coordination/DECISIONS.md)", current_relpath="reviews/a.md"
    )
    assert html_out == (
        '<p><a class="docs-link" href="#docs-doc-coordination/DECISIONS.md">decisions</a></p>'
    )


def test_internal_md_link_fragment_is_stripped_before_resolution() -> None:
    html_out = render_markdown_html("[s](other.md#part)", current_relpath="guide/a.md")
    assert 'href="#docs-doc-guide/other.md"' in html_out


def test_internal_link_escaping_the_root_stays_literal() -> None:
    html_out = render_markdown_html("[x](../../secret.md)", current_relpath="reviews/a.md")
    assert "<a" not in html_out
    assert "[x](../../secret.md)" in html_out


def test_autolink_renders_url_span_never_anchor() -> None:
    html_out = _render("<https://example.invalid/x>")
    assert html_out == '<p><span class="docs-external-url">(https://example.invalid/x)</span></p>'
    assert "<a" not in html_out


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def test_images_render_alt_span_only_and_drop_src() -> None:
    html_out = _render("![diagram](assets/pic.png)")
    assert html_out == '<p><span class="docs-image-alt">diagram</span></p>'
    assert "<img" not in html_out
    assert "assets/pic.png" not in html_out


def test_image_with_empty_alt_renders_literal_image_label() -> None:
    assert _render("![](assets/pic.png)") == '<p><span class="docs-image-alt">image</span></p>'


# ---------------------------------------------------------------------------
# Code
# ---------------------------------------------------------------------------


def test_fenced_code_is_verbatim_escaped_with_info_string_dropped() -> None:
    html_out = _render("```python\n**not bold** <b>\n```")
    assert html_out == "<pre><code>**not bold** &lt;b&gt;</code></pre>"
    assert "python" not in html_out
    assert "<strong>" not in html_out


def test_unclosed_fence_consumes_to_end_of_input() -> None:
    assert _render("```\nline1\nline2") == "<pre><code>line1\nline2</code></pre>"


def test_tilde_fence_closes_only_on_same_char_and_length() -> None:
    html_out = _render("~~~~\ncontent\n~~~\n~~~~")
    assert html_out == "<pre><code>content\n~~~</code></pre>"


def test_inline_code_freezes_contents_from_later_inline_rules() -> None:
    html_out = _render("use `**x**` here")
    assert "<code>**x**</code>" in html_out
    assert "<strong>" not in html_out


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


def test_atx_headings_levels_one_through_six_without_id_attribute() -> None:
    for level in range(1, 7):
        html_out = _render("#" * level + " Title text")
        assert html_out == f"<h{level}>Title text</h{level}>"
        assert "id=" not in html_out
    # Trailing closing hashes are dropped; seven hashes are a paragraph.
    assert _render("# Title #") == "<h1>Title</h1>"
    assert _render("####### deep") == "<p>####### deep</p>"


def test_table_renders_scroll_wrapper_and_docs_table_class() -> None:
    html_out = _render("| A | B |\n|---|---|\n| 1 | 2 |")
    assert html_out == (
        '<div class="table-scroll"><table class="docs-table">'
        "<thead><tr><th>A</th><th>B</th></tr></thead>"
        "<tbody><tr><td>1</td><td>2</td></tr></tbody></table></div>"
    )


def test_blockquote_parses_escaped_marker() -> None:
    assert _render("> quoted text") == "<blockquote><p>quoted text</p></blockquote>"


def test_blockquote_recursion_cap_degrades_to_paragraph_text() -> None:
    html_out = _render("> " * 6 + "deep")
    assert html_out.count("<blockquote>") == 5
    assert "&gt; deep" in html_out


def test_hr_wins_over_list_disambiguation() -> None:
    assert _render("- - -") == "<hr>"
    assert _render("***") == "<hr>"
    assert _render("___") == "<hr>"
    assert _render("- item") == "<ul><li>item</li></ul>"


def test_nested_list_levels_and_depth_cap() -> None:
    html_out = _render("- a\n  - b\n    - c\n          - d")
    # 10 leading spaces would be level 5; the cap keeps it at level 3.
    assert html_out == (
        "<ul><li>a<ul><li>b<ul><li>c<ul><li>d</li></ul></li></ul></li></ul></li></ul>"
    )


def test_ordered_list_has_no_start_attribute() -> None:
    html_out = _render("3. three\n4. four")
    assert html_out == "<ol><li>three</li><li>four</li></ol>"
    assert "start" not in html_out


def test_list_lazy_continuation_joins_with_single_space() -> None:
    assert _render("- item\n  continued") == "<ul><li>item continued</li></ul>"


def test_paragraph_run_joins_lines_with_single_spaces() -> None:
    assert _render("one\ntwo\n\nthree") == "<p>one two</p><p>three</p>"


# ---------------------------------------------------------------------------
# Whole-output whitelist
# ---------------------------------------------------------------------------


_ALLOWED_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "ul", "ol", "li", "blockquote", "pre", "code", "strong", "em", "hr",
    "table", "thead", "tbody", "tr", "th", "td", "br",
    "div", "a", "span",
}

_ALLOWED_ATTR_PATTERNS = (
    re.compile(r"^$"),
    re.compile(r'^ class="table-scroll"$'),
    re.compile(r'^ class="docs-table"$'),
    re.compile(r'^ class="docs-external-url"$'),
    re.compile(r'^ class="docs-image-alt"$'),
    re.compile(r'^ class="docs-link" href="#docs-doc-[^"]+"$'),
)

_KITCHEN_SINK = """# Title

Intro paragraph with **bold**, *italic*, `code`, a [link](../coordination/DECISIONS.md),
an external [site](https://example.invalid/x), an autolink <https://example.invalid/y>,
an image ![alt text](pic.png), and raw <b onclick=x>html</b>.

## Table

| Col | Val |
|-----|-----|
| a   | 1   |

> quote line
> second line

- item one
  - nested item
1. ordered

---

```text
fenced <script> **content**
```
"""


def test_rendered_output_uses_only_whitelisted_tags_and_attributes() -> None:
    html_out = render_markdown_html(_KITCHEN_SINK, current_relpath="reviews/a.md")
    for match in re.finditer(r"</?([a-zA-Z][a-zA-Z0-9]*)((?:\s[^>]*)?)>", html_out):
        tag = match.group(1)
        attrs = match.group(2)
        assert tag in _ALLOWED_TAGS, match.group(0)
        if match.group(0).startswith("</"):
            assert attrs == "", match.group(0)
        else:
            assert any(pattern.match(attrs) for pattern in _ALLOWED_ATTR_PATTERNS), match.group(0)
    assert 'href="http' not in html_out
    assert re.search(r"<[^>]*\son\w+=", html_out) is None
    assert re.search(r"<[^>]*javascript:", html_out) is None
    assert "<img" not in html_out
    assert "<script" not in html_out
    assert "style=" not in html_out
    assert "id=" not in html_out


# ---------------------------------------------------------------------------
# extract_markdown_title
# ---------------------------------------------------------------------------


def test_extract_markdown_title_first_heading_wins() -> None:
    assert extract_markdown_title("# Top Title\n\n## Later\n") == "Top Title"


def test_extract_markdown_title_skips_non_heading_lines() -> None:
    assert extract_markdown_title("intro text\n\n## Second Level ##\nbody") == "Second Level"


def test_extract_markdown_title_returns_none_without_heading() -> None:
    assert extract_markdown_title("no headings here\njust text") is None
    assert extract_markdown_title("####### seven hashes") is None
    assert extract_markdown_title("#missing space") is None
    assert extract_markdown_title("") is None
