"""Render trees into native GTK widgets - never a web view.

Text runs become escaped Pango markup in wrapping, selectable labels; code blocks become read-only GtkSourceView
views (or monospace labels without GtkSourceView); tables become a grid in a horizontal scroller; quotes and
spoilers are boxes styled in style.css.  Parsed trees are cached per message, and long messages are parsed in the
worker pool by the caller."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable

from gi.repository import GLib, Gtk, Pango

from .. import deps
from ..core.markup import parse as parse_markup
from ..core.markup.tree import (
    BOLD,
    CODE,
    DIRECTIVE,
    ITALIC,
    LINK,
    MENTION,
    SPOILER,
    STRIKE,
    CodeBlock,
    Document,
    Heading,
    Inlines,
    ListBlock,
    Paragraph,
    Quote,
    Rule,
    Table,
)
from ..i18n import _

_cache: OrderedDict[tuple, tuple[Document, int]] = OrderedDict()
_cache_chars = 0
_MAX_CACHE_ENTRIES = 2000
_MAX_CACHE_CHARS = 2_000_000
_MAX_HIGHLIGHT_CHARS = 50_000
HEADING_SIZES = {1: "x-large", 2: "large", 3: "large"}


def parsed(key, text: str, is_markdown: bool, nicknames=None, others_markdown: bool = False) -> Document:
    global _cache_chars
    nicks_key = frozenset(nicknames) if (nicknames and "@" in text) else None
    k = (key, len(text), hash(text), is_markdown, others_markdown, nicks_key)
    entry = _cache.get(k)
    if entry is None:
        doc = parse_markup(text, is_markdown, nicknames, others_markdown)
        cost = len(text)
        _cache[k] = (doc, cost)
        _cache_chars += cost
        while len(_cache) > _MAX_CACHE_ENTRIES or (_cache_chars > _MAX_CACHE_CHARS and len(_cache) > 1):
            _old_k, (_old_doc, old_cost) = _cache.popitem(last=False)
            _cache_chars -= old_cost
    else:
        doc = entry[0]
        _cache.move_to_end(k)
    return doc


def inline_code_ranges(runs: Inlines) -> list[tuple[int, int, str]]:
    """Character ranges of inline-code runs in the label's plain text, with their contents."""
    ranges = []
    start = 0
    for r in runs:
        end = start + len(r.text)
        if CODE in r.styles and r.text:
            ranges.append((start, end, r.text))
        start = end
    return ranges


def markup(runs: Inlines, revealed: set[int] | None = None, dark: bool = False) -> str:
    out = []
    for r in runs:
        t = GLib.markup_escape_text(r.text)
        st = r.styles
        if CODE in st:
            t = "<tt><span bgalpha='12%%' background='%s'>%s</span></tt>" % ("#ffffff" if dark else "#000000", t)
        if BOLD in st:
            t = "<b>%s</b>" % t
        if ITALIC in st:
            t = "<i>%s</i>" % t
        if STRIKE in st:
            t = "<s>%s</s>" % t
        if DIRECTIVE in st:
            t = "<span alpha='45%%'>%s</span>" % t
        if MENTION in st:
            t = "<b><span foreground='%s'>%s</span></b>" % ("#99c1f1" if dark else "#1c71d8", t)
        if SPOILER in st and not (revealed and r.spoiler_id in revealed):
            col = "#77767b"
            t = "<span background='%s' foreground='%s' fgalpha='1'>%s</span>" % (col, col, t)
        if LINK in st and r.href:
            t = "<a href=\"%s\">%s</a>" % (GLib.markup_escape_text(r.href), t)
        out.append(t)
    return "".join(out)


