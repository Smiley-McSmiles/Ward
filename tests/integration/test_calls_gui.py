"""Calls in the window: the header buttons, an incoming call's window answered with video, and the line it leaves."""

import asyncio
import os

import pytest
from helpers import befriend, fresh, make_core, stop, wait_for
from test_gui import app  # noqa: F401 - the fixture

from ward.core.calls import media

pytestmark = [pytest.mark.integration, pytest.mark.gui,
              pytest.mark.skipif(not media.available(), reason="GStreamer WebRTC is missing")]


def _children(w):
    c = w.get_first_child()
    while c is not None:
        yield c
        c = c.get_next_sibling()


def test_incoming_video_call_in_the_window(app, prosody, run, tmp_path, monkeypatch):  # noqa: F811
    from ward.gui.call import CallWindow

    monkeypatch.setenv("WARD_CALL_SOURCES", "test")
    shots = os.environ.get("WARD_CALL_SHOTS", "")

    async def scenario():
        await wait_for(lambda: app.core.started, 20, "core start")
        user = fresh(prosody, "cg")
        opts = {"host": "127.0.0.1", "port": prosody.c2s, "tls": "starttls", "proxy": "none"}
        acc = await app.core.add_account("%s@localhost" % user, "password", "never", opts, connect=False)
        await app.core.connect_account(acc, "password")
        await wait_for(lambda: acc.state == "online" and acc.step == "", 30, "online")
        app.core.settings.set("encryption.default", "off")
        peer = await make_core(tmp_path, prosody, fresh(prosody, "cgp"))
        try:
            peer.settings.set("encryption.default", "off")
            await befriend(app.core, peer)
            pacc = peer.accounts.list()[0]
            chat = await app.core.open_chat(acc, pacc.jid)
            w = app.window
            w.set_default_size(900, 640)
            w.show_chat(chat)
            view = w.conversation
            await wait_for(lambda: view.call_button.get_visible() and view.call_button.get_sensitive()
                           and view.video_button.get_sensitive(), 20, "call buttons")

            call = await peer.calls.start(pacc, acc.jid, video=True)
            key = (acc.id, call.sid)
            await wait_for(lambda: key in app.calls_ui.windows, 15, "the call window")
            win: CallWindow = app.calls_ui.windows[key]
            incoming = win.call
            assert incoming.state == "incoming" and win.status.get_text() == "Incoming video call"
            tips = [b.get_tooltip_text() for b in _children(win.buttons)]
            assert tips == ["Decline", "Answer with video", "Answer"]
            if shots:
                await asyncio.sleep(0.5)
                _shot(win, os.path.join(shots, "incoming.png"))

            list(_children(win.buttons))[1].emit("clicked")          # answer with video
            await wait_for(lambda: incoming.state == "active" and call.state == "active", 30, "connected")
            await wait_for(lambda: win.stack.get_visible_child_name() == "video", 20, "their video shown")
            assert [b.get_tooltip_text() for b in _children(win.buttons)] == ["Mute", "Turn off camera", "Hang up"]
            await asyncio.sleep(1.5)
            if shots:
                _shot(win, os.path.join(shots, "active.png"))
            mute = next(_children(win.buttons))
            mute.set_active(True)
            assert incoming.muted and mute.get_icon_name() == "microphone-disabled-symbolic"

            list(_children(win.buttons))[-1].emit("clicked")           # hang up
            await wait_for(lambda: call.state == "ended" and incoming.state == "ended", 15, "ended")
            await wait_for(lambda: key not in app.calls_ui.windows, 5, "window let go")
            await wait_for(lambda: any(r.msg is not None and r.msg.kind == "call" for r in view.rows.values()), 10,
                           "the call line")
            line = next(r for r in view.rows.values() if r.msg is not None and r.msg.kind == "call")
            assert line.msg.extra["call"]["end"] == "ended"
        finally:
            await stop(peer)
        await app.core.shutdown()

    run(scenario(), timeout=180)


def _shot(widget, path: str) -> None:
    from gi.repository import Graphene, Gtk

    snap = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(widget).snapshot(snap, widget.get_width(), widget.get_height())
    node = snap.to_node()
    if node is not None:
        widget.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, widget.get_width(),
                                                                         widget.get_height())).save_to_png(path)
