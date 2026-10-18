"""Chats and new messages slide in, and the preferences can turn that off."""

import time

import pytest
from helpers import wait_for
from test_gui import app  # noqa: F401 - the fixture

pytestmark = [pytest.mark.integration, pytest.mark.gui]


def test_chat_and_message_animations_can_be_turned_off(app, run):  # noqa: F811
    from gi.repository import Gtk

    async def scenario():
        from ward.core.chats import insert_message_job

        await wait_for(lambda: app.core.started, 20, "core start")
        Gtk.Settings.get_default().set_property("gtk-enable-animations", True)
        acc = await app.core.add_account("anim@localhost", "x", "never", connect=False)
        first = await app.core.open_chat(acc, "sam@localhost")
        second = await app.core.open_chat(acc, "lee@localhost")
        w = app.window
        view = w.conversation
        settings = app.core.settings

        async def add_message(chat, i: int, direction: int, body: str) -> int:
            sender = acc.jid if direction else "sam@localhost"
            values = {"chat_id": chat.id, "origin_id": "anim%d" % i, "msg_id": "anim%d" % i,
                      "direction": direction, "timestamp": int(time.time() * 1000), "sender": sender,
                      "sender_jid": sender, "body": body, "is_markdown": 0, "kind": "text", "status": 0,
                      "extra": {}}
            mid = await app.core.db.run(lambda conn: insert_message_job(conn, values))
            msg = await app.core.chats.reload_message(chat, mid)
            app.core.chats.show_message(chat, msg)
            return mid

        settings.set("appearance.chat_animations", False)
        w.show_chat(first)
        assert view.reveal.get_transition_type() == Gtk.RevealerTransitionType.NONE
        await wait_for(view.reveal.get_child_revealed, 2, "the first chat shown")
        await wait_for(lambda: view.ml is not None, 5, "the chat loaded")

        # a new message does not slide when its animation is off, but is visible right away
        settings.set("appearance.message_animations", False)
        first_id = await add_message(first, 1, 1, "sent without animation")
        row = await wait_for(lambda: view.rows.get(first_id), 5, "the first message row")
        assert row.reveal.get_transition_type() == Gtk.RevealerTransitionType.NONE
        assert row.reveal.get_reveal_child()

        # both directions animate when it is on
        settings.set("appearance.message_animations", True)
        sent_id = await add_message(first, 2, 1, "sent with animation")
        sent = await wait_for(lambda: view.rows.get(sent_id), 5, "the sent message row")
        assert sent.reveal.get_transition_type() == Gtk.RevealerTransitionType.SLIDE_UP
        await wait_for(sent.reveal.get_child_revealed, 2, "the sent message to slide in")
        received_id = await add_message(first, 3, 0, "received with animation")
        received = await wait_for(lambda: view.rows.get(received_id), 5, "the received message row")
        assert received.reveal.get_transition_type() == Gtk.RevealerTransitionType.SLIDE_UP
        await wait_for(received.reveal.get_child_revealed, 2, "the received message to slide in")

        # and the chat transition comes back when the chat setting is on
        settings.set("appearance.chat_animations", True)
        w.show_chat(second)
        assert view.reveal.get_transition_type() == Gtk.RevealerTransitionType.SLIDE_UP
        await wait_for(view.reveal.get_child_revealed, 2, "the second chat slid in")
        await app.core.shutdown()

    run(scenario(), timeout=60)


def test_composer_padding_does_not_move_the_window(app, run):  # noqa: F811
    """The composer used to be in an Adw.ToolbarView bottom bar, which puts a Gtk.WindowHandle around it: a drag
    on the padding below the text moved the window instead of selecting text."""

    async def scenario():
        await wait_for(lambda: app.core.started, 20, "core start")
        parent = app.window.conversation.composer.get_parent()
        while parent is not None:
            assert type(parent).__gtype__.name != "GtkWindowHandle", \
                "the composer is inside a window-drag area"
            parent = parent.get_parent()
        await app.core.shutdown()

    run(scenario(), timeout=30)
