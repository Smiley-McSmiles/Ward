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
# the five Fitzpatrick modifiers; tone 0 is the default (yellow) emoji
TONES = (0x1F3FB, 0x1F3FC, 0x1F3FD, 0x1F3FE, 0x1F3FF)
HAND = (0x270B, 0)        # raised hand: shows the chosen tone on the tone button
TONE_NAMES = (N_("Default skin tone"), N_("Light skin tone"), N_("Medium-light skin tone"), N_("Medium skin tone"),
              N_("Medium-dark skin tone"), N_("Dark skin tone"))
# (codepoints, name, " name keywords" in lower case, group); a 0 among the codepoints is where a skin tone goes
_table: list[tuple[tuple[int, ...], str, str, int]] | None = None
_by_base: dict[str, tuple[int, ...]] = {}


def text(codes: tuple[int, ...], tone: int = 0) -> str:
    """The emoji, with skin tone 1-5 where it takes one (0: the default)."""
    return "".join(chr(TONES[tone - 1]) if c == 0 and tone else chr(c) for c in codes if c or tone)


def takes_tone(emoji: str) -> tuple[int, ...] | None:
    """The codepoints of an emoji (with or without a tone) that comes in skin tones, None for any other."""
    return _by_base.get("".join(ch for ch in emoji if ord(ch) not in TONES))


def table() -> list[tuple[tuple[int, ...], str, str, int]]:
    """GTK's emoji table, read once; empty when this GTK has none we can read."""
    global _table
    if _table is None:
        _table = []
        try:
            data = Gio.resources_lookup_data("/org/gtk/libgtk/emoji/en.data", Gio.ResourceLookupFlags.NONE)
            v = GLib.Variant.new_from_bytes(GLib.VariantType("a(aussasasu)"), data, True)
            for i in range(v.n_children()):
                codes, name, local, keywords, _short, group = v.get_child_value(i).unpack()
                # where a skin tone goes, GTK's table has a 0 or the light tone: both become 0
                codes = tuple(0 if c == TONES[0] else c for c in codes)
                if any(codes) and isinstance(name, str):
                    words = " " + " ".join([name, local or "", *keywords]).lower().replace(":", " ")
                    _table.append((codes, local or name, words, int(group)))
                    if 0 in codes:
                        _by_base[text(codes)] = codes
        except (GLib.Error, TypeError, ValueError):
            _table = []
    return _table


def available() -> bool:
    return bool(table())


