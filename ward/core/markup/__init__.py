"""Message rendering: Markdown (our clients) and XEP-0393 Message Styling (everyone else) into one render tree."""

from __future__ import annotations

import threading
from collections import OrderedDict

from .parse import parse_markdown
from .plain import preview, preview_text, to_html, to_plain
from .styling import parse_styling
from .tree import Document

__all__ = ["Document", "parse", "parse_markdown", "parse_styling", "preview", "preview_text", "to_html", "to_plain"]

_CACHE_MAX_ENTRIES = 256
_CACHE_MAX_BYTES = 262_144
_CACHE_MAX_ITEM_LEN = 4096
_cache: OrderedDict[tuple[str, bool, frozenset[str] | None], tuple[Document, int]] = OrderedDict()
_cache_bytes = 0
_cache_lock = threading.Lock()


def parse(text: str, is_markdown: bool, nicknames=None, markdown_from_others: bool = False) -> Document:
    """Pick the right parser for a message: full Markdown when the sender flagged it (or the user asked for it),
    XEP-0393 otherwise, because *text* means bold there but italic in Markdown."""
    s = text or ""
    use_md = bool(is_markdown or markdown_from_others)
    if len(s) > _CACHE_MAX_ITEM_LEN:
        return parse_markdown(s, nicknames) if use_md else parse_styling(s, nicknames)
    nicks = frozenset(n.lower() for n in nicknames if n) if (nicknames and "@" in s) else None
    key = (s, use_md, nicks or None)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit[0]
    doc = parse_markdown(s, nicks) if use_md else parse_styling(s, nicks)
    size = len(s)
    global _cache_bytes
    with _cache_lock:
        if key not in _cache:
            _cache[key] = (doc, size)
            _cache_bytes += size
            while len(_cache) > _CACHE_MAX_ENTRIES or _cache_bytes > _CACHE_MAX_BYTES:
                _, (_, dropped) = _cache.popitem(last=False)
                _cache_bytes -= dropped
        else:
            _cache.move_to_end(key)
    return doc
