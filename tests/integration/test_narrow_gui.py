"""Making the window narrow and wide again: the members panel comes back only where it belongs, and the button to
invite people to a group lives in the top bar."""

import pytest
from helpers import wait_for
from test_gui import app  # noqa: F401 - the fixture

pytestmark = [pytest.mark.integration, pytest.mark.gui]


def _children(w):
    c = w.get_first_child()
    while c is not None:
        yield c
        yield from _children(c)
        c = c.get_next_sibling()


def test_widening_does_not_open_an_empty_members_panel(app, run):  # noqa: F811
    async def scenario():
        await wait_for(lambda: app.core.started, 20, "core start")
        acc = await app.core.add_account("narrow@localhost", "x", "never", connect=False)
        person = await app.core.open_chat(acc, "sam@localhost")
        group = await app.core.chats.get_or_create(acc.id, "team@groups.localhost", "groupchat", name="Team")
        w = app.window
        view = w.conversation
        settings = app.core.settings
        settings.set("ui.members_panel", True)

        # a one-to-one chat: no members panel, before or after the window was narrow (Adw.OverlaySplitView shows
        # its sidebar again when it stops being collapsed, which put an empty panel beside the chat)
        w.show_chat(person)
        await wait_for(lambda: view.chat is person, 5, "the chat shown")
        assert not view.split.get_show_sidebar() and not view.invite_button.get_visible()
        w._set_narrow(True)
        w._set_narrow(False)
        assert not view.split.get_show_sidebar()
        assert not view.members_button.get_active()

        # a group: the panel comes back after narrow, as it was, and the user's choice is not overwritten
        w.show_chat(group)
        await wait_for(lambda: view.chat is group, 5, "the group shown")
        assert view.split.get_show_sidebar() and view.invite_button.get_visible()
        w._set_narrow(True)
        assert not view.split.get_show_sidebar() and not view.invite_button.get_visible()
        assert settings["ui.members_panel"]
        w._set_narrow(False)
        assert view.split.get_show_sidebar() and view.invite_button.get_visible()
        assert settings["ui.members_panel"]

        # closed by the user, it stays closed across a resize
        view.members_button.set_active(False)
        assert not settings["ui.members_panel"]
        w._set_narrow(True)
        w._set_narrow(False)
        assert not view.split.get_show_sidebar()

        # inviting is in the top bar, not in the panel
        assert view.invite_button.get_action_name() == "chat.invite"
        assert view.invite_button.get_parent() is not None
        assert not any(getattr(c, "get_tooltip_text", lambda: "")() in ("Invite…", "Invite to this group…")
                       for c in _children(view.members))
        await app.core.shutdown()

    run(scenario(), timeout=60)
