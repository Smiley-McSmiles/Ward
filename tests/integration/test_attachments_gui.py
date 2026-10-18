"""Saving attachments from the chat: the message menu offers it for every attachment (downloading first when
needed), and videos have save and fullscreen buttons that show while the pointer is on them."""

import asyncio
import os
import shutil
import subprocess

import pytest
from helpers import befriend, fresh, make_core, stop, wait_for
from test_gui import app  # noqa: F401 - the fixture

pytestmark = [pytest.mark.integration, pytest.mark.gui]


def _menu_entries(model) -> list[tuple[str, str]]:
    from gi.repository import GLib

    out = []
    for i in range(model.get_n_items()):
        section = model.get_item_link(i, "section")
        if section is not None:
            out += _menu_entries(section)
            continue
        label = model.get_item_attribute_value(i, "label", None)
        action = model.get_item_attribute_value(i, "action", None)
        target = model.get_item_attribute_value(i, "target", GLib.VariantType("i"))
        detail = "(%s)" % target.print_(False) if target is not None else ""     # (a Variant of 0 is falsy)
        out.append((label.get_string() if label else "", (action.get_string() if action else "") + detail))
    return out


def test_save_attachment_from_menu(app, prosody, run, tmp_path, monkeypatch):  # noqa: F811
    from gi.repository import Gio, Gtk

    from ward.gui import conversation

    src = tmp_path / "notes.txt"
    src.write_text("hello file " * 100)
    dest = tmp_path / "saved" / "copy.txt"
    dest.parent.mkdir()

    class FakeDialog:       # the file chooser, answering with dest
        def __init__(self, **_kw):
            pass

        def save(self, _parent, _cancel, done):
            done(self, None)

        def save_finish(self, _res):
            return Gio.File.new_for_path(str(dest))
    monkeypatch.setattr(conversation.Gtk, "FileDialog", FakeDialog)

    async def scenario():
        await wait_for(lambda: app.core.started, 20, "core start")
        user = fresh(prosody, "att")
        opts = {"host": "127.0.0.1", "port": prosody.c2s, "tls": "starttls", "proxy": "none"}
        acc = await app.core.add_account("%s@localhost" % user, "password", "never", opts, connect=False)
        await app.core.connect_account(acc, "password")
        await wait_for(lambda: acc.state == "online" and acc.step == "", 30, "online")
        app.core.settings.set("encryption.default", "off")
        peer = await make_core(tmp_path, prosody, fresh(prosody, "attp"))
        try:
            peer.settings.set("encryption.default", "off")
            await befriend(app.core, peer)
            pacc = peer.accounts.list()[0]
            pchat = await peer.open_chat(pacc, acc.jid)
            sent = await peer.files.send_file(pchat, str(src))
            await wait_for(lambda: sent.attachments[0].state == "done", 20, "upload")
            chat = await wait_for(lambda: app.core.chats.find(acc.id, pacc.jid, "chat"), 20, "chat")
            w = app.window
            w.show_chat(chat)
            view = w.conversation
            await wait_for(lambda: any(r.msg is not None and r.msg.attachments for r in view.rows.values()), 20,
                           "attachment row")
            row = next(r for r in view.rows.values() if r.msg is not None and r.msg.attachments)
            m = row.msg
            assert not (m.attachments[0].local_path and os.path.exists(m.attachments[0].local_path))

            view.message_menu(row, m, 10, 10)
            pop = next(c for c in _children(row) if isinstance(c, Gtk.PopoverMenu))
            entries = _menu_entries(pop.get_menu_model())
            assert ("Save as…", "msg.save-attachment(0)") in entries
            assert ("Open", "msg.open-attachment(0)") in entries
            pop.popdown()

            view.activate_action("msg.save-attachment", conversation.GLib.Variant("i", 0))
            await wait_for(lambda: dest.exists(), 20, "saved copy")
            assert dest.read_text() == src.read_text()
        finally:
            await stop(peer)
        await app.core.shutdown()

    run(scenario(), timeout=120)


def _children(w):
    c = w.get_first_child()
    while c is not None:
        yield c
        c = c.get_next_sibling()


def _can_play_video() -> bool:
    """GTK aborts on a video it cannot play: only with GStreamer's playbin (and ffmpeg to make a clip)."""
    if shutil.which("ffmpeg") is None:
        return False
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError):
        return False
    Gst.init(None)
    return Gst.ElementFactory.find("playbin") is not None


@pytest.mark.skipif(not _can_play_video(), reason="needs GStreamer's playbin and ffmpeg")
def test_video_buttons(app, run, tmp_path):  # noqa: F811
    from gi.repository import Gtk

    from ward.gui import video

    path = tmp_path / "clip.webm"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
                    "-t", "2", "-pix_fmt", "yuv420p", str(path)], check=True)
    saved = []

    async def scenario():
        w = Gtk.Window()
        att = video.attachment(str(path), True, lambda: saved.append(True))
        w.set_child(att)
        w.present()
        await asyncio.sleep(0.5)
        bar = att.buttons
        assert not bar.get_reveal_child()               # hidden until the pointer is on the video
        hover = next(c for c in att.observe_controllers() if isinstance(c, Gtk.EventControllerMotion))
        hover.emit("enter", 10, 10)
        assert bar.get_reveal_child()
        save, full = list(_children(bar.get_child()))
        save.emit("clicked")
        assert saved == [True]
        hover.emit("leave")
        assert not bar.get_reveal_child()
        full.emit("clicked")
        await asyncio.sleep(0.5)
        big = [x for x in Gtk.Window.list_toplevels() if x is not w and x.get_visible() and x.is_fullscreen()]
        assert big, "no fullscreen window"
        stream = att.get_child().get_media_stream()
        big[0].close()
        await asyncio.sleep(0.2)
        assert att.get_child().get_media_stream() is stream
        stream.pause()
        w.destroy()

    run(scenario(), timeout=60)
