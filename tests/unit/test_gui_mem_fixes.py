"""Unit tests for GUI memory leaks, texture caching/threading, and responsiveness fixes."""

from __future__ import annotations

import asyncio
import gc
import os
import tempfile
import threading
import time
import unittest
import weakref
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk

from ward import aio, memreport
from ward.core.models import AccountItem, ChatItem, OccupantItem
from ward.gui import (
    app as app_mod,
)
from ward.gui import (
    call as call_mod,
)
from ward.gui import (
    compat,
    dialogs,
    emoji_picker,
    lifetime,
    members,
    prefs,
    tools,
    tray,
    video,
    welcome,
    widgets,
)
from ward.settings import Settings

Adw.init()


def _pump_main_loop(predicate=None, timeout: float = 2.0) -> None:
    ctx = GLib.MainContext.default()
    if predicate is None:
        while ctx.iteration(False):
            pass
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        while ctx.iteration(False):
            pass
        if predicate():
            return
        time.sleep(0.005)


def _write_png(path: str, width: int = 32, height: int = 32, color=(20, 120, 220, 255)) -> None:
    from PIL import Image

    img = Image.new("RGBA", (width, height), color)
    img.save(path, "PNG")


class _LeakMessageRow(Gtk.Box):
    __gtype_name__ = "WardTestGuiMemMessageRow"

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.reveal = Gtk.Revealer()
        self.reveal.connect("notify::child-revealed", self._on_revealed)
        self.append(self.reveal)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.reveal.set_child(self.content)

    def _on_revealed(self, *_a) -> None:
        pass

    def poke(self) -> None:
        pass


class _FakeListItem:
    def __init__(self):
        self._child = None

    def set_child(self, child) -> None:
        self._child = child

    def get_child(self):
        return self._child


def _build_nested_row():
    row = _LeakMessageRow()
    frame = Gtk.Frame()
    sc = Gtk.ScrolledWindow()
    clamp = Adw.Clamp()
    center = Gtk.CenterBox()
    stack = Gtk.Stack()
    exp = Gtk.Expander(label="Spoiler")
    overlay = Gtk.Overlay()
    grid = Gtk.Grid()
    inner_box = Gtk.Box()

    lbl = Gtk.Label(label="secret")
    click = Gtk.GestureClick()
    click.connect("released", lambda *_a: row.poke())
    lbl.add_controller(click)

    btn = Gtk.Button()
    btn_box = Gtk.Box()
    btn.set_child(btn_box)
    btn.connect("clicked", lambda *_a: row.poke())

    pic = Gtk.Picture.new_for_paintable(
        Gdk.MemoryTexture.new(4, 4, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(b"\xff" * 64), 16)
    )
    menu_btn = Gtk.MenuButton()
    widgets.emoji_button(menu_btn, None, lambda _e: row.poke(), lambda: row.poke())
    widgets._create_emoji_chooser(menu_btn)
    picker = menu_btn.get_popover()

    grid.attach(lbl, 0, 0, 1, 1)
    inner_box.append(grid)
    inner_box.append(btn)
    inner_box.append(pic)
    inner_box.append(menu_btn)
    exp.set_child(inner_box)
    overlay.set_child(exp)
    stack.add_named(overlay, "main")
    center.set_center_widget(stack)
    clamp.set_child(center)
    sc.set_child(clamp)
    frame.set_child(sc)
    row.content.append(frame)

    objs = [row, row.reveal, row.content, frame, sc, clamp, center, stack, overlay, exp, grid, lbl, btn, pic, menu_btn]
    if picker is not None:
        objs.append(picker)
    return row, [weakref.ref(o) for o in objs]