class Renderer:
    """Builds the widgets of one message.  on_link(uri, text) decides what clicking a link does."""

    def __init__(self, on_link: Callable[[str, str], bool] | None = None, dark: bool = False,
                 large_emoji: bool = False, action: bool = False,
                 on_code_copy: Callable[[str], None] | None = None):
        self.on_link = on_link
        self.dark = dark
        self.large_emoji = large_emoji
        self.action = action
        self.on_code_copy = on_code_copy

    def build(self, doc: Document) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for b in doc.blocks:
            w = self.block(b)
            if w is not None:
                box.append(w)
        return box

    # ------------------------------------------------------------------ blocks
    def block(self, b) -> Gtk.Widget | None:
        if isinstance(b, Paragraph):
            return self.text(b.inlines)
        if isinstance(b, Heading):
            size = HEADING_SIZES.get(b.level, "medium")
            return self.text(b.inlines, "<span size='%s' weight='bold'>%s</span>" % (
                size, markup(b.inlines, dark=self.dark)))
        if isinstance(b, CodeBlock):
            return self.code(b.text, b.language)
        if isinstance(b, Quote):
            q = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["quote-block"])
            for c in b.children:
                w = self.block(c)
                if w is not None:
                    q.append(w)
            return q
        if isinstance(b, ListBlock):
            # boxes, not a Gtk.Grid: a grid measures its height at its narrowest, so wrapping text inside it
            # made bubbles far too tall
            items = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            for i, item in enumerate(b.items):
                line = Gtk.Box(spacing=6)
                if item.checked is not None:
                    bullet: Gtk.Widget = Gtk.CheckButton(active=bool(item.checked), sensitive=False,
                                                         valign=Gtk.Align.START)
                else:
                    bullet = Gtk.Label(label=("%d." % (b.start + i)) if b.ordered else "•", xalign=1,
                                       css_classes=["message-text"], valign=Gtk.Align.START)
                line.append(bullet)
                inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
                for c in item.children:
                    w = self.block(c)
                    if w is not None:
                        inner.append(w)
                line.append(inner)
                items.append(line)
            return items
        if isinstance(b, Table):
            return self.table(b)
        if isinstance(b, Rule):
            return Gtk.Separator(margin_top=4, margin_bottom=4)
        return None

    def text(self, runs: Inlines, markup_override: str | None = None) -> Gtk.Label:
        lbl = Gtk.Label(xalign=0, wrap=True, selectable=True, css_classes=["message-text"], hexpand=True,
                        max_width_chars=90)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        # focusable, or a selection could not be copied with Ctrl+C
        if self.action:
            lbl.add_css_class("action")
        if markup_override is None and not runs:
            return lbl
        if markup_override is None and len(runs) == 1 and not runs[0].styles and not runs[0].href:
            lbl.set_text(runs[0].text)
            return lbl
        has_link = False
        has_code = False
        has_spoiler = False
        for r in runs:
            st = r.styles
            if LINK in st and r.href:
                has_link = True
            if CODE in st:
                has_code = True
            if SPOILER in st:
                has_spoiler = True
        revealed: set[int] = set()
        lbl.set_markup(markup_override if markup_override is not None else markup(runs, revealed, self.dark))
        if has_link:
            lbl.connect("activate-link", self._link, runs)
        if self.on_code_copy is not None and has_code:
            ranges = inline_code_ranges(runs)
            state = {"code": None}
            click = Gtk.GestureClick(button=1)
            click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            click.connect("pressed", self._code_pressed, lbl, ranges, state)
            click.connect("released", self._code_clicked, lbl, ranges, state)
            lbl.add_controller(click)
        if has_spoiler:
            click = Gtk.GestureClick()

            def reveal(*_a):
                revealed.update(r.spoiler_id for r in runs if SPOILER in r.styles)
                lbl.set_markup(markup(runs, revealed, self.dark))
            click.connect("released", reveal)
            lbl.add_controller(click)
            lbl.set_tooltip_text(_("Spoiler: click to reveal"))
        return lbl

    def _code_at(self, label, ranges, x, y) -> str | None:
        layout = label.get_layout()
        if layout is None:
            return None
        ox, oy = label.get_layout_offsets()
        # Gesture coordinates and the label offsets are pixels; Pango's hit test wants Pango units.
        found, index, _trailing = layout.xy_to_index(int((x - ox) * Pango.SCALE),
                                                     int((y - oy) * Pango.SCALE))
        if not found:
            return None
        text = label.get_text()
        # Pango indexes are byte offsets into the UTF-8 text; the runs and ranges are in characters.
        char_index = len(text.encode("utf-8")[:max(0, index)].decode("utf-8", "ignore"))
        for start, end, code in ranges:
            if start <= char_index < end:
                return code
        return None

    def _code_pressed(self, gesture, n_press, x, y, label, ranges, state) -> None:
        state["code"] = None
        if n_press != 1:
            return
        code = self._code_at(label, ranges, x, y)
        if code is not None:
            state["code"] = code
            # Claim this click so the selectable label does not turn it into a text cursor move.
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _code_clicked(self, _gesture, n_press, _x, _y, label, _ranges, state) -> None:
        code = state["code"]
        state["code"] = None
        if n_press != 1 or code is None:
            return
        label.get_clipboard().set(code)
        if self.on_code_copy is not None:
            self.on_code_copy(code)

    def _link(self, label, uri, runs) -> bool:
        if self.on_link is None:
            return False
        text = "".join(r.text for r in runs if r.href == uri)
        return self.on_link(uri, text)

    def code(self, text: str, language: str = "") -> Gtk.Widget:
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["code-block"])
        head = Gtk.Box(css_classes=["code-header"])
        head.append(Gtk.Label(label=language or _("Code"), xalign=0, hexpand=True))
        copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text=_("Copy"), css_classes=["flat"])
        copy.connect("clicked", lambda b: (b.get_clipboard().set(text), b.set_icon_name("ward-check-symbolic")))
        head.append(copy)
        frame.append(head)
        view = None
        if language and deps.have("source"):
            try:
                lang = _resolve_language(language)
                if lang is not None:
                    view = _source_view(text, language, self.dark, lang=lang)
            except Exception:
                view = None
        if view is None:
            view = Gtk.Label(label=text, xalign=0, selectable=True, css_classes=["message-code"])
            view.set_wrap(False)
        sc = _CodeScroller(view, text)
        frame.append(sc)
        return frame

    def table(self, t: Table) -> Gtk.Widget:
        grid = Gtk.Grid(css_classes=["message-table"], column_homogeneous=False)
        aligns = {"left": 0.0, "center": 0.5, "right": 1.0}
        dark = self.dark
        total_cells = len(t.header) + sum(len(row) for row in t.rows)
        eager_selectable = total_cells <= 60
        for c, cell in enumerate(t.header):
            align = aligns.get(t.align[c] if c < len(t.align) else "", 0.0)
            if len(cell) == 1 and not cell[0].styles and not cell[0].href:
                lbl = Gtk.Label(label=cell[0].text, xalign=align, css_classes=["header"])
            else:
                lbl = Gtk.Label(xalign=align, css_classes=["header"])
                lbl.set_markup(markup(cell, dark=dark))
            grid.attach(lbl, c, 0, 1, 1)
        for r, row in enumerate(t.rows, start=1):
            for c, cell in enumerate(row):
                align = aligns.get(t.align[c] if c < len(t.align) else "", 0.0)
                if len(cell) == 1 and not cell[0].styles and not cell[0].href:
                    plain = cell[0].text
                    lbl = Gtk.Label(label=plain, xalign=align, selectable=eager_selectable,
                                    ellipsize=Pango.EllipsizeMode.END, max_width_chars=60)
                    if len(plain) > 60:
                        lbl.set_tooltip_text(plain)
                else:
                    lbl = Gtk.Label(xalign=align, selectable=eager_selectable,
                                    ellipsize=Pango.EllipsizeMode.END, max_width_chars=60)
                    # no wrapping: the table scrolls sideways, and wrapped cells made it measure as if squeezed
                    lbl.set_markup(markup(cell, dark=dark))
                    if sum(len(run.text) for run in cell) > 60:
                        lbl.set_tooltip_text("".join(run.text for run in cell))
                grid.attach(lbl, c, r, 1, 1)
        if not eager_selectable:
            motion = Gtk.EventControllerMotion()

            def on_motion(_c, x, y) -> None:
                target = grid.pick(x, y, Gtk.PickFlags.DEFAULT)
                if isinstance(target, Gtk.Label) and not target.get_selectable() and not target.has_css_class("header"):
                    target.set_selectable(True)

            motion.connect("motion", on_motion)
            grid.add_controller(motion)
        sc = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                propagate_natural_width=True)
        sc.set_child(grid)
        _forward_wheel_to_parent(sc)
        return sc


