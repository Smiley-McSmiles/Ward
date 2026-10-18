"""The render tree: what a message looks like, independent of how it was written (Markdown or XEP-0393) and of
how it is drawn (GTK widgets, plain text for notifications and search, HTML for export).

Inline text is a flat list of Runs, each with a set of styles, which maps directly onto Pango markup.  Blocks
nest (quotes and list items contain blocks)."""

from __future__ import annotations

from dataclasses import dataclass, field

# Inline styles
BOLD, ITALIC, STRIKE, CODE, SPOILER, LINK, MENTION, DIRECTIVE = (
    "bold", "italic", "strike", "code", "spoiler", "link", "mention", "directive")


@dataclass
class Run:
    text: str
    styles: frozenset[str] = frozenset()
    href: str = ""
    spoiler_id: int = 0          # runs of one spoiler share an id, so they are revealed together

    def with_style(self, *styles: str) -> Run:
        return Run(self.text, self.styles | frozenset(styles), self.href, self.spoiler_id)


Inlines = list[Run]


@dataclass
class Block:
    pass


@dataclass
class Paragraph(Block):
    inlines: Inlines = field(default_factory=list)


@dataclass
class Heading(Block):
    level: int = 1
    inlines: Inlines = field(default_factory=list)


@dataclass
class CodeBlock(Block):
    text: str = ""
    language: str = ""


@dataclass
class Quote(Block):
    children: list[Block] = field(default_factory=list)


@dataclass
class ListItem:
    children: list[Block] = field(default_factory=list)
    checked: bool | None = None  # None: not a task list item


@dataclass
class ListBlock(Block):
    ordered: bool = False
    start: int = 1
    items: list[ListItem] = field(default_factory=list)


@dataclass
class Table(Block):
    header: list[Inlines] = field(default_factory=list)
    rows: list[list[Inlines]] = field(default_factory=list)
    align: list[str] = field(default_factory=list)    # "" | left | center | right


@dataclass
class Rule(Block):
    pass


@dataclass
class Document:
    blocks: list[Block] = field(default_factory=list)
    markdown: bool = True        # False: parsed as XEP-0393 Message Styling

    def __bool__(self) -> bool:
        return bool(self.blocks)


def merge_runs(runs: Inlines) -> Inlines:
    """Join neighbouring runs that look the same (fewer Pango spans, simpler tests)."""
    out: Inlines = []
    for r in runs:
        if not r.text:
            continue
        if out and out[-1].styles == r.styles and out[-1].href == r.href and out[-1].spoiler_id == r.spoiler_id:
            out[-1] = Run(out[-1].text + r.text, r.styles, r.href, r.spoiler_id)
        else:
            out.append(r)
    return out


def inline_text(runs: Inlines) -> str:
    return "".join(r.text for r in runs)


def links(doc: Document) -> list[str]:
    """Every link target in the document, in order, without duplicates (for link previews)."""
    seen: list[str] = []

    def walk_inlines(runs):
        for r in runs:
            if r.href and r.href not in seen:
                seen.append(r.href)

    def walk(blocks):
        for b in blocks:
            if isinstance(b, (Paragraph, Heading)):
                walk_inlines(b.inlines)
            elif isinstance(b, Quote):
                walk(b.children)
            elif isinstance(b, ListBlock):
                for it in b.items:
                    walk(it.children)
            elif isinstance(b, Table):
                for cell in b.header:
                    walk_inlines(cell)
                for row in b.rows:
                    for cell in row:
                        walk_inlines(cell)

    walk(doc.blocks)
    return seen
