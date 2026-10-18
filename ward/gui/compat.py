"""libadwaita 1.4 to 1.7+ from one code base.

Ward needs libadwaita 1.4 (Ubuntu 24.04 ships 1.5, Debian 13 1.7, the BSDs 1.6+).  Newer widgets are used when
present, with a close equivalent otherwise: dialogs (Adw.Dialog 1.5 / Adw.Window), alerts (Adw.AlertDialog 1.5 /
Adw.MessageDialog), preferences (Adw.PreferencesDialog 1.5 / Adw.PreferencesWindow), the about dialog, spinners
(Adw.Spinner 1.6 / Gtk.Spinner), button rows (Adw.ButtonRow 1.6) and accent colours (Adw 1.6 or CSS)."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

ADW = (Adw.get_major_version(), Adw.get_minor_version())
GTK = (Gtk.get_major_version(), Gtk.get_minor_version())


def adw(minor: int) -> bool:
    return (1, minor) <= ADW


HAS_DIALOG = hasattr(Adw, "Dialog")                  # 1.5
HAS_ALERT = hasattr(Adw, "AlertDialog")              # 1.5
HAS_PREFS_DIALOG = hasattr(Adw, "PreferencesDialog")  # 1.5
HAS_SPINNER = hasattr(Adw, "Spinner")                # 1.6
HAS_BUTTON_ROW = hasattr(Adw, "ButtonRow")           # 1.6
HAS_BOTTOM_SHEET = hasattr(Adw, "BottomSheet")       # 1.6
HAS_WRAP_BOX = hasattr(Adw, "WrapBox")               # 1.7


def spinner() -> Gtk.Widget:
    if HAS_SPINNER:
        return Adw.Spinner()
    s = Gtk.Spinner()
    s.start()
    return s


class Dialog:
    """A modal dialog: Adw.Dialog where available (it becomes a bottom sheet on phones), else an Adw.Window."""

    def __init__(self, title: str = "", width: int = 480, height: int = -1):
        self.title = title
        if HAS_DIALOG:
            self.widget = Adw.Dialog(title=title, content_width=width)
            if height > 0:
                self.widget.set_content_height(height)
        else:
            self.widget = Adw.Window(title=title, modal=True, default_width=width,
                                     default_height=height if height > 0 else 560)
            self.widget.set_hide_on_close(False)
        self._closed: list[Callable] = []
        sig = "closed" if HAS_DIALOG else "close-request"
        self.widget.connect(sig, self._on_closed)

    def _on_closed(self, *_a):
        for cb in self._closed:
            cb()
        return False

    def on_closed(self, cb: Callable) -> None:
        self._closed.append(cb)

    def set_child(self, child: Gtk.Widget) -> None:
        if HAS_DIALOG:
            self.widget.set_child(child)
        else:
            self.widget.set_content(child)

    def present(self, parent: Gtk.Widget | None) -> None:
        if HAS_DIALOG:
            self.widget.present(parent)
        else:
            root = parent.get_root() if parent is not None else None
            if isinstance(root, Gtk.Window):
                self.widget.set_transient_for(root)
            self.widget.present()

    def close(self) -> None:
        if HAS_DIALOG:
            self.widget.close()
        else:
            self.widget.close()
            self.widget.destroy()

    def set_default_widget(self, w: Gtk.Widget) -> None:
        if HAS_DIALOG:
            self.widget.set_default_widget(w)
        else:
            self.widget.set_default_widget(w)


def alert(parent: Gtk.Widget | None, heading: str, body: str = "", responses: list[tuple[str, str, str]] | None = None,
          callback: Callable[[str], None] | None = None, default: str = "", close: str = "cancel",
          extra: Gtk.Widget | None = None) -> None:
    """responses: [(id, label, appearance)] with appearance '' | 'suggested' | 'destructive'."""
    responses = responses or [("ok", "OK", "suggested")]
    appearances = {"suggested": Adw.ResponseAppearance.SUGGESTED, "destructive": Adw.ResponseAppearance.DESTRUCTIVE,
                   "": Adw.ResponseAppearance.DEFAULT}
    if HAS_ALERT:
        d = Adw.AlertDialog(heading=heading, body=body)
    else:
        root = parent.get_root() if parent is not None else None
        d = Adw.MessageDialog(heading=heading, body=body, transient_for=root if isinstance(root, Gtk.Window) else None,
                              modal=True)
    for rid, label, app in responses:
        d.add_response(rid, label)
        d.set_response_appearance(rid, appearances.get(app, Adw.ResponseAppearance.DEFAULT))
    if default:
        d.set_default_response(default)
    ids = [r[0] for r in responses]
    if close in ids:
        d.set_close_response(close)
    if extra is not None:
        d.set_extra_child(extra)
    if callback is not None:
        d.connect("response", lambda _d, rid: callback(rid))
    if HAS_ALERT:
        d.present(parent)
    else:
        d.present()


def preferences_dialog():
    if HAS_PREFS_DIALOG:
        d = Adw.PreferencesDialog()
        d.set_search_enabled(True)
        return d
    w = Adw.PreferencesWindow()
    w.set_search_enabled(True)
    w.set_modal(True)
    return w


def present_preferences(d, parent: Gtk.Widget) -> None:
    if HAS_PREFS_DIALOG:
        d.present(parent)
    else:
        root = parent.get_root()
        if isinstance(root, Gtk.Window):
            d.set_transient_for(root)
        d.present()


def prefs_push(d, page) -> None:
    """Show a sub-page (an account's settings) inside the preferences."""
    if HAS_PREFS_DIALOG or hasattr(d, "push_subpage"):
        d.push_subpage(page)
    else:
        d.present_subpage(page)


def prefs_pop(d) -> None:
    if hasattr(d, "pop_subpage"):
        d.pop_subpage()
    else:
        d.close_subpage()


def subpage(title: str, child: Gtk.Widget):
    """An Adw.NavigationPage for prefs_push."""
    view = Adw.ToolbarView()
    view.add_top_bar(Adw.HeaderBar())
    view.set_content(child)
    return Adw.NavigationPage(title=title, child=view)


def toast(widget: Gtk.Widget, text: str, timeout: int = 4) -> None:
    """Show a toast on the nearest overlay (the window's, or a dialog's)."""
    w = widget
    while w is not None:
        if isinstance(w, Adw.ToastOverlay):
            w.add_toast(Adw.Toast(title=text, timeout=timeout))
            return
        if HAS_PREFS_DIALOG and isinstance(w, Adw.PreferencesDialog):
            w.add_toast(Adw.Toast(title=text, timeout=timeout))
            return
        if isinstance(w, Adw.PreferencesWindow):
            w.add_toast(Adw.Toast(title=text, timeout=timeout))
            return
        w = w.get_parent()
    root = widget.get_root() if widget is not None else None
    overlay = getattr(root, "toasts", None)
    if overlay is not None:
        overlay.add_toast(Adw.Toast(title=text, timeout=timeout))


def button_row(title: str, icon: str = "", style: str = "") -> Gtk.Widget:
    """A preferences row that is a button (Adw.ButtonRow on 1.6, an activatable Adw.ActionRow before)."""
    if HAS_BUTTON_ROW:
        r = Adw.ButtonRow(title=title)
        if icon:
            r.set_start_icon_name(icon)
        if style:
            r.add_css_class(style)
        return r
    r = Adw.ActionRow(title=title, activatable=True)
    if icon:
        r.add_prefix(Gtk.Image.new_from_icon_name(icon))
    if style:
        r.add_css_class(style)
    return r


def row_activated(row: Gtk.Widget, cb: Callable) -> None:
    row.connect("activated", lambda *_: cb())


def wrap_box(spacing: int = 6) -> Gtk.Widget:
    if HAS_WRAP_BOX:
        return Adw.WrapBox(child_spacing=spacing, line_spacing=spacing)
    fb = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=spacing, row_spacing=spacing,
                     max_children_per_line=30, homogeneous=False)
    return fb


def wrap_append(box: Gtk.Widget, child: Gtk.Widget) -> None:
    if HAS_WRAP_BOX and isinstance(box, Adw.WrapBox):
        box.append(child)
    else:
        box.append(child)


def wrap_clear(box: Gtk.Widget) -> None:
    if HAS_WRAP_BOX and isinstance(box, Adw.WrapBox):
        box.remove_all()
        return
    c = box.get_first_child()
    while c is not None:
        nxt = c.get_next_sibling()
        box.remove(c)
        c = nxt


def set_accent(name: str) -> None:
    """Accent colour from Preferences: libadwaita 1.6 knows system accents; we override with CSS either way."""
    from . import style

    style.set_accent(name)
