"""Memory: rows a list view drops are freed, and a chat no longer shown keeps only its newest messages."""

import gc
import weakref

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from ward.core.models import ChatItem, MessageItem, MessageList
from ward.gui import widgets


class Row(Gtk.Box):
    __gtype_name__ = "WardTestLeakRow"

    def __init__(self):
        super().__init__()
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self.poke())       # a handler that refers back to the row
        self.add_controller(motion)
        button = Gtk.Button()
        button.connect("clicked", lambda *_: self.poke())
        self.append(button)
        self.bar = button                                     # and the row refers to the button

    def poke(self):
        pass


def _settle():
    for _ in range(3):
        gc.collect()
        while GLib.MainContext.default().iteration(False):
            pass


def _show_list() -> list:
    """A window with a list of 50 rows, shown and then closed; returns weak references to the rows."""
    alive = []
    factory = Gtk.SignalListItemFactory()

    def setup(_f, item):
        row = Row()
        alive.append(weakref.ref(row))
        widgets.setup_row(item, row)
    factory.connect("setup", setup)
    factory.connect("teardown", widgets.teardown_row)
    store = Gtk.StringList.new([str(i) for i in range(50)])
    view = Gtk.ListView(factory=factory, model=Gtk.NoSelection.new(store))
    win = Gtk.Window(child=view, default_width=300, default_height=300)
    win.present()
    _settle()
    assert alive
    view.set_model(None)
    win.destroy()
    return alive


def test_list_view_rows_are_freed(display):
    alive = _show_list()
    _settle()
    assert not [r for r in alive if r() is not None]


def test_message_list_trim():
    ml = MessageList(ChatItem(id=1))
    ml.add_many([MessageItem(id=i, chat_id=1, timestamp=i * 1000) for i in range(1, 251)])
    ml.has_older = False
    ml.trim(100)
    assert len(ml) == 100 and ml.items()[0].id == 151 and ml.items()[-1].id == 250
    assert ml.get(10) is None and ml.get(200) is not None and ml.has_older
    ml.add(MessageItem(id=300, chat_id=1, timestamp=300_000))
    assert ml.items()[-1].id == 300