class LifetimeReleaseTests(unittest.TestCase):
    def test_release_recurses_into_revealer_and_all_containers(self):
        item = _FakeListItem()

        def setup() -> list[weakref.ref]:
            row, refs = _build_nested_row()
            lifetime.setup_row(item, row)
            return refs

        refs = setup()
        lifetime.teardown_row(None, item)
        _pump_main_loop()
        gc.collect()
        alive = [r() for r in refs if r() is not None]
        self.assertEqual(alive, [])

    def test_widgets_clear_releases_nested_cycles(self):
        parent = Gtk.Box()

        def populate() -> list[weakref.ref]:
            row, refs = _build_nested_row()
            parent.append(row)
            return refs

        refs = populate()
        widgets.clear(parent)
        _pump_main_loop()
        gc.collect()
        alive = [r() for r in refs if r() is not None]
        self.assertEqual(alive, [])

    def test_disconnect_all_leaves_adw_action_row_intact_and_breaks_suffix_handlers(self):
        group = Adw.PreferencesGroup()
        row = Adw.ActionRow(title="Item", activatable=True)
        owner = SimpleNamespace(called=0)
        suffix = Gtk.Button(label="Act")
        suffix.connect("clicked", lambda *_: setattr(owner, "called", owner.called + 1))
        row.connect("activated", lambda *_: setattr(owner, "called", owner.called + 1))
        row.add_suffix(suffix)
        group.add(row)

        lifetime.release(group)
        # Adw.ActionRow internal layout box must not be torn apart
        self.assertIsNotNone(row.get_child())
        suffix.emit("clicked")
        row.emit("activated")
        self.assertEqual(owner.called, 0)

    def test_watch_uses_weakref_and_unwatch_all_disconnects(self):
        class Target(GObject.Object):
            __gsignals__ = {"ping": (GObject.SignalFlags.RUN_LAST, None, ())}

        def make_and_watch():
            t = Target()
            lifetime.watch(t, "ping", lambda *_: None)
            return weakref.ref(t)

        w_t = make_and_watch()
        gc.collect()
        self.assertIsNone(w_t())

        seen = []
        t2 = Target()
        lifetime.watch(t2, "ping", lambda *_: seen.append(1))
        t2.emit("ping")
        self.assertEqual(seen, [1])
        lifetime.unwatch_all()
        t2.emit("ping")
        self.assertEqual(seen, [1])


