"""Ward's emoji picker: one category at a time in a grid that only builds the cells on screen.

GTK's Gtk.EmojiChooser draws every emoji it knows, about 1900.  With a COLRv1 colour emoji font (Fedora's Noto Color
Emoji, among others) cairo keeps about 100 KB for each emoji drawn, for good, so one GTK chooser cost 400-600 MB.  This
picker draws only what is on screen: the emoji someone actually looks at.  The emoji themselves, with their names,
keywords and categories, come from the table GTK ships for its own chooser."""

from __future__ import annotations

from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from ..i18n import N_, _

# GTK's categories (the component group, skin tones on their own, is left out)
GROUPS = (
    (0, "emoji-people-symbolic", N_("Smileys & People")),
    (1, "emoji-body-symbolic", N_("Body & Clothing")),
    (3, "emoji-nature-symbolic", N_("Animals & Nature")),
    (4, "emoji-food-symbolic", N_("Food & Drink")),
    (5, "emoji-travel-symbolic", N_("Travel & Places")),
    (6, "emoji-activities-symbolic", N_("Activities")),
    (7, "emoji-objects-symbolic", N_("Objects")),
    (8, "emoji-symbols-symbolic", N_("Symbols")),
    (9, "emoji-flags-symbolic", N_("Flags")),
)
RECENT = -1
MAX_RECENT = 30

COLUMNS, ROWS, CELL = 8, 7, 40
_table: list[tuple[str, str, str, int]] | None = None   # (emoji, name, " name keywords" in lower case, group)


def table() -> list[tuple[str, str, str, int]]:
    """GTK's emoji table, read once; empty when this GTK has none we can read."""
    global _table
    if _table is None:
        _table = []
        try:
            data = Gio.resources_lookup_data("/org/gtk/libgtk/emoji/en.data", Gio.ResourceLookupFlags.NONE)
            v = GLib.Variant.new_from_bytes(GLib.VariantType("a(aussasasu)"), data, True)
            for i in range(v.n_children()):
                codes, name, local, keywords, _short, group = v.get_child_value(i).unpack()
                # 0 marks where a skin tone may go: without one, the default (yellow) emoji
                text = "".join(chr(c) for c in codes if c)
                if text and isinstance(name, str):
                    words = " " + " ".join([name, local or "", *keywords]).lower().replace(":", " ")
                    _table.append((text, local or name, words, int(group)))
        except (GLib.Error, TypeError, ValueError):
            _table = []
    return _table


def available() -> bool:
    return bool(table())


