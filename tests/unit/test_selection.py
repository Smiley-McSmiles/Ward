"""Copy in the message menu copies what the user selected in the message, if anything."""

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from ward.gui.conversation import selected_text


def test_selected_text_walks_the_message():
    box = Gtk.Box()
    a = Gtk.Label(label="hello world", selectable=True)
    b = Gtk.Label(label="second line", selectable=True)
    inner = Gtk.Box()
    inner.append(b)
    box.append(a)
    box.append(inner)
    assert selected_text(box) == ""
    a.select_region(6, 11)
    assert selected_text(box) == "world"
    b.select_region(0, 6)          # one selection at a time, as GTK keeps it
    assert selected_text(box) in ("second", "world\nsecond")
