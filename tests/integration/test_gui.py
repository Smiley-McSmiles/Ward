"""The real window against the test server: sign in through the welcome page, chat with another Ward, see the
reply appear, and take a screenshot.  Needs a display (CI runs it under Xvfb)."""

import itertools
import os

import pytest
from helpers import befriend, fresh, make_core, stop, wait_for

pytestmark = [pytest.mark.integration, pytest.mark.gui]
_ids = itertools.count(1)


def _gtk():
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("no display")
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("GTK cannot open the display")


@pytest.fixture
def app(trust_test_ca, monkeypatch, loop):
    _gtk()
    monkeypatch.setenv("GSK_RENDERER", "cairo")
    from gi.repository import Gio

    from ward.gui.app import WardApplication

    a = WardApplication()
    a.set_flags(a.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    # each test's application exports its own D-Bus object: give it its own id
    a.set_application_id("%s.Test%d" % (a.get_application_id(), next(_ids)))
    a.register(None)
    a.activate()
    yield a
    if a.window is not None:
        a.window.destroy()


def _screenshot(window, name: str) -> None:
    from gi.repository import Gtk

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "screenshots")
    os.makedirs(out, exist_ok=True)
    p = Gtk.WidgetPaintable.new(window)
    snap = Gtk.Snapshot()
    p.snapshot(snap, window.get_width(), window.get_height())
    node = snap.to_node()
    if node is not None:
        window.get_renderer().render_texture(node, None).save_to_png(os.path.join(out, name))


def test_sign_in_and_chat(app, prosody, run, tmp_path):
    user = fresh(prosody, "gui")
    peer_user = fresh(prosody, "peer")

    async def scenario():
        await wait_for(lambda: app.core.started, 20, "core start")
        w = app.window
        w.set_default_size(1100, 760)
        assert w.top.get_visible_child_name() == "welcome"
        page = w.welcome
        page.nav.push(page._signin_page())
        page.jid_row.set_text("%s@localhost" % user)
        page.pw_row.set_text("password")
        page.store_row.set_selected(2)            # never store it
        page.host_row.set_text("127.0.0.1")
        page.port_row.set_value(prosody.c2s)
        page.tls_row.set_selected(1)              # STARTTLS
        page.proxy_row.set_selected(1)            # no proxy
        page.signin_button.emit("clicked")
        await wait_for(lambda: w.top.get_visible_child_name() == "main", 30, "the main view after signing in")
        acc = app.core.accounts.list()[0]
        await wait_for(lambda: acc.state == "online" and acc.step == "", 30, "login steps")

        peer = await make_core(tmp_path, prosody, peer_user)
        try:
            await befriend(app.core, peer)
            chat = await app.core.open_chat(acc, "%s@localhost" % peer_user)
            w.show_chat(chat)
            composer = w.conversation.composer
            composer.buffer.set_text("Hello from the **window**")
            composer.send()
            peer_acc = peer.accounts.list()[0]
            await wait_for(lambda: peer.chats.find(peer_acc.id, acc.jid, "chat") is not None, 20, "peer's chat")
            pchat = peer.chats.find(peer_acc.id, acc.jid, "chat")
            pml = await peer.chats.open_messages(pchat)
            await wait_for(lambda: any(m.body == "Hello from the **window**" for m in pml.items()), 20,
                           "the message at the peer")
            got = next(m for m in pml.items() if m.body == "Hello from the **window**")
            assert got.encryption in ("omemo2", "omemo"), "sent encrypted by default"

            await peer.messaging.send_text(pchat, "Hi! Here is a list:\n- one\n- two")
            view = w.conversation
            await wait_for(lambda: view.ml is not None and any(m.body.startswith("Hi! Here is a list")
                                                               for m in view.ml.items()), 20, "the reply in the window")
            reply = next(m for m in view.ml.items() if m.body.startswith("Hi! Here is a list"))
            await wait_for(lambda: reply.id in view.rows, 10, "a row for the reply")
            assert reply.encryption and not reply.extra.get("unencrypted")
            assert chat.preview.startswith("Hi! Here is a list")
            # sent message got its receipt
            mine = next(m for m in view.ml.items() if m.outgoing)
            await wait_for(lambda: mine.status >= 2, 20, "delivery receipt")

            # react, reply and edit through the view, as the hover bar and the composer do
            view.react(reply, "👍")
            pm_reply = next(m for m in pml.items() if m.body.startswith("Hi! Here is a list"))
            await wait_for(lambda: any(e == "👍" for e, _who, _me in pm_reply.reactions), 20, "the reaction")
            view.start_reply(reply)
            composer.buffer.set_text("Two is my favourite")
            composer.send()
            await wait_for(lambda: any(m.body == "Two is my favourite" and m.reply_target == pm_reply.id
                                       for m in pml.items()), 20, "the reply")
            composer.start_edit(mine)
            composer.buffer.set_text("Hello from the **new window**")
            composer.send()
            await wait_for(lambda: got.body == "Hello from the **new window**" and got.edited, 20, "the edit")
            await _settle(0.5)
            _screenshot(w, "gui-chat.png")
        finally:
            await stop(peer)
        await app.core.shutdown()

    run(scenario(), timeout=120)