class EmojiGrid(Gtk.Widget):
    """A grid of emoji that draws only the rows on screen and scrolls itself.  Gtk.GridView would not do: it keeps
    up to 30 rows of cells measured, and measuring an emoji is what costs (see above)."""

    __gtype_name__ = "WardEmojiGrid"
    __gsignals__ = {"picked": (GObject.SignalFlags.RUN_LAST, None, (str,))}

    def __init__(self):
        super().__init__(focusable=True, can_focus=True, has_tooltip=True, overflow=Gtk.Overflow.HIDDEN,
                         css_classes=["emoji-grid"])
        self.items: list[tuple[str, str]] = []        # (emoji, name)
        self.adjustment = Gtk.Adjustment(lower=0, upper=0, step_increment=CELL, page_increment=CELL * ROWS,
                                         page_size=CELL * ROWS)
        self.adjustment.connect("value-changed", lambda *_: self.queue_draw())
        self.cursor = -1          # keyboard / pointer position
        self._layouts: dict[str, Pango.Layout] = {}
        self.set_size_request(COLUMNS * CELL, ROWS * CELL)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y: self._move_to(self._index_at(x, y), scroll_to=False))
        motion.connect("leave", lambda *_: self._move_to(-1, scroll_to=False))
        self.add_controller(motion)
        click = Gtk.GestureClick()
        click.connect("released", lambda _g, _n, x, y: self._pick(self._index_at(x, y)))
        self.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.connect("query-tooltip", self._on_tooltip)
        self.update_property([Gtk.AccessibleProperty.LABEL], [_("Emoji")])

    def set_items(self, items: list[tuple[str, str]]) -> None:
        self.items = items
        self._layouts.clear()
        rows = (len(items) + COLUMNS - 1) // COLUMNS
        self.adjustment.set_upper(rows * CELL)
        self.adjustment.set_value(0)
        self.cursor = -1
        self.queue_draw()

    # ------------------------------------------------------------------ geometry
    def _index_at(self, x: float, y: float) -> int:
        col, row = int(x // CELL), int((y + self.adjustment.get_value()) // CELL)
        i = row * COLUMNS + col
        return i if 0 <= col < COLUMNS and 0 <= i < len(self.items) else -1

    def _move_to(self, i: int, scroll_to: bool = True) -> None:
        if i == self.cursor:
            return
        self.cursor = i
        if scroll_to and i >= 0:
            top = (i // COLUMNS) * CELL
            v = self.adjustment.get_value()
            if top < v:
                self.adjustment.set_value(top)
            elif top + CELL > v + CELL * ROWS:
                self.adjustment.set_value(top + CELL - CELL * ROWS)
        self.queue_draw()

    # ------------------------------------------------------------------ drawing: only what shows
    def do_snapshot(self, snapshot) -> None:
        height = self.get_height()
        v = self.adjustment.get_value()
        first = int(v // CELL)
        last = min((len(self.items) + COLUMNS - 1) // COLUMNS, int((v + height) // CELL) + 1)
        from gi.repository import Graphene

        if 0 <= self.cursor < len(self.items) and first <= self.cursor // COLUMNS < last:
            color = self.get_color()
            color.alpha = 0.12
            x = (self.cursor % COLUMNS) * CELL
            y = (self.cursor // COLUMNS) * CELL - v
            snapshot.append_color(color, Graphene.Rect().init(x + 2, y + 2, CELL - 4, CELL - 4))
        fg = self.get_color()
        for row in range(first, last):
            for col in range(COLUMNS):
                i = row * COLUMNS + col
                if i >= len(self.items):
                    break
                layout = self._layout(self.items[i][0])
                _ink, logical = layout.get_pixel_extents()
                snapshot.save()
                snapshot.translate(Graphene.Point().init(col * CELL + (CELL - logical.width) / 2,
                                                         row * CELL - v + (CELL - logical.height) / 2))
                snapshot.append_layout(layout, fg)
                snapshot.restore()

    def _layout(self, emoji: str) -> Pango.Layout:
        layout = self._layouts.get(emoji)
        if layout is None:
            layout = self.create_pango_layout(emoji)
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_scale_new(1.6))
            layout.set_attributes(attrs)
            if len(self._layouts) > 4 * COLUMNS * ROWS:
                self._layouts.clear()
            self._layouts[emoji] = layout
        return layout

    # ------------------------------------------------------------------ input
    def _on_scroll(self, _c, _dx, dy) -> bool:
        a = self.adjustment
        a.set_value(max(0, min(a.get_upper() - a.get_page_size(), a.get_value() + dy * CELL)))
        return True

    def _on_key(self, _c, keyval, _code, _state) -> bool:
        step = {Gdk.KEY_Left: -1, Gdk.KEY_Right: 1, Gdk.KEY_Up: -COLUMNS, Gdk.KEY_Down: COLUMNS,
                Gdk.KEY_Page_Up: -COLUMNS * ROWS, Gdk.KEY_Page_Down: COLUMNS * ROWS}.get(keyval)
        if step is not None and self.items:
            self._move_to(max(0, min(len(self.items) - 1, max(self.cursor, 0) + step if self.cursor >= 0 else 0)))
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space) and self.cursor >= 0:
            self._pick(self.cursor)
            return True
        return False

    def _on_tooltip(self, _w, x, y, keyboard, tooltip) -> bool:
        i = self.cursor if keyboard else self._index_at(x, y)
        if i < 0:
            return False
        tooltip.set_text(self.items[i][1])
        return True

    def _pick(self, i: int) -> None:
        if 0 <= i < len(self.items):
            self.emit("picked", self.items[i][0])


class EmojiPicker(Gtk.Popover):
    """A drop-in for Gtk.EmojiChooser: emits "emoji-picked" with the emoji."""

    __gtype_name__ = "WardEmojiPicker"
    __gsignals__ = {"emoji-picked": (GObject.SignalFlags.RUN_LAST, None, (str,))}

    def __init__(self, settings=None):
        super().__init__(css_classes=["emoji-picker"])
        self.settings = settings
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.search = Gtk.SearchEntry(placeholder_text=_("Search emoji"))
        self.search.connect("search-changed", lambda *_: self._show_search())
        self.search.connect("activate", lambda *_: self.grid._pick(0))
        box.append(self.search)
        down = Gtk.EventControllerKey()
        down.connect("key-pressed", lambda _c, k, *_a: k == Gdk.KEY_Down and self._into_grid())
        self.search.add_controller(down)

        self.grid = EmojiGrid()
        self.grid.connect("picked", lambda _g, e: self._picked(e))
        self.pane = Gtk.Box()
        self.pane.append(self.grid)
        self.pane.append(Gtk.Scrollbar(orientation=Gtk.Orientation.VERTICAL, adjustment=self.grid.adjustment))
        box.append(self.pane)
        self.empty = Gtk.Label(label=_("No emoji found"), css_classes=["dim-label"], visible=False,
                               height_request=ROWS * CELL)
        box.append(self.empty)

        tabs = Gtk.Box(spacing=0, homogeneous=True)
        self.tabs: dict[int, Gtk.ToggleButton] = {}
        first = None
        for group, icon, title in ((RECENT, "emoji-recent-symbolic", N_("Recent")), *GROUPS):
            b = Gtk.ToggleButton(icon_name=icon, tooltip_text=_(title), css_classes=["flat"])
            if first is None:
                first = b
            else:
                b.set_group(first)
            b.connect("toggled", lambda b, g=group: b.get_active() and self._show_group(g))
            self.tabs[group] = b
            tabs.append(b)
        box.append(tabs)
        self.set_child(box)
        self.connect("show", lambda *_: self._opened())

    # ------------------------------------------------------------------ contents
    def _recent(self) -> list[str]:
        if self.settings is not None:
            return list(self.settings["chats.recent_emoji"])
        return []

    def _set(self, items: list[tuple[str, str]]) -> None:
        self.grid.set_items(items)
        self.pane.set_visible(bool(items))
        self.empty.set_visible(not items)

    def _show_group(self, group: int) -> None:
        if self.search.get_text():
            self.search.set_text("")        # shows the group through _show_search
            return
        if group == RECENT:
            names = {e: n for e, n, _w, _g in table()}
            self._set([(e, names.get(e, "")) for e in self._recent()])
        else:
            self._set([(e, n) for e, n, _w, g in table() if g == group])

    def _show_search(self) -> None:
        words = [" " + x for x in self.search.get_text().lower().split()]    # each matching the start of a word
        if not words:
            active = next((g for g, b in self.tabs.items() if b.get_active()), GROUPS[0][0])
            self._show_group(active)
            return
        self._set([(e, n) for e, n, w, _g in table() if all(x in w for x in words)])

    def _opened(self) -> None:
        self.search.set_text("")
        group = RECENT if self._recent() else GROUPS[0][0]
        if self.tabs[group].get_active():
            self._show_group(group)
        else:
            self.tabs[group].set_active(True)
        self.search.grab_focus()

    def _into_grid(self) -> bool:
        if self.grid.items:
            self.grid.grab_focus()
            self.grid._move_to(0)
        return True

    # ------------------------------------------------------------------ picking
    def _picked(self, emoji: str) -> None:
        if self.settings is not None:
            recent = [emoji] + [e for e in self._recent() if e != emoji]
            self.settings.set("chats.recent_emoji", recent[:MAX_RECENT])
        self.popdown()
        self.emit("emoji-picked", emoji)
