"""Stdlib escape-first markdown renderer for the read-only Docs view (CP6-4).

Safety contract (decision D8, CP6-4 spec section 1.3): the whole source is
HTML-escaped before any parsing, all block and inline transforms operate on
the escaped text, and transforms only ever wrap escaped text in a fixed
whitelist of output tags. Nothing is ever unescaped. External URLs render as
plain-text spans (never anchors), images render alt text only (``<img>``
never appears), and headings carry no ``id`` attribute (the hash namespace
belongs to the app router).

Allowed output tags: h1-h6, p, ul, ol, li, blockquote, pre, code, strong,
em, hr, table, thead, tbody, tr, th, td, br (reserved),
div (class="table-scroll" only), a (class="docs-link" with
href="#docs-doc-..." only), span (class="docs-external-url" or
class="docs-image-alt" only).
"""

from __future__ import annotations

import html
import posixpath
import re
from typing import Callable

# Nesting caps keep pathological inputs bounded: at the caps, deeper content
# degrades to paragraph text / the capped list level instead of recursing.
_BLOCKQUOTE_RECURSION_CAP = 4
_LIST_LEVEL_CAP = 3

_BLOCKQUOTE_MARKER = "&gt;"  # the escaped form of ">" (step 2 escapes first)

_FENCE_OPEN_RE = re.compile(r"^(`{3,}|~{3,})(.*)$")
_HEADING_RE = re.compile(r"^(#{1,6}) +(.+?)(?: +#+)? *$")
_TITLE_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*(?:#+\s*)?$")
_HR_RE = re.compile(r"^ *([-*_])( *\1){2,} *$")
_TABLE_DELIMITER_RE = re.compile(r"^\|? *:?-{3,}:? *(\| *:?-{3,}:? *)*\|? *$")
_LIST_ITEM_RE = re.compile(r"^( *)([-*+]|\d{1,9}[.)]) +(.+)$")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

_CODE_DOUBLE_RE = re.compile(r"``([^\n]+?)``")
_CODE_SINGLE_RE = re.compile(r"`([^`\n]+)`")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^()\s]+)\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^()\s]+)\)")
_AUTOLINK_RE = re.compile(r"&lt;(https?://[^&\s]+)&gt;")
_BOLD_STAR_RE = re.compile(r"\*\*(.+?)\*\*")
_BOLD_UNDER_RE = re.compile(r"__(.+?)__")
_ITALIC_STAR_RE = re.compile(r"\*([^*\n]+)\*")
_ITALIC_UNDER_RE = re.compile(r"(?<![A-Za-z0-9_])_([^_\n]+)_(?![A-Za-z0-9_])")

# Frozen inline fragments (code spans, link/image/autolink output) are parked
# behind NUL-delimited placeholders so later inline rules never touch them;
# NUL is stripped from the source during normalization to keep the channel
# collision-free.
_PLACEHOLDER_RE = re.compile("\x00(\\d+)\x00")


def extract_markdown_title(text: str) -> str | None:
    """Text of the first ATX heading line, or ``None`` when no line matches."""

    for line in _normalize(text).split("\n"):
        match = _TITLE_RE.match(line)
        if match:
            return match.group(2).strip()
    return None


def render_markdown_html(text: str, *, current_relpath: str) -> str:
    """Render markdown source to whitelisted HTML (escape-first pipeline).

    ``current_relpath`` is the posix relpath of the document being rendered;
    relative ``.md`` links resolve against its directory into
    ``#docs-doc-<relpath>`` app-router hashes.
    """

    base_dir = posixpath.dirname(current_relpath)
    # Escape everything first: every subsequent transform parses escaped text
    # and only wraps it in whitelisted tags. Nothing is ever unescaped.
    escaped = html.escape(_normalize(text), quote=True)
    return _render_blocks(escaped.split("\n"), 0, base_dir)


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


# ---------------------------------------------------------------------------
# Block rules (applied top-down on escaped lines; first match wins).
# ---------------------------------------------------------------------------


