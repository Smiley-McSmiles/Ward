"""Render trees as plain text (notifications, chat list previews, the search index, copying) and as HTML (chat
export).  Spoilers are hidden in previews so a notification never gives them away."""

from __future__ import annotations

import html

from .tree import (BOLD, CODE, ITALIC, LINK, SPOILER, STRIKE, Block, CodeBlock, Document, Heading, Inlines,
                   ListBlock, Paragraph, Quote, Rule, Table)

SPOILER_MASK = "▒▒▒"


def inlines_text(runs: Inlines, hide_spoilers: bool) -> str:
    out = []
    last_spoiler = 0
    for r in runs:
        if SPOILER in r.styles and hide_spoilers:
            if r.spoiler_id != last_spoiler:
                out.append(SPOILER_MASK)
            last_spoiler = r.spoiler_id
            continue
        last_spoiler = 0
        out.append(r.text)
    return "".join(out)


def to_plain(doc: Document, hide_spoilers: bool = False) -> str:
    return "\n".join(_blocks(doc.blocks, hide_spoilers)).strip("\n")


def _blocks(blocks: list[Block], hide: bool) -> list[str]:
    lines: list[str] = []
    for b in blocks:
        if isinstance(b, (Paragraph, Heading)):
            lines.extend(inlines_text(b.inlines, hide).split("\n"))
        elif isinstance(b, CodeBlock):
            lines.extend(b.text.split("\n"))
        elif isinstance(b, Quote):
            lines.extend("> " + x if x else ">" for x in _blocks(b.children, hide))
        elif isinstance(b, ListBlock):
            for k, it in enumerate(b.items):
                if it.checked is not None:
                    bullet = "☑ " if it.checked else "☐ "
                else:
                    bullet = "%d. " % (b.start + k) if b.ordered else "• "
                sub = _blocks(it.children, hide) or [""]
                lines.append(bullet + sub[0])
                lines.extend("   " + x for x in sub[1:])
        elif isinstance(b, Table):
            if b.header:
                lines.append(" | ".join(inlines_text(c, hide) for c in b.header))
            for row in b.rows:
                lines.append(" | ".join(inlines_text(c, hide) for c in row))
        elif isinstance(b, Rule):
            lines.append("———")
    return lines


def preview(doc: Document, limit: int = 120) -> str:
    """One line for the chat list and notifications."""
    text = " ".join(x.strip() for x in to_plain(doc, hide_spoilers=True).split("\n") if x.strip())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ------------------------------------------------------------------------------------------------- HTML export
def inlines_html(runs: Inlines) -> str:
    out = []
    for r in runs:
        t = html.escape(r.text).replace("\n", "<br>")
        if CODE in r.styles:
            t = "<code>%s</code>" % t
        if BOLD in r.styles:
            t = "<strong>%s</strong>" % t
        if ITALIC in r.styles:
            t = "<em>%s</em>" % t
        if STRIKE in r.styles:
            t = "<del>%s</del>" % t
        if SPOILER in r.styles:
            t = '<span class="spoiler">%s</span>' % t
        if LINK in r.styles and r.href:
            t = '<a href="%s" rel="noreferrer noopener">%s</a>' % (html.escape(r.href, quote=True), t)
        out.append(t)
    return "".join(out)


def to_html(doc: Document) -> str:
    return "\n".join(_html_blocks(doc.blocks))


def _html_blocks(blocks: list[Block]) -> list[str]:
    out = []
    for b in blocks:
        if isinstance(b, Paragraph):
            out.append("<p>%s</p>" % inlines_html(b.inlines))
        elif isinstance(b, Heading):
            lvl = min(max(b.level + 2, 3), 6)
            out.append("<h%d>%s</h%d>" % (lvl, inlines_html(b.inlines), lvl))
        elif isinstance(b, CodeBlock):
            out.append("<pre><code>%s</code></pre>" % html.escape(b.text))
        elif isinstance(b, Quote):
            out.append("<blockquote>%s</blockquote>" % "".join(_html_blocks(b.children)))
        elif isinstance(b, ListBlock):
            tag = "ol" if b.ordered else "ul"
            items = []
            for it in b.items:
                box = "" if it.checked is None else ("☑ " if it.checked else "☐ ")
                items.append("<li>%s%s</li>" % (box, "".join(_html_blocks(it.children))))
            out.append("<%s>%s</%s>" % (tag, "".join(items), tag))
        elif isinstance(b, Table):
            head = "".join("<th>%s</th>" % inlines_html(c) for c in b.header)
            rows = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % inlines_html(c) for c in row) for row in b.rows)
            out.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (head, rows))
        elif isinstance(b, Rule):
            out.append("<hr>")
    return out