def _wheel_step(page_size: float) -> float:
    """How far one wheel detent scrolls, matching GtkScrolledWindow (page_size ** 2/3)."""
    if page_size <= 0:
        return 48.0
    return page_size ** (2.0 / 3.0)


def _forward_vertical_scroll(widget: Gtk.Widget, dy: float) -> bool:
    """Wheel events over a horizontal-only scroller should scroll the message list, not disappear.

    GTK 4.14 (the AppImage) drops an unconsumed vertical wheel event instead of handing it to the message
    list; newer GTK propagates it itself.  This is only installed for the older behaviour, see
    _wheel_needs_forwarding()."""
    parent = widget.get_parent()
    while parent is not None:
        if isinstance(parent, Gtk.ScrolledWindow):
            adjustment = parent.get_vadjustment()
            if adjustment is not None and adjustment.get_upper() > adjustment.get_page_size():
                step = _wheel_step(adjustment.get_page_size())
                value = adjustment.get_value() + dy * step
                value = max(adjustment.get_lower(), min(value, adjustment.get_upper() - adjustment.get_page_size()))
                adjustment.set_value(value)
                return True
        parent = parent.get_parent()
    return False


def _wheel_needs_forwarding() -> bool:
    """True when the running GTK still needs the manual hand-off to the message list.

    Forwarding it *and* letting GTK propagate it made the list jump about twice as far per detent, which
    is what made scrolling over an expanded code block feel so fast."""
    from .compat import GTK
    return GTK < (4, 16)


