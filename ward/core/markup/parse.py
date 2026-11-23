"""Markdown -> render tree, with markdown-it-py (CommonMark).

Enabled beyond CommonMark: pipe tables, ~~strikethrough~~, task lists (- [x] done), ||spoilers||, bare links,
@mentions of known nicknames, and newlines as line breaks (chat messages are not prose).  Raw HTML is disabled at
the parser, so it is shown as text; images are turned into links and never loaded."""

from __future__ import annotations

import re
from functools import cache

from .tree import (
    BOLD,
    CODE,
    ITALIC,
    LINK,
    MENTION,
    SPOILER,
    STRIKE,
    Block,
    CodeBlock,
    Document,
    Heading,
    Inlines,
    ListBlock,
    ListItem,
    Paragraph,
    Quote,
    Rule,
    Run,
    Table,
    merge_runs,
)

SAFE_SCHEMES = ("http://", "https://", "xmpp:", "mailto:", "geo:", "tel:", "aesgcm://")
URL_RE = re.compile(
    r"(?<![\w@/])((?:https?://|aesgcm://|www\.)[^\s<>\"'`]+|xmpp:[^\s<>\"'`]+|geo:-?\d[\d.,;=a-z-]*)",
    re.IGNORECASE)
_TRAILING = ".,;:!?)]}'\""
TASK_RE = re.compile(r"^\[([ xX])\]\s+")
_EMPTY: frozenset[str] = frozenset()
_MD_SYNTAX_RE = re.compile(r"[*_~`\[\]<>!&\\\x85\u2028\u2029]")
_MAX_MARKDOWN_LEN = 32_768
_MAX_DELIM_COUNT = 2_000


@cache
def _md():
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": False, "breaks": True, "linkify": False, "typographer": False})
    md.enable(["table", "strikethrough"])
    # Indented code blocks surprise people who indent a line in a chat; fenced code blocks are the way.
    md.disable(["code"])
    return md


def safe_href(href: str) -> str:
    h = (href or "").strip()
    low = h.lower()
    if low.startswith("www."):
        return "https://" + h
    return h if low.startswith(SAFE_SCHEMES) else ""


def _plain_fallback(s: str, nicks: frozenset[str], markdown: bool, max_blocks: int = 512) -> Document:
    """Bounded plain-paragraph fallback for huge or delimiter-heavy inputs so the parser cannot stall."""
    text = s.replace("\r\n", "\n").replace("\r", "\n")
    parts = [p for p in text.split("\n\n") if p.strip()]
    if not parts:
        return Document([], markdown=markdown)
    if len(parts) > max_blocks:
        parts = [*parts[: max_blocks - 1], "\n\n".join(parts[max_blocks - 1:])]
    allow_spoilers = "||" in text and text.count("||") <= 200
    counter = [0]
    blocks: list[Block] = []
    for p in parts:
        runs: Inlines = [Run(p)]
        if allow_spoilers:
            runs = _spoilers(runs, counter)
        if len(p) <= _MAX_MARKDOWN_LEN * 2:
            runs = _autolinks(runs)
            if nicks:
                runs = _mentions(runs, nicks)
        blocks.append(Paragraph(merge_runs(runs)))
    return Document(blocks, markdown=markdown)


def _is_plain_md_line(s: str) -> bool:
    if "\n" in s or "\r" in s or "\t" in s or "\x00" in s or "\x0b" in s or "\x0c" in s:
        return False
    if s[0].isspace() or s[-1].isspace():
        return False
    if s[0] in "#>+-*_=~`|" or s[0].isdigit():
        return False
    return _MD_SYNTAX_RE.search(s) is None


def parse_markdown(text: str, nicknames: frozenset[str] | set[str] | None = None) -> Document:
    s = text or ""
    if not s or not s.strip():
        return Document([], markdown=True)
    nicks = frozenset(n.lower() for n in (nicknames or ()) if n) if (nicknames and "@" in s) else _EMPTY
    if _is_plain_md_line(s) and len(s) <= _MAX_MARKDOWN_LEN:
        counter = [0]
        return Document([Paragraph(postprocess_inlines([Run(s)], nicks, counter))], markdown=True)
    if len(s) > _MAX_MARKDOWN_LEN or (
        len(s) > _MAX_DELIM_COUNT
        and (s.count("[") + s.count("]") + s.count("*") + s.count("_") + s.count("~")) > _MAX_DELIM_COUNT
    ):
        return _plain_fallback(s, nicks, markdown=True)
    tokens = _md().parse(s)
    blocks = _convert(tokens)
    counter = [0]
    _postprocess(blocks, nicks, counter)
    return Document(blocks, markdown=True)


