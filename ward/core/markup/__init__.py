"""Message rendering: Markdown (our clients) and XEP-0393 Message Styling (everyone else) into one render tree."""

from __future__ import annotations

from .parse import parse_markdown
from .plain import preview, to_html, to_plain
from .styling import parse_styling
from .tree import Document

__all__ = ["Document", "parse", "parse_markdown", "parse_styling", "preview", "to_html", "to_plain"]


def parse(text: str, is_markdown: bool, nicknames=None, markdown_from_others: bool = False) -> Document:
    """Pick the right parser for a message: full Markdown when the sender flagged it (or the user asked for it),
    XEP-0393 otherwise, because *text* means bold there but italic in Markdown."""
    if is_markdown or markdown_from_others:
        return parse_markdown(text, nicknames)
    return parse_styling(text, nicknames)
