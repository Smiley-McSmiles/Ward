"""XEP-0393 Message Styling: the simple rules Conversations, Dino and Gajim use, for messages without our Markdown
flag.  *strong*, _emphasis_, ~strike~, `pre`, ```code blocks``` and > quotes.  The directive characters stay
visible (dimmed), as the specification recommends."""

from __future__ import annotations

import re

from .parse import _plain_fallback, postprocess_inlines
from .tree import (
    BOLD,
    CODE,
    DIRECTIVE,
    ITALIC,
    STRIKE,
    Block,
    CodeBlock,
    Document,
    Inlines,
    Paragraph,
    Quote,
    Run,
    merge_runs,
)

_STYLE = {"*": BOLD, "_": ITALIC, "~": STRIKE, "`": CODE}
_STYLE_SYNTAX_RE = re.compile(r"[*_~`]")
_EMPTY: frozenset[str] = frozenset()
_MAX_STYLING_LEN = 32_768
_MAX_QUOTE_DEPTH = 32
_MAX_SPAN_DEPTH = 32


def parse_styling(text: str, nicknames: frozenset[str] | set[str] | None = None) -> Document:
    s = text or ""
    if not s or not s.strip():
        return Document([], markdown=False)
    nicks = frozenset(n.lower() for n in (nicknames or ()) if n) if (nicknames and "@" in s) else _EMPTY
    if (
        len(s) <= _MAX_STYLING_LEN
        and "\n" not in s
        and "\r" not in s
        and not s.startswith((">", "```"))
        and _STYLE_SYNTAX_RE.search(s) is None
    ):
        return Document([Paragraph(postprocess_inlines([Run(s)], nicks, [0]))], markdown=False)
    if len(s) > _MAX_STYLING_LEN:
        return _plain_fallback(s, nicks, markdown=False)
    lines = s.replace("\r\n", "\n").split("\n")
    counter = [0]
    blocks = _blocks(lines, nicks, counter)
    return Document(blocks, markdown=False)


def _blocks(lines: list[str], nicks, counter, depth: int = 0) -> list[Block]:
    out: list[Block] = []
    para: list[str] = []

    def flush():
        if para:
            runs: Inlines = []
            for k, line in enumerate(para):
                if k:
                    runs.append(Run("\n"))
                runs.extend(spans(line))
            out.append(Paragraph(postprocess_inlines(merge_runs(runs), nicks, counter)))
            para.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(">") and depth < _MAX_QUOTE_DEPTH:
            flush()
            quoted = []
            while i < len(lines) and lines[i].startswith(">"):
                q = lines[i][1:]
                quoted.append(q[1:] if q.startswith(" ") else q)
                i += 1
            out.append(Quote(_blocks(quoted, nicks, counter, depth + 1)))
            continue
        if line.startswith("```"):
            flush()
            lang = line[3:].strip().split(" ")[0]
            body = []
            i += 1
            while i < len(lines) and lines[i] != "```":
                body.append(lines[i])
                i += 1
            i += 1  # the closing line (or the end of the message)
            out.append(CodeBlock("\n".join(body), lang))
            continue
        para.append(line)
        i += 1
    flush()
    # Leading/trailing empty paragraphs from blank lines are noise
    return [b for b in out if not (isinstance(b, Paragraph) and not "".join(r.text for r in b.inlines).strip())]


def _can_open(s: str, i: int) -> bool:
    if i + 1 >= len(s) or s[i + 1].isspace():
        return False
    return i == 0 or s[i - 1].isspace() or s[i - 1] in _STYLE


def _find_close(s: str, i: int) -> int | None:
    c = s[i]
    pos = i + 2
    while True:
        k = s.find(c, pos)
        if k == -1:
            return None
        if not s[k - 1].isspace():
            return k
        pos = k + 1


def spans(s: str, styles: frozenset[str] = frozenset(), depth: int = 0) -> Inlines:
    if depth >= _MAX_SPAN_DEPTH:
        return [Run(s, styles)] if s else []
    out: Inlines = []
    dead: set[str] = set()
    i = 0
    last = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c in _STYLE and c not in dead and _can_open(s, i):
            j = _find_close(s, i)
            if j is not None:
                if i > last:
                    out.append(Run(s[last:i], styles))
                inner = s[i + 1:j]
                st = styles | {_STYLE[c]}
                dir_st = styles | {DIRECTIVE}
                out.append(Run(c, dir_st))
                if c == "`":
                    out.append(Run(inner, st))
                else:
                    out.extend(spans(inner, st, depth + 1))
                out.append(Run(c, dir_st))
                i = j + 1
                last = i
                continue
            dead.add(c)
        i += 1
    if last < n:
        out.append(Run(s[last:], styles))
    return out