# ---------------------------------------------------------------------------------------------- block level
def _convert(tokens) -> list[Block]:
    root: list[Block] = []
    containers: list[list[Block]] = [root]
    lists: list[ListBlock] = []
    items: list[ListItem] = []
    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]
        typ = t.type
        if typ == "paragraph_open":
            containers[-1].append(Paragraph(_inlines(tokens[i + 1].children or [])))
            i += 3
            continue
        if typ == "heading_open":
            level = int(t.tag[1]) if t.tag[1:].isdigit() else 1
            containers[-1].append(Heading(level, _inlines(tokens[i + 1].children or [])))
            i += 3
            continue
        if typ in ("fence", "code_block"):
            lang = (t.info or "").strip().split(" ")[0] if typ == "fence" else ""
            containers[-1].append(CodeBlock(t.content[:-1] if t.content.endswith("\n") else t.content, lang))
        elif typ == "blockquote_open":
            q = Quote()
            containers[-1].append(q)
            containers.append(q.children)
        elif typ == "blockquote_close":
            containers.pop()
        elif typ in ("bullet_list_open", "ordered_list_open"):
            start = t.attrGet("start") if typ == "ordered_list_open" else None
            lb = ListBlock(ordered=typ == "ordered_list_open", start=int(start) if start else 1)
            containers[-1].append(lb)
            lists.append(lb)
        elif typ in ("bullet_list_close", "ordered_list_close"):
            lists.pop()
        elif typ == "list_item_open":
            item = ListItem()
            lists[-1].items.append(item)
            items.append(item)
            containers.append(item.children)
        elif typ == "list_item_close":
            containers.pop()
            _task(items.pop())
        elif typ == "hr":
            containers[-1].append(Rule())
        elif typ == "table_open":
            table, i = _table(tokens, i)
            containers[-1].append(table)
            continue
        i += 1
    return root


def _task(item: ListItem) -> None:
    if not item.children or not isinstance(item.children[0], Paragraph):
        return
    p = item.children[0]
    if not p.inlines or p.inlines[0].styles:
        return
    m = TASK_RE.match(p.inlines[0].text)
    if m:
        item.checked = m.group(1) != " "
        rest = p.inlines[0].text[m.end():]
        p.inlines = ([Run(rest, p.inlines[0].styles)] if rest else []) + p.inlines[1:]


def _table(tokens, i):
    table = Table()
    row: list[Inlines] | None = None
    in_head = False
    while i < len(tokens):
        t = tokens[i]
        typ = t.type
        if typ == "thead_open":
            in_head = True
        elif typ == "thead_close":
            in_head = False
        elif typ == "tr_open":
            row = []
        elif typ == "tr_close":
            if in_head:
                table.header = row or []
            else:
                table.rows.append(row or [])
            row = None
        elif typ in ("th_open", "td_open"):
            style = t.attrGet("style") or ""
            m = re.search(r"text-align:\s*(left|center|right)", str(style))
            if in_head:
                table.align.append(m.group(1) if m else "")
            cell = _inlines(tokens[i + 1].children or []) if i + 1 < len(tokens) and tokens[i + 1].type == "inline" \
                else []
            if row is not None:
                row.append(cell)
        elif typ == "table_close":
            return table, i + 1
        i += 1
    return table, i


# ---------------------------------------------------------------------------------------------- inline level
def _inlines(children) -> Inlines:
    runs: Inlines = []
    styles: list[str] = []
    hrefs: list[str] = []
    cur_styles: frozenset[str] = _EMPTY

    def add(text, extra=None):
        href = hrefs[-1] if hrefs else ""
        st = cur_styles
        if extra is not None:
            st = st | extra
        if href:
            st = st | {LINK}
        runs.append(Run(text, st, href))

    for c in children:
        typ = c.type
        if typ == "text":
            add(c.content)
        elif typ in ("softbreak", "hardbreak"):
            add("\n")
        elif typ == "code_inline":
            add(c.content, frozenset((CODE,)))
        elif typ in ("strong_open", "em_open", "s_open"):
            styles.append({"strong_open": BOLD, "em_open": ITALIC, "s_open": STRIKE}[typ])
            cur_styles = frozenset(styles)
        elif typ in ("strong_close", "em_close", "s_close"):
            want = {"strong_close": BOLD, "em_close": ITALIC, "s_close": STRIKE}[typ]
            for k in range(len(styles) - 1, -1, -1):
                if styles[k] == want:
                    del styles[k]
                    break
            cur_styles = frozenset(styles) if styles else _EMPTY
        elif typ == "link_open":
            hrefs.append(safe_href(c.attrGet("href") or ""))
        elif typ == "link_close":
            if hrefs:
                hrefs.pop()
        elif typ == "image":
            src = safe_href(c.attrGet("src") or "")
            alt = c.content or src
            hrefs.append(src)
            add(alt or src)
            hrefs.pop()
        else:  # html_inline (only if someone enables HTML), and anything new: show the source text
            add(c.content or "")
    return merge_runs(runs)