def _forward_wheel_to_parent(widget: Gtk.Widget) -> None:
    if not _wheel_needs_forwarding():
        return
    controller = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
    controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    controller.connect("scroll", lambda _c, _dx, dy: _forward_vertical_scroll(widget, dy))
    widget.add_controller(controller)


_MONO_FONT: Pango.FontDescription | None = None


def _longest_line_sample(text: str, limit: int = 160) -> str:
    if not text:
        return ""
    best = ""
    best_len = 0
    pos = 0
    n = len(text)
    while pos < n:
        nl = text.find("\n", pos)
        end = n if nl < 0 else nl
        line_len = end - pos
        if line_len > best_len:
            best_len = line_len
            best = text[pos:min(end, pos + limit)]
            if best_len >= limit:
                return best
        if nl < 0:
            break
        pos = nl + 1
    return best


class _CodeScroller(Gtk.ScrolledWindow):
    """Scrolls a code block sideways, but asks for the width of its longest line: a text view only knows its own
    natural width after a first layout, so bubbles would otherwise cut code off."""
    __gtype_name__ = "WardCodeScroller"

    def __init__(self, view: Gtk.Widget, text: str):
        global _MONO_FONT
        super().__init__(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                         propagate_natural_width=True)
        self.set_child(view)
        _forward_wheel_to_parent(self)
        longest = _longest_line_sample(text)
        layout = view.create_pango_layout(longest)
        if _MONO_FONT is None:
            _MONO_FONT = Pango.FontDescription.from_string("monospace")
        layout.set_font_description(_MONO_FONT)
        self.want = min(layout.get_pixel_size()[0] + 28, 900)
        # never squeezed down to a few characters, while still fitting a phone-sized window
        self.set_min_content_width(min(self.want, 240))

    def do_measure(self, orientation, for_size):
        mn, nat, mb, nb = Gtk.ScrolledWindow.do_measure(self, orientation, for_size)
        if orientation == Gtk.Orientation.HORIZONTAL:
            return mn, max(nat, self.want), -1, -1     # a horizontal measure has no baseline
        return mn, nat, mb, nb


_lang_manager = None
_scheme_manager = None
_lang_cache: dict[str, object] = {}
_scheme_cache: dict[str, object] = {}


def _gtksource():
    import gi

    gi.require_version("GtkSource", "5")
    from gi.repository import GtkSource

    return GtkSource


def _resolve_language(language: str):
    global _lang_manager
    key = (language or "").strip().lower()
    if not key:
        return None
    if key in _lang_cache:
        return _lang_cache[key]
    if _lang_manager is None:
        _lang_manager = _gtksource().LanguageManager.get_default()
    lang = _lang_manager.get_language(key) or _lang_manager.guess_language("x." + key, None)
    _lang_cache[key] = lang
    return lang


def _get_style_scheme(dark: bool):
    global _scheme_manager
    name = "Adwaita-dark" if dark else "Adwaita"
    if name in _scheme_cache:
        return _scheme_cache[name]
    if _scheme_manager is None:
        _scheme_manager = _gtksource().StyleSchemeManager.get_default()
    scheme = _scheme_manager.get_scheme(name)
    _scheme_cache[name] = scheme
    return scheme


def _schedule_source_highlight(view, lang) -> None:
    def apply() -> bool:
        if view.get_parent() is None or view.get_root() is None:
            return False
        buf = view.get_buffer()
        if buf is None:
            return False
        buf.set_language(lang)
        buf.set_highlight_syntax(True)
        return False

    GLib.idle_add(apply, priority=GLib.PRIORITY_LOW)


def _source_view(text: str, language: str, dark: bool, lang=None, defer_highlight: bool = True):
    GtkSource = _gtksource()
    buf = GtkSource.Buffer()
    if hasattr(buf, "set_enable_undo"):
        buf.set_enable_undo(False)
    if lang is None and language:
        lang = _resolve_language(language)
    scheme = _get_style_scheme(dark)
    if scheme is not None:
        buf.set_style_scheme(scheme)
    buf.set_highlight_syntax(False)
    buf.set_text(text)
    view = GtkSource.View(buffer=buf, editable=False, cursor_visible=False, monospace=True,
                          wrap_mode=Gtk.WrapMode.NONE, css_classes=["message-code"])
    if lang is not None and len(text) <= _MAX_HIGHLIGHT_CHARS:
        if defer_highlight:
            _schedule_source_highlight(view, lang)
        else:
            buf.set_language(lang)
            buf.set_highlight_syntax(True)
    return view