class TextureCacheTests(unittest.TestCase):
    def setUp(self):
        widgets.clear_texture_cache()

    def tearDown(self):
        widgets.clear_texture_cache()

    def test_texture_byte_budget_evicts_before_200_entries(self):
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for i in range(6):
                p = os.path.join(d, f"img_{i}.png")
                _write_png(p, 512, 512, (i * 40, 80, 160, 255))
                paths.append(p)
            with mock.patch.object(widgets, "MAX_TEXTURE_BYTES", 3 * 1024 * 1024):
                for p in paths:
                    tex = widgets.texture(p, max_side=640)
                    self.assertIsNotNone(tex)
                self.assertLessEqual(widgets._texture_bytes, 3 * 1024 * 1024)
                self.assertLessEqual(len(widgets._textures), 3)
                self.assertIsNone(widgets.cached_texture(paths[0], 640))
                self.assertIsNotNone(widgets.cached_texture(paths[-1], 640))

    def test_negative_cache_skips_redundant_stat_and_corrupt_decode(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "nope.png")
            with mock.patch.object(widgets, "_stat_regular", wraps=widgets._stat_regular) as stat_spy:
                self.assertIsNone(widgets.texture(missing))
                self.assertIsNone(widgets.texture(missing))
                self.assertIsNone(widgets.texture(missing))
                self.assertEqual(stat_spy.call_count, 1)

            corrupt = os.path.join(d, "bad.png")
            with open(corrupt, "wb") as fh:
                fh.write(b"not an image")
            with mock.patch.object(widgets, "_decode_pil_rgba", wraps=widgets._decode_pil_rgba) as dec_spy:
                self.assertIsNone(widgets.texture(corrupt))
                self.assertIsNone(widgets.texture(corrupt))
                self.assertEqual(dec_spy.call_count, 1)

            _write_png(corrupt, 24, 24)
            widgets.invalidate_texture(corrupt)
            self.assertIsNotNone(widgets.texture(corrupt))

    def test_texture_async_decodes_off_main_thread_and_deduplicates(self):
        main_tid = threading.get_ident()
        worker_tids: list[int] = []
        results: list[tuple[int, Gdk.Texture | None]] = []

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "avatar.png")
            _write_png(p, 96, 96)

            orig_worker = widgets._decode_worker

            def spy_worker(path, max_side):
                worker_tids.append(threading.get_ident())
                return orig_worker(path, max_side)

            with mock.patch.object(widgets, "_decode_worker", side_effect=spy_worker):
                for _ in range(4):
                    widgets.texture_async(
                        p,
                        widgets.AVATAR_PIXELS,
                        lambda tex: results.append((threading.get_ident(), tex)),
                    )
                _pump_main_loop(lambda: len(results) == 4)

        self.assertEqual(len(worker_tids), 1)
        self.assertNotEqual(worker_tids[0], main_tid)
        self.assertEqual(len(results), 4)
        for tid, tex in results:
            self.assertEqual(tid, main_tid)
            self.assertIsNotNone(tex)
            self.assertIs(tex, results[0][1])

    def test_avatar_loads_async_and_ignores_stale_path(self):
        with tempfile.TemporaryDirectory() as d:
            p1 = os.path.join(d, "a1.png")
            p2 = os.path.join(d, "a2.png")
            _write_png(p1, 48, 48, (255, 0, 0, 255))
            _write_png(p2, 64, 64, (0, 255, 0, 255))

            av = widgets.Avatar(40, "Alice", p1)
            av.update("Alice", p2)
            _pump_main_loop(lambda: av.avatar.get_custom_image() is not None)
            img = av.avatar.get_custom_image()
            self.assertIsNotNone(img)
            self.assertEqual((img.get_width(), img.get_height()), (64, 64))

    def test_memreport_reports_cached_textures(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.png")
            _write_png(p, 64, 64)
            widgets.texture(p, 192)
            text = memreport.build()
            self.assertIn("decoded images cached: 1/", text)


class EmojiPickerAndDialogsTests(unittest.TestCase):
    def test_emoji_picker_freed_on_emoji_button_close(self):
        if not emoji_picker.available():
            self.skipTest("GTK emoji resource not available")
        btn = Gtk.MenuButton()
        owner = SimpleNamespace(picked=[], closed=0)
        widgets.emoji_button(btn, None, owner.picked.append, lambda: setattr(owner, "closed", owner.closed + 1))
        widgets._create_emoji_chooser(btn)

        def close_and_drop() -> tuple[weakref.ref, weakref.ref]:
            chooser = btn.get_popover()
            self.assertIsInstance(chooser, emoji_picker.EmojiPicker)
            chooser._show_variants(999999)  # bounds-safe no-op
            refs = (weakref.ref(chooser), weakref.ref(chooser.grid))
            chooser.emit("closed")
            return refs

        w_chooser, w_grid = close_and_drop()
        _pump_main_loop(lambda: btn.get_popover() is None)
        gc.collect()
        self.assertEqual(owner.closed, 1)
        self.assertIsNone(w_chooser())
        self.assertIsNone(w_grid())

    def test_compat_dialog_wrapper_collected_after_close_and_base_dialog_skips_callbacks(self):
        def make_compat_dialog():
            d = compat.Dialog("Test", 300, 200)
            closed = []
            d.on_closed(lambda: closed.append(True))
            d._on_closed()
            self.assertEqual(closed, [True])
            return weakref.ref(d)

        w_d = make_compat_dialog()
        gc.collect()
        self.assertIsNone(w_d())

        fake_win = SimpleNamespace(core=SimpleNamespace())
        bd = dialogs.BaseDialog(fake_win, "Test", "Save")
        bd.dialog._on_closed()
        self.assertTrue(bd._closed)
        called = []

        async def job():
            await asyncio.sleep(0)
            return "ok"

        loop = aio.get_loop()

        async def driver():
            task = bd.run(job(), done=lambda res: called.append(res))
            await task

        loop.run_until_complete(driver())
        self.assertEqual(called, [])

    def test_account_page_disconnects_account_signal_when_closed(self):
        acc = AccountItem(id=1, jid="a@example.org")
        fake_core = SimpleNamespace(settings=Settings(), pgp=None)
        fake_prefs = SimpleNamespace(core=fake_core, window=SimpleNamespace(), d=compat.preferences_dialog())
        try:
            ap = prefs.AccountPage(fake_prefs, acc)
            w_ap = weakref.ref(ap)
            fake_prefs.d.emit("close-request" if isinstance(fake_prefs.d, Gtk.Window) else "closed")
            del ap
            gc.collect()
            self.assertIsNone(w_ap())
        finally:
            if isinstance(fake_prefs.d, Gtk.Window):
                fake_prefs.d.destroy()


class PanelAndWindowCleanupTests(unittest.TestCase):
    def test_members_panel_cancels_timer_and_clears_popover_on_chat_switch(self):
        class FakeMuc(GObject.Object):
            __gsignals__ = {"occupants-changed": (GObject.SignalFlags.RUN_LAST, None, (object,))}

            def sorted_occupants(self, _c):
                return [
                    OccupantItem(nick="alice", role="moderator", real_jid="alice@example.org"),
                    OccupantItem(nick="bob", role="participant", real_jid="bob@example.org"),
                ]

        fake_muc = FakeMuc()
        core = SimpleNamespace(muc=fake_muc, settings=Settings(), accounts=SimpleNamespace(get=lambda _i: None))
        view = SimpleNamespace(core=core, window=None, composer=None)
        panel = members.MembersPanel(view)
        chat = ChatItem(id=1, account_id=1, jid="room@conf.example.org", type="groupchat")

        panel.set_chat(chat)
        self.assertTrue(panel.count.get_text().startswith("2"))
        panel._refresh_pending = GLib.timeout_add(10_000, lambda: False)
        panel._occupant = OccupantItem(nick="alice")
        panel.set_chat(None)
        self.assertEqual(panel._refresh_pending, 0)
        self.assertIsNone(panel._occupant)
        self.assertEqual(panel.count.get_text(), "")
        lifetime.unwatch_all()
        lifetime.dismantle(panel)

    def test_xml_console_rebuild_limits_lines_and_close_detaches(self):
        class FakeSession(GObject.Object):
            __gsignals__ = {"xml": (GObject.SignalFlags.RUN_LAST, None, (bool, str))}

            def __init__(self):
                super().__init__()
                self.console = False
                self.xml_history = [(1000.0 + i, True, f"<m id='{i}'/>") for i in range(1500)]

            def clear_xml_history(self):
                self.xml_history.clear()

        sess = FakeSession()
        acc = AccountItem(id=1, jid="a@example.org")
        core = SimpleNamespace(
            accounts=SimpleNamespace(list=lambda: [acc]),
            session_for=lambda _a: sess,
        )
        win = Gtk.Window()
        win.core = core
        console = tools.XmlConsole(win)
        try:
            self.assertTrue(sess.console)
            self.assertLessEqual(console.buffer.get_line_count(), tools.MAX_LINES + 5)
            console._on_close()
            self.assertFalse(sess.console)
        finally:
            console.destroy()
            win.destroy()

    def test_call_window_cleanup_prevents_post_close_ended_crash(self):
        class FakeCall(GObject.Object):
            state = GObject.Property(type=str, default="active")
            __gsignals__ = {"media": (GObject.SignalFlags.RUN_LAST, None, ())}

            def __init__(self):
                super().__init__()
                self.account_id = 1
                self.sid = "s1"
                self.peer = "peer@example.org"
                self.video = False
                self.outgoing = False
                self.media = {"audio"}
                self.duration = 5
                self.end = "ended"
                self.active = False
                self.ms = None
                self.camera = False
                self.screen = False

        calls_obj = GObject.Object()
        calls_obj.calls = {}
        calls_obj.summary = lambda _d: "Ended"
        core = SimpleNamespace(
            calls=calls_obj,
            names=SimpleNamespace(contact_name=lambda _a, j: j),
            roster=SimpleNamespace(contacts=SimpleNamespace(get=lambda _a, _p: None)),
            settings=Settings(),
        )
        fake_app = SimpleNamespace(core=core, window=None, withdraw_notification=lambda _k: None)

        ui = call_mod.CallUI.__new__(call_mod.CallUI)
        ui.app = fake_app
        ui.core = core
        ui.windows = {}
        ui._call_handlers = {}
        ui._ring_source = 0

        c = FakeCall()
        cwin = call_mod.CallWindow(ui, c)
        ui.windows[(1, "s1")] = cwin
        try:
            cwin._timer = GLib.timeout_add_seconds(60, lambda: False)
            cwin._on_close()
            self.assertFalse(hasattr(cwin, "call"))
            self.assertNotIn((1, "s1"), ui.windows)
            cwin.ended()
        finally:
            cwin.destroy()

    def test_welcome_dialog_cleanup_and_provider_markup_escaping(self):
        d = compat.Dialog("Add", 400, 500)
        acc = AccountItem(id=1, jid="a@example.org")
        fake_win = SimpleNamespace(core=SimpleNamespace(accounts=SimpleNamespace(list=lambda: [])))
        wp = welcome.WelcomePage(fake_win, dialog=d)
        wp._acc = acc
        hid = acc.connect("notify::state", lambda *_: None)
        wp._acc_handlers = [hid]
        d._on_closed()
        self.assertTrue(wp._closed)
        self.assertEqual(wp._acc_handlers, [])
        self.assertFalse(GObject.signal_handler_is_connected(acc, hid))

        wp._closed = False
        wp.providers = Adw.PreferencesGroup()
        wp._provider_rows = []
        wp._fill_providers([SimpleNamespace(domain="a&b.example", upload_mb=5, archive_days=0, locations=["US & EU"])])
        self.assertEqual(len(wp._provider_rows), 1)
        self.assertIn("&amp;", wp._provider_rows[0].get_title())
        lifetime.unwatch_all()
        lifetime.dismantle(wp)

    def test_video_unmap_pauses_playing_stream_unless_fullscreen(self):
        stream = mock.Mock()
        stream.get_playing.return_value = True
        vid = mock.Mock()
        vid.get_media_stream.return_value = stream
        vid._ward_fullscreen = False
        video._pause_on_unmap(vid)
        stream.pause.assert_called_once()

        stream.reset_mock()
        vid._ward_fullscreen = True
        video._pause_on_unmap(vid)
        stream.pause.assert_not_called()

    def test_tray_close_is_idempotent_and_clears_caches(self):
        t = tray.Tray.__new__(tray.Tray)
        t.bus = mock.Mock()
        t.app = mock.Mock()
        t._ids = [1, 2]
        t._watch = 0
        t._owner = 0
        t._registered = True
        t._pixmaps = {"icon": [(22, 22, b"")]}
        t._pixmap_variants = {"icon": GLib.Variant("a(iiay)", [])}
        t.close()
        t.close()
        self.assertEqual(t._ids, [])
        self.assertIsNone(t.app)
        self.assertEqual(t._pixmaps, {})
        self.assertEqual(t._pixmap_variants, {})
        self.assertFalse(t._toggle())
        self.assertFalse(t._clicked(tray.SHOW))

    def test_app_theme_sound_cache_is_bounded_and_scoped(self):
        with tempfile.TemporaryDirectory() as d:
            home = os.path.join(d, "home")
            sysdir = os.path.join(d, "sys")
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": home, "XDG_DATA_DIRS": sysdir}):
                fake = SimpleNamespace(_theme_sound_cache={})
                self.assertFalse(app_mod.WardApplication._has_theme_sound(fake, "message-new-instant"))
                stereo = os.path.join(sysdir, "sounds", "freedesktop", "stereo")
                os.makedirs(stereo)
                with open(os.path.join(stereo, "message-new-instant.oga"), "wb") as fh:
                    fh.write(b"OggS")
                self.assertTrue(app_mod.sound_theme_has("message-new-instant"))


if __name__ == "__main__":
    unittest.main()