def _postprocess(blocks: list[Block], nicks: frozenset[str], counter: list[int]) -> None:
    for b in blocks:
        if isinstance(b, (Paragraph, Heading)):
            b.inlines = postprocess_inlines(b.inlines, nicks, counter)
        elif isinstance(b, Quote):
            _postprocess(b.children, nicks, counter)
        elif isinstance(b, ListBlock):
            for it in b.items:
                _postprocess(it.children, nicks, counter)
        elif isinstance(b, Table):
            b.header = [postprocess_inlines(c, nicks, counter) for c in b.header]
            b.rows = [[postprocess_inlines(c, nicks, counter) for c in row] for row in b.rows]


def postprocess_inlines(runs: Inlines, nicks: frozenset[str], counter: list[int]) -> Inlines:
    runs = _spoilers(runs, counter)
    runs = _autolinks(runs)
    if nicks:
        runs = _mentions(runs, nicks)
    return merge_runs(runs)


def _spoilers(runs: Inlines, counter: list[int]) -> Inlines:
    total = 0
    for r in runs:
        if CODE not in r.styles and "||" in r.text:
            total += r.text.count("||")
    usable = total - (total % 2)
    if usable == 0:
        return runs
    out: Inlines = []
    seen = 0
    active = 0
    for r in runs:
        if CODE in r.styles or "||" not in r.text:
            out.append(Run(r.text, r.styles | {SPOILER}, r.href, active) if active else r)
            continue
        parts = r.text.split("||")
        for k, part in enumerate(parts):
            if k > 0:
                seen += 1
                if seen <= usable:
                    if active:
                        active = 0
                    else:
                        counter[0] += 1
                        active = counter[0]
                else:
                    out.append(Run("||", r.styles, r.href))
            if part:
                out.append(Run(part, r.styles | ({SPOILER} if active else _EMPTY), r.href, active))
    return out


def _autolinks(runs: Inlines) -> Inlines:
    out: Inlines = []
    for r in runs:
        if r.href or CODE in r.styles or (":" not in r.text and "." not in r.text):
            out.append(r)
            continue
        pos = 0
        for m in URL_RE.finditer(r.text):
            url = m.group(1)
            while url and url[-1] in _TRAILING:
                if url[-1] == ")" and url.count("(") >= url.count(")"):
                    break
                url = url[:-1]
            start, end = m.start(1), m.start(1) + len(url)
            href = safe_href(url)
            if not href:
                continue
            if start > pos:
                out.append(Run(r.text[pos:start], r.styles, "", r.spoiler_id))
            out.append(Run(url, r.styles | {LINK}, href, r.spoiler_id))
            pos = end
        if pos == 0:
            out.append(r)
        elif pos < len(r.text):
            out.append(Run(r.text[pos:], r.styles, "", r.spoiler_id))
    return out


MENTION_RE = re.compile(r"(?<![\w@])@([^\s@,:;!?()\[\]{}<>\"']+)")


def _mentions(runs: Inlines, nicks: frozenset[str]) -> Inlines:
    out: Inlines = []
    for r in runs:
        if r.href or CODE in r.styles or "@" not in r.text:
            out.append(r)
            continue
        pos = 0
        for m in MENTION_RE.finditer(r.text):
            name = m.group(1).rstrip(".")
            if name.lower() not in nicks:
                continue
            start, end = m.start(), m.start(1) + len(name)
            if start > pos:
                out.append(Run(r.text[pos:start], r.styles, "", r.spoiler_id))
            out.append(Run(r.text[start:end], r.styles | {MENTION}, "", r.spoiler_id))
            pos = end
        if pos == 0:
            out.append(r)
        elif pos < len(r.text):
            out.append(Run(r.text[pos:], r.styles, "", r.spoiler_id))
    return out
