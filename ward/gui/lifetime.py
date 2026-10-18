"""Freeing widgets that Python handlers keep alive.

A Python handler that refers back to its own widget, or to the object that built it (an event controller's callback
using self, a button's callback in a dialog), is held by GTK, where Python's garbage collector cannot see it: the
cycle widget -> handler -> owner -> widget is never collected, and everything the widget holds stays in memory.
Rows a list view drops and dialogs that have closed are taken apart here, which breaks those cycles."""

from __future__ import annotations

import weakref

from gi.repository import Adw, GLib, GObject, Gtk

FORGET_AFTER = 30      # seconds after a dialog closes: anything it started (a save, a lookup) has finished by then


def _children(widget: Gtk.Widget) -> list[Gtk.Widget]:
    out = []
    child = widget.get_first_child()
    while child is not None:
        out.append(child)
        child = child.get_next_sibling()
    return out


def release(widget: Gtk.Widget) -> None:
    """Remove the widget's event controllers and empty the containers Ward builds (boxes, overlays), all the way down:
    the Python handlers on them go with them.  The insides of GTK's own widgets are left alone - taking those apart
    can crash GTK - and nothing of Ward's is connected there."""
    controllers = widget.observe_controllers()
    for c in [controllers.get_item(i) for i in range(controllers.get_n_items())]:
        widget.remove_controller(c)
    if isinstance(widget, (Gtk.Box, Gtk.ListBox, Gtk.FlowBox)):
        for c in _children(widget):
            release(c)
            widget.remove(c)
    elif isinstance(widget, Gtk.Overlay):
        main = widget.get_child()
        for c in _children(widget):
            release(c)
            if c is not main:
                widget.remove_overlay(c)
        widget.set_child(None)


def setup_row(item: Gtk.ListItem, row: Gtk.Widget) -> None:
    """Give a list item its row, remembered for teardown_row (GTK has already taken the child away by then)."""
    item.set_child(row)
    item.ward_row = row


def teardown_row(_factory, item: Gtk.ListItem) -> None:
    """A list view dropped one of its rows: unbind it and break its cycles (see release)."""
    row = getattr(item, "ward_row", None) or item.get_child()
    if row is None:
        return
    item.ward_row = None
    if hasattr(row, "unbind"):
        row.unbind()
    release(row)
    item.set_child(None)
    # the row's own references to its widgets (a hover bar, a menu button): each of those holds handlers that
    # refer back to the row, a cycle that runs through GTK where the garbage collector cannot follow it
    vars(row).clear()


# ------------------------------------------------------------------ handlers Ward connects to widgets
_connect = Gtk.Widget.connect


def _tracked_connect(widget, signal: str, callback, *args):
    """Gtk.Widget.connect, remembering the handler: disconnect_all() undoes exactly Ward's handlers and never
    GTK's own (disconnecting those makes GTK complain when it later disconnects them itself)."""
    hid = _connect(widget, signal, callback, *args)
    ids = widget.__dict__.setdefault("_ward_handlers", [])
    ids.append(hid)
    if len(ids) > 64:          # a long-lived widget that connects again and again: forget handlers already gone
        ids[:] = [h for h in ids if GObject.signal_handler_is_connected(widget, h)]
    return hid


Gtk.Widget.connect = _tracked_connect


def disconnect_all(widget: Gtk.Widget) -> None:
    """Disconnect the handlers Ward connected in this widget tree.  For a dialog that has closed: a handler on a
    button that refers to the row the button is in (or to the dialog's owner) keeps both alive through GTK, and only
    disconnecting it breaks that."""
    for hid in widget.__dict__.pop("_ward_handlers", ()):
        if GObject.signal_handler_is_connected(widget, hid):
            widget.disconnect(hid)
    for c in _children(widget):
        disconnect_all(c)


def _closed_signal(dialog: Gtk.Widget) -> str:
    return "closed" if isinstance(dialog, getattr(Adw, "Dialog", ())) else "close-request"


def on_closed(dialog: Gtk.Widget, callback) -> None:
    """callback() once the dialog closes: the place to disconnect from long-lived objects (the account list,
    an account), which would otherwise keep the dialog's handlers, and so the dialog, alive."""
    dialog.connect(_closed_signal(dialog), lambda *_a: callback() and False)


def forget_when_closed(owner: object, dialog: Gtk.Widget) -> None:
    """Once the dialog has closed (and a little later), drop the owner's references to its widgets: the handlers
    that refer to the owner then only live in widgets GTK frees, and the dialog and its owner can go."""
    def closed(closed_dialog, *_a) -> bool:
        ref = weakref.ref(closed_dialog)      # no strong reference from a handler on the dialog to itself

        def later() -> bool:
            d = ref()
            if d is not None:
                disconnect_all(d)
            vars(owner).clear()
            return False
        GLib.timeout_add_seconds(FORGET_AFTER, later)
        return False
    dialog.connect(_closed_signal(dialog), closed)