def _render_blocks(lines: list[str], depth: int, base_dir: str) -> str:
    out: list[str] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        fence = _FENCE_OPEN_RE.match(stripped)
        if fence:
            index = _consume_fence(lines, index, fence, out)
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2), base_dir)}</h{level}>")
            index += 1
            continue
        if _HR_RE.match(line):
            out.append("<hr>")
            index += 1
            continue
        if _starts_table(lines, index):
            index = _consume_table(lines, index, out, base_dir)
            continue
        if line.startswith(_BLOCKQUOTE_MARKER):
            index = _consume_blockquote(lines, index, out, depth, base_dir)
            continue
        if _LIST_ITEM_RE.match(line):
            index = _consume_list(lines, index, out, base_dir)
            continue
        index = _consume_paragraph(lines, index, out, base_dir)
    return "".join(out)


def _is_block_trigger(lines: list[str], index: int) -> bool:
    """True when the line opens any non-paragraph block (lazy-continuation stop)."""

    line = lines[index]
    if _FENCE_OPEN_RE.match(line.strip()):
        return True
    if _HEADING_RE.match(line) or _HR_RE.match(line):
        return True
    if _starts_table(lines, index):
        return True
    if line.startswith(_BLOCKQUOTE_MARKER):
        return True
    return _LIST_ITEM_RE.match(line) is not None


def _starts_table(lines: list[str], index: int) -> bool:
    return (
        "|" in lines[index]
        and index + 1 < len(lines)
        and _TABLE_DELIMITER_RE.match(lines[index + 1]) is not None
    )


def _consume_fence(lines: list[str], index: int, fence: re.Match[str], out: list[str]) -> int:
    fence_char = fence.group(1)[0]
    fence_length = len(fence.group(1))
    body: list[str] = []
    cursor = index + 1
    while cursor < len(lines):
        stripped = lines[cursor].strip()
        if stripped and set(stripped) == {fence_char} and len(stripped) >= fence_length:
            cursor += 1
            break
        body.append(lines[cursor])
        cursor += 1
    # Contents stay verbatim (already escaped); no inline transforms inside.
    content = "\n".join(body)
    out.append(f"<pre><code>{content}</code></pre>")
    return cursor


