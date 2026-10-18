"""The real window against the test server: sign in through the welcome page, chat with another Ward, see the
reply appear, and take a screenshot.  Needs a display (CI runs it under Xvfb)."""

import os

import pytest

from helpers import befriend, fresh, make_core, stop, wait_for

pytestmark = [pytest.mark.integration, pytest.mark.gui]


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
            _screenshot(w, "gui-chat.png")
        finally:
            await stop(peer)
        await app.core.shutdown()

    run(scenario(), timeout=120)
