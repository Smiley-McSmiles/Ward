"""XEP-0393 Message Styling: the simple rules Conversations, Dino and Gajim use, for messages without our Markdown
flag.  *strong*, _emphasis_, ~strike~, `pre`, ```code blocks``` and > quotes.  The directive characters stay
visible (dimmed), as the specification recommends."""

from __future__ import annotations

from .parse import postprocess_inlines
from .tree import (BOLD, CODE, DIRECTIVE, ITALIC, STRIKE, Block, CodeBlock, Document, Inlines, Paragraph, Quote,
                   Run, merge_runs)

_STYLE = {"*": BOLD, "_": ITALIC, "~": STRIKE, "`": CODE}


def parse_styling(text: str, nicknames: frozenset[str] | set[str] | None = None) -> Document:
    lines = (text or "").replace("\r\n", "\n").split("\n")
    nicks = frozenset(n.lower() for n in (nicknames or ()) if n)
    counter = [0]
    blocks = _blocks(lines, nicks, counter)
    return Document(blocks, markdown=False)


def _blocks(lines: list[str], nicks, counter) -> list[Block]:
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
        if line.startswith(">"):
            flush()
            quoted = []
            while i < len(lines) and lines[i].startswith(">"):
                q = lines[i][1:]
                quoted.append(q[1:] if q.startswith(" ") else q)
                i += 1
            out.append(Quote(_blocks(quoted, nicks, counter)))
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
    for k in range(i + 2, len(s)):
        if s[k] == c and not s[k - 1].isspace():
            return k
    return None


def spans(s: str, styles: frozenset[str] = frozenset()) -> Inlines:
    out: Inlines = []
    buf = []
    i = 0
    while i < len(s):
        c = s[i]
        if c in _STYLE and _can_open(s, i):
            j = _find_close(s, i)
            if j is not None:
                if buf:
                    out.append(Run("".join(buf), styles))
                    buf = []
                inner = s[i + 1:j]
                st = styles | {_STYLE[c]}
                out.append(Run(c, styles | {DIRECTIVE}))
                if c == "`":
                    out.append(Run(inner, st))
                else:
                    out.extend(spans(inner, st))
                out.append(Run(c, styles | {DIRECTIVE}))
                i = j + 1
                continue
        buf.append(c)
        i += 1
    if buf:
        out.append(Run("".join(buf), styles))
    return out
