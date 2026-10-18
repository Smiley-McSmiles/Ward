"""Opening a chat slides it up into the message area, and the preference can turn that off."""

import pytest
from helpers import wait_for
from test_gui import app  # noqa: F401 - the fixture

pytestmark = [pytest.mark.integration, pytest.mark.gui]


def test_chat_animations_can_be_turned_off(app, run):  # noqa: F811
    from gi.repository import Gtk

    async def scenario():
        await wait_for(lambda: app.core.started, 20, "core start")
        Gtk.Settings.get_default().set_property("gtk-enable-animations", True)
        acc = await app.core.add_account("anim@localhost", "x", "never", connect=False)
        first = await app.core.open_chat(acc, "sam@localhost")
        second = await app.core.open_chat(acc, "lee@localhost")
        w = app.window
        view = w.conversation
        settings = app.core.settings

        settings.set("appearance.chat_animations", False)
        w.show_chat(first)
        assert view.reveal.get_transition_type() == Gtk.RevealerTransitionType.NONE
        await wait_for(view.reveal.get_child_revealed, 2, "the first chat shown")

        settings.set("appearance.chat_animations", True)
        w.show_chat(second)
        assert view.reveal.get_transition_type() == Gtk.RevealerTransitionType.SLIDE_UP
        await wait_for(view.reveal.get_child_revealed, 2, "the second chat slid in")
        await app.core.shutdown()

    run(scenario(), timeout=60)