class EmojiGrid(Gtk.Widget):
    """A grid of emoji that draws only the rows on screen and scrolls itself.  Gtk.GridView would not do: it keeps
    up to 30 rows of cells measured, and measuring an emoji is what costs (see above)."""

    __gtype_name__ = "WardEmojiGrid"
    __gsignals__ = {"picked": (GObject.SignalFlags.RUN_LAST, None, (str,)),
                    "variants": (GObject.SignalFlags.RUN_LAST, None, (int,))}     # right click, long press, Menu

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
        other = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        other.connect("pressed", lambda _g, _n, x, y: self._variants(self._index_at(x, y)))
        self.add_controller(other)
        hold = Gtk.GestureLongPress()
        hold.connect("pressed", lambda _g, x, y: self._variants(self._index_at(x, y)))
        self.add_controller(hold)
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
        if keyval in (Gdk.KEY_Menu, Gdk.KEY_F10) and self.cursor >= 0:
            self._variants(self.cursor)
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

    def _variants(self, i: int) -> None:
        if 0 <= i < len(self.items):
            self._move_to(i, scroll_to=False)
            self.emit("variants", i)

    def cell_rect(self, i: int) -> Gdk.Rectangle:
        r = Gdk.Rectangle()
        r.x, r.y = (i % COLUMNS) * CELL, int((i // COLUMNS) * CELL - self.adjustment.get_value())
        r.width = r.height = CELL
        return r


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
        top = Gtk.Box(spacing=6)
        self.search.set_hexpand(True)
        top.append(self.search)
        # the skin tone for every emoji that takes one; right click (or long press) on an emoji for just that one
        self.tone_button = Gtk.MenuButton(css_classes=["flat"], tooltip_text=_("Skin tone"))
        tones = Gtk.Box(spacing=2, margin_start=4, margin_end=4, margin_top=4, margin_bottom=4)
        for tone in range(len(TONE_NAMES)):
            b = Gtk.Button(label=text(HAND, tone), css_classes=["flat"], tooltip_text=_(TONE_NAMES[tone]))
            b.connect("clicked", lambda _b, t=tone: self._set_tone(t))
            tones.append(b)
        self.tone_button.set_popover(Gtk.Popover(child=tones))
        top.append(self.tone_button)
        box.append(top)
        down = Gtk.EventControllerKey()
        down.connect("key-pressed", lambda _c, k, *_a: k == Gdk.KEY_Down and self._into_grid())
        self.search.add_controller(down)

        self.grid = EmojiGrid()
        self.grid.connect("picked", lambda _g, e: self._picked(e))
        self.grid.connect("variants", lambda _g, i: self._show_variants(i))
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
        self._update_tone_button()

    # ------------------------------------------------------------------ contents
    def _recent(self) -> list[str]:
        if self.settings is not None:
            return list(self.settings["chats.recent_emoji"])
        return []

    def _tone(self) -> int:
        if self.settings is not None:
            return max(0, min(len(TONES), int(self.settings["chats.emoji_skin_tone"])))
        return getattr(self, "_own_tone", 0)

    def _set_tone(self, tone: int) -> None:
        if self.settings is not None:
            self.settings.set("chats.emoji_skin_tone", tone)
        else:
            self._own_tone = tone
        self.tone_button.popdown()
        self._update_tone_button()
        self._show_search()        # the same view again, in the new tone

    def _update_tone_button(self) -> None:
        self.tone_button.set_label(text(HAND, self._tone()))

    def _set(self, items: list[tuple[str, str]]) -> None:
        self.grid.set_items(items)
        self.pane.set_visible(bool(items))
        self.empty.set_visible(not items)

    def _show_group(self, group: int) -> None:
        if self.search.get_text():
            self.search.set_text("")        # shows the group through _show_search
            return
        if group == RECENT:
            names = {text(c): n for c, n, _w, _g in table()}
            self._set([(e, names.get(e, "") or names.get(text(takes_tone(e) or ()), "")) for e in self._recent()])
        else:
            tone = self._tone()
            self._set([(text(c, tone), n) for c, n, _w, g in table() if g == group])

    def _show_search(self) -> None:
        words = [" " + x for x in self.search.get_text().lower().split()]    # each matching the start of a word
        if not words:
            active = next((g for g, b in self.tabs.items() if b.get_active()), GROUPS[0][0])
            self._show_group(active)
            return
        tone = self._tone()
        self._set([(text(c, tone), n) for c, n, w, _g in table() if all(x in w for x in words)])

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

    def _show_variants(self, i: int) -> None:
        """The emoji in every skin tone, for this one pick."""
        emoji = self.grid.items[i][0]
        codes = takes_tone(emoji)
        if codes is None:
            return
        pop = Gtk.Popover(position=Gtk.PositionType.TOP)
        pop.set_parent(self.grid)
        pop.set_pointing_to(self.grid.cell_rect(i))
        row = Gtk.Box(spacing=2)
        for tone in range(len(TONE_NAMES)):
            b = Gtk.Button(label=text(codes, tone), css_classes=["flat", "emoji-variant"],
                           tooltip_text=_(TONE_NAMES[tone]))
            b.connect("clicked", lambda _b, e=text(codes, tone): (pop.popdown(), self._picked(e)))
            row.append(b)
        pop.set_child(row)
        # popovers parented by hand must be unparented by hand
        pop.connect("closed", lambda p: GLib.idle_add(lambda: p.get_parent() is not None and p.unparent()))
        pop.popup()

    # ------------------------------------------------------------------ picking
    def _picked(self, emoji: str) -> None:
        if self.settings is not None:
            recent = [emoji] + [e for e in self._recent() if e != emoji]
            self.settings.set("chats.recent_emoji", recent[:MAX_RECENT])
        self.popdown()
        self.emit("emoji-picked", emoji)