def _consume_table(lines: list[str], index: int, out: list[str], base_dir: str) -> int:
    header_cells = _split_table_row(lines[index])
    cursor = index + 2  # skip the delimiter line (alignment hints ignored)
    body_rows: list[list[str]] = []
    while cursor < len(lines) and "|" in lines[cursor]:
        body_rows.append(_split_table_row(lines[cursor]))
        cursor += 1
    head = "".join(f"<th>{_inline(cell, base_dir)}</th>" for cell in header_cells)
    body = "".join(
        "<tr>" + "".join(f"<td>{_inline(cell, base_dir)}</td>" for cell in row) + "</tr>"
        for row in body_rows
    )
    out.append(
        '<div class="table-scroll"><table class="docs-table">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )
    return cursor


def _split_table_row(line: str) -> list[str]:
    parts = line.split("|")
    if parts and not parts[0].strip():
        parts = parts[1:]
    if parts and not parts[-1].strip():
        parts = parts[:-1]
    return [part.strip() for part in parts]


def _consume_blockquote(
    lines: list[str], index: int, out: list[str], depth: int, base_dir: str
) -> int:
    inner: list[str] = []
    cursor = index
    while cursor < len(lines) and lines[cursor].startswith(_BLOCKQUOTE_MARKER):
        content = lines[cursor][len(_BLOCKQUOTE_MARKER):]
        if content.startswith(" "):
            content = content[1:]
        inner.append(content)
        cursor += 1
    if depth < _BLOCKQUOTE_RECURSION_CAP:
        rendered = _render_blocks(inner, depth + 1, base_dir)
    else:
        # Beyond the recursion cap the stripped lines degrade to paragraph
        # text (still escaped) instead of recursing further.
        joined = " ".join(part.strip() for part in inner if part.strip())
        rendered = f"<p>{_inline(joined, base_dir)}</p>" if joined else ""
    out.append(f"<blockquote>{rendered}</blockquote>")
    return cursor


def _consume_list(lines: list[str], index: int, out: list[str], base_dir: str) -> int:
    items: list[tuple[int, str, str]] = []
    cursor = index
    while cursor < len(lines):
        match = _LIST_ITEM_RE.match(lines[cursor])
        if match is None:
            break
        level = min(len(match.group(1)) // 2, _LIST_LEVEL_CAP)
        tag = "ul" if match.group(2) in {"-", "*", "+"} else "ol"
        text = match.group(3)
        cursor += 1
        # Lazy continuation: a following non-blank line matching no other
        # block trigger joins the current item with a single space.
        while (
            cursor < len(lines)
            and lines[cursor].strip()
            and not _is_block_trigger(lines, cursor)
        ):
            text = f"{text} {lines[cursor].strip()}"
            cursor += 1
        items.append((level, tag, text))
    out.append(_list_html(items, base_dir))
    return cursor


def _list_html(items: list[tuple[int, str, str]], base_dir: str) -> str:
    parts: list[str] = []
    stack: list[tuple[int, str]] = []
    for level, tag, text in items:
        while stack and stack[-1][0] > level:
            parts.append(f"</li></{stack.pop()[1]}>")
        if stack and stack[-1][0] == level and stack[-1][1] != tag:
            parts.append(f"</li></{stack.pop()[1]}>")
        if not stack or stack[-1][0] < level:
            parts.append(f"<{tag}><li>{_inline(text, base_dir)}")
            stack.append((level, tag))
        else:
            parts.append(f"</li><li>{_inline(text, base_dir)}")
    while stack:
        parts.append(f"</li></{stack.pop()[1]}>")
    return "".join(parts)


def _consume_paragraph(lines: list[str], index: int, out: list[str], base_dir: str) -> int:
    parts = [lines[index].strip()]
    cursor = index + 1
    while cursor < len(lines) and lines[cursor].strip() and not _is_block_trigger(lines, cursor):
        parts.append(lines[cursor].strip())
        cursor += 1
    out.append(f"<p>{_inline(' '.join(parts), base_dir)}</p>")
    return cursor


# ---------------------------------------------------------------------------
# Inline rules (fixed order; code spans are frozen first so later rules
# never touch their contents).
# ---------------------------------------------------------------------------


def _inline(text: str, base_dir: str) -> str:
    frozen: list[str] = []

    def _freeze(fragment: str) -> str:
        frozen.append(fragment)
        return f"\x00{len(frozen) - 1}\x00"

    text = _CODE_DOUBLE_RE.sub(lambda m: _freeze(f"<code>{m.group(1)}</code>"), text)
    text = _CODE_SINGLE_RE.sub(lambda m: _freeze(f"<code>{m.group(1)}</code>"), text)
    # Images: the src is dropped entirely; <img> never appears in output.
    text = _IMAGE_RE.sub(lambda m: _freeze(_image_fragment(m.group(1))), text)
    text = _LINK_RE.sub(lambda m: _link_replacement(m, base_dir, _freeze), text)
    text = _AUTOLINK_RE.sub(
        lambda m: _freeze(f'<span class="docs-external-url">({m.group(1)})</span>'), text
    )
    text = _BOLD_STAR_RE.sub(r"<strong>\1</strong>", text)
    text = _BOLD_UNDER_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_STAR_RE.sub(r"<em>\1</em>", text)
    text = _ITALIC_UNDER_RE.sub(r"<em>\1</em>", text)
    return _PLACEHOLDER_RE.sub(lambda m: frozen[int(m.group(1))], text)


def _image_fragment(alt: str) -> str:
    label = alt if alt else "image"
    return f'<span class="docs-image-alt">{label}</span>'


def _link_replacement(
    match: re.Match[str], base_dir: str, freeze: Callable[[str], str]
) -> str:
    text = match.group(1)
    target = match.group(2)
    if target.startswith(("http://", "https://")):
        # External targets never become anchors: the link text stays plain
        # text and the URL renders as an escaped text span.
        span = freeze(f'<span class="docs-external-url">({target})</span>')
        return f"{text} {span}"
    relpath = _internal_doc_relpath(target, base_dir)
    if relpath is not None:
        return freeze(f'<a class="docs-link" href="#docs-doc-{relpath}">{text}</a>')
    # Anything else (mailto:, javascript:, absolute paths, non-.md targets)
    # stays as the escaped literal source, untouched.
    return match.group(0)


def _internal_doc_relpath(target: str, base_dir: str) -> str | None:
    """Root-contained posix relpath for a relative ``.md`` target, else None."""

    if target.startswith("/") or _SCHEME_RE.match(target):
        return None
    path_part = target.split("#", 1)[0]
    if not path_part.endswith(".md"):
        return None
    normalized = posixpath.normpath(posixpath.join(base_dir, path_part))
    if normalized.startswith("/") or ".." in normalized.split("/"):
        return None
    return normalized