@pytest.fixture
def errors(monkeypatch):
    """Exceptions raised in GTK callbacks (printed, not raised, by PyGObject) and errors logged by Ward."""
    import logging
    import sys

    seen: list[str] = []
    monkeypatch.setattr(sys, "excepthook", lambda t, v, tb: seen.append("%s: %s" % (t.__name__, v)))

    class Collect(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                seen.append(record.getMessage())
    h = Collect()
    logging.getLogger().addHandler(h)
    yield seen
    logging.getLogger().removeHandler(h)


async def _settle(seconds: float = 0.3) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def test_register_group_and_dialogs(app, prosody, run, errors):
    """Create an account from the welcome page, create a group, then open every dialog once."""
    from gi.repository import Gtk

    from ward.gui import admin, details, dialogs, prefs, tools, trust

    user = "newbie%d" % (os.getpid() % 100000)

    async def scenario():
        await wait_for(lambda: app.core.started, 20, "core start")
        w = app.window
        page = w.welcome
        page.nav.push(page._servers_page())
        page.host_row.set_text("127.0.0.1")
        page.port_row.set_value(prosody.c2s)
        page.tls_row.set_selected(1)
        page.proxy_row.set_selected(1)
        page._get_form("localhost")
        await wait_for(lambda: page.nav.get_visible_page().get_tag() == "register", 20, "the registration form")
        content = page.nav.get_visible_page().get_child().get_content().get_child().get_child().get_child()
        entries = []

        def collect(widget):
            from gi.repository import Adw

            if isinstance(widget, (Adw.EntryRow, Adw.PasswordEntryRow)):
                entries.append(widget)
            c = widget.get_first_child()
            while c is not None:
                collect(c)
                c = c.get_next_sibling()
        collect(content)
        user_row, pw_row, pw2_row = entries[:3]
        user_row.set_text(user)
        pw_row.set_text("correct horse battery")
        pw2_row.set_text("correct horse battery")
        button = content.get_last_child()
        while not (isinstance(button, Gtk.Button) and button.get_label() == "Create account"):
            button = button.get_prev_sibling()
        button.emit("clicked")
        await wait_for(lambda: w.top.get_visible_child_name() == "main", 30, "the main view after registering")
        acc = app.core.accounts.list()[0]
        assert acc.jid == "%s@localhost" % user
        await wait_for(lambda: acc.state == "online" and acc.step == "", 30, "login steps")

        # a new private group through the dialog
        d = dialogs.NewGroupDialog(w)
        d.present(w)
        d.name.set_text("Planning")
        d.on_action()
        await wait_for(lambda: w.current_chat is not None and w.current_chat.type == "groupchat" and
                       w.current_chat.joined, 30, "the new group")
        group = w.current_chat
        assert group.name == "Planning"
        await wait_for(lambda: w.conversation.members.count.get_text().startswith("1"), 10, "the members panel")
        view = w.conversation
        view.composer.buffer.set_text("hello?")
        view.composer.send()
        # alone in an encrypted group: nobody could read it, and the message says so
        await wait_for(lambda: view.ml is not None and any(m.body == "hello?" and m.status == -1
                                                           for m in view.ml.items()), 20, "the refused message")
        failed = next(m for m in view.ml.items() if m.body == "hello?")
        assert "Invite people first" in failed.error
        view._set_encryption("off")
        await wait_for(lambda: group.encryption_state == "plain", 10, "encryption off")
        view.composer.buffer.set_text("first post")
        view.composer.send()
        await wait_for(lambda: any(m.body == "first post" and m.status >= 2 for m in view.ml.items()), 20,
                       "the reflected group message")
        await _settle(0.5)
        _screenshot(w, "gui-group.png")

        # every dialog opens and closes without errors
        opened = [dialogs.StartChatDialog(w), dialogs.AddContactDialog(w), dialogs.JoinGroupDialog(w),
                  dialogs.InviteDialog(w, group), dialogs.ProfileDialog(w, acc), dialogs.ChangePasswordDialog(w, acc),
                  dialogs.RequestsDialog(w), dialogs.ChatPicker(w, "Forward", lambda c: None),
                  dialogs.QuickSwitcher(w), details.DetailsDialog(w, group), trust.DevicesDialog(w, acc, acc.jid),
                  details.GroupDevicesDialog(w, group), admin.AdminDialog(w, acc), tools.DiscoBrowser(w, acc)]
        if group.our_affiliation == "owner":
            opened += [details.RoomConfigDialog(w, group), details.AffiliationsDialog(w, group)]
        msg = next(m for m in w.conversation.ml.items() if m.body == "first post")
        opened.append(dialogs.MessageDetails(w.conversation, msg))
        for dlg in opened:
            dlg.present(w)
            await _settle(0.4)
            dlg.dialog.close() if hasattr(dlg, "dialog") else dlg.close()
        p = prefs.Preferences(w)
        p.present(w)
        for key in ("appearance", "chats", "notifications", "accounts", "encryption", "storage", "advanced"):
            p.d.set_visible_page_name(key)
            await _settle(0.2)
        app.core.settings["pgp.enabled"] = True
        page = prefs.AccountPage(p, acc)
        from ward.gui import compat

        compat.prefs_push(p.d, page.page)
        await _settle(1.0)
        _screenshot(w, "gui-account.png")
        p.d.close()
        console = tools.XmlConsole(w)
        console.present()
        await _settle(0.3)
        console.close()
        w.conversation.search_button.set_active(True)
        w.conversation.search_entry.set_text("first")
        await wait_for(lambda: w.conversation.stack.get_visible_child_name() == "results", 10, "search results")
        w.conversation.search_button.set_active(False)
        # the keyboard shortcuts run without errors
        for action in ("reply-last", "edit-last", "focus-composer", "jump-latest", "mark-read", "toggle-mute",
                       "toggle-mute", "toggle-pin", "next-unread", "prev-chat", "next-chat", "search-chat"):
            w.activate_action("win." + action, None)
            await _settle(0.1)
        assert w._back() is True             # Escape closes the search opened by search-chat
        await app.core.shutdown()

    run(scenario(), timeout=180)
    assert not errors, errors
