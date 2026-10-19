"""Bubbles: messages you sent sit on the right, messages you received on the left."""

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk

from ward.core.models import ChatItem, MessageItem
from ward.gui.message_row import MessageRow
from ward.settings import Settings


class FakeView:
    """What MessageRow needs from ConversationView, without building the whole chat view."""

    def __init__(self):
        self.settings = Settings()
        self.settings["appearance.layout"] = "bubbles"
        self.settings["appearance.avatars"] = "always"
        self.chat = ChatItem(id=1, type="chat")
        self.first_unread_id = 0
        self.highlight_id = 0

    def _layout_changing(self):
        pass

    def previous(self, _m):
        return None

    def display_name(self, m):
        return "Sam" if m.direction == 0 else "Alex"

    def avatar_path(self, _m):
        return ""

    def mention_names(self):
        return set()

    def on_link(self, *_a):
        pass

    def on_code_copy(self, *_a):
        pass

    def message_menu(self, *_a):
        pass

    def react(self, *_a):
        pass

    def start_reply(self, *_a):
        pass


def _settle() -> None:
    # a frame, so the window and its rows have been measured
    loop = GLib.MainLoop()
    GLib.timeout_add(250, loop.quit)
    loop.run()


def test_sent_bubbles_are_right_aligned(display):
    view = FakeView()
    incoming = MessageRow(view)
    incoming.bind(MessageItem(id=1, chat_id=1, timestamp=1000, direction=0, sender="sam@example.org",
                              sender_jid="sam@example.org", body="hello there"), None)
    outgoing = MessageRow(view)
    outgoing.bind(MessageItem(id=2, chat_id=1, timestamp=2000, direction=1, sender="me@example.org",
                              sender_jid="me@example.org", body="this one is mine"), None)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.append(incoming)
    box.append(outgoing)
    win = Gtk.Window(child=box, default_width=700, default_height=300)
    win.present()
    _settle()
    try:
        in_wrap = incoming.row.get_last_child()
        out_wrap = outgoing.row.get_last_child()
        in_bubble = in_wrap.get_first_child()
        out_bubble = out_wrap.get_first_child()
        assert in_bubble.has_css_class("message-content") and out_bubble.has_css_class("message-content")
        ok, in_box = in_bubble.compute_bounds(box)
        assert ok
        ok, out_box = out_bubble.compute_bounds(box)
        assert ok
        width = box.get_width()
        assert in_box.get_x() < width * 0.4, "a received bubble starts on the left"
        assert out_box.get_x() > width * 0.5, "a sent bubble starts on the right"
        assert out_box.get_x() + out_box.get_width() <= width, "a sent bubble stays inside the row"
        assert incoming.actions.get_halign() == Gtk.Align.END
        assert outgoing.actions.get_halign() == Gtk.Align.END
        assert incoming.actions.get_parent() is incoming.overlay
    finally:
        win.destroy()
