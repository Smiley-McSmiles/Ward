"""The tray icon against a stand-in StatusNotifierWatcher, the way KDE or GNOME's AppIndicator extension sees it."""

import os
import time

import pytest

WATCHER_XML = """<node><interface name="org.kde.StatusNotifierWatcher">
<method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method></interface></node>"""


def _iterate(cond, timeout=5.0):
    from gi.repository import GLib

    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.01)
    return cond()


@pytest.fixture
def bus():
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        pytest.skip("no session bus (run under dbus-run-session)")
    from gi.repository import Gio

    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


class FakeApp:
    def __init__(self):
        from ward.settings import Settings

        class Core:
            settings = Settings()
        self.core = Core()
        self.window = None
        self.presence = []

    def common_show(self):
        return "away"

    def set_all_presence(self, show):
        self.presence.append(show)


def test_sni_properties_answer_one_at_a_time():
    """A tray host asks for every property in turn (GetAll).  Answering must not rasterize and pack the icons for
    the ones that have nothing to do with pixels, and the packed pixels must be reused."""
    from gi.repository import GLib

    from ward.gui import tray

    t = tray.Tray.__new__(tray.Tray)
    t.state, t.show, t.unread, t.icon_path = "offline", "available", 0, ""
    t._pixmaps, t._pixmap_variants = {}, {}

    def no_pixels(_name):
        raise AssertionError("the icons were built for a property that does not need them")

    t._pixmap = no_pixels
    for prop in ("Category", "Id", "Title", "Status", "IconName", "AttentionIconName", "OverlayIconName",
                 "WindowId", "IconThemePath", "ItemIsMenu", "Menu", "ToolTip"):
        assert t._sni_prop(None, None, None, None, prop) is not None, prop
    assert t._sni_prop(None, None, None, None, "NoSuchProperty") is None
    del t._pixmap

    name = t._icon_name()
    t._pixmaps[name] = [(22, 22, bytes(range(256)) * 2), (32, 32, b"\x00\x01\x02\x03")]
    first = t._sni_prop(None, None, None, None, "IconPixmap")
    assert first is t._sni_prop(None, None, None, None, "IconPixmap"), "the packed pixels are cached"
    assert first.get_type_string() == "a(iiay)"
    assert first.equal(GLib.Variant("a(iiay)", t._pixmaps[name]))


def test_tray_registers_and_follows_state(bus):
    from gi.repository import Gio, GLib

    from ward.gui import tray

    registered = []

    def call(_c, sender, _p, _i, method, params, inv):
        registered.append((sender, params.unpack()[0]))
        inv.return_value(None)
    info = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0]
    oid = tray.register(bus, "/StatusNotifierWatcher", info, call, None)
    owner = Gio.bus_own_name_on_connection(bus, "org.kde.StatusNotifierWatcher", Gio.BusNameOwnerFlags.NONE,
                                           None, None)
    try:
        assert _iterate(lambda: tray.watcher_running(bus))
        app = FakeApp()
        t = tray.Tray(app)
        assert _iterate(lambda: registered), "the item registered with the watcher"
        name = registered[0][1]
        assert name.startswith("org.kde.StatusNotifierItem-")

        def call(path, iface, method, args):
            # the item lives in this process: call asynchronously and let the main loop answer
            out = []
            bus.call(name, path, iface, method, args, None, Gio.DBusCallFlags.NONE, 3000, None,
                     lambda b, r: out.append(b.call_finish(r)))
            assert _iterate(lambda: out)
            return out[0].unpack()

        def props():
            return call("/StatusNotifierItem", "org.freedesktop.DBus.Properties", "GetAll",
                        GLib.Variant("(s)", ("org.kde.StatusNotifierItem",)))[0]
        p = props()
        assert p["IconName"].endswith("-offline") and p["Status"] == "Active"
        assert p["IconPixmap"] and p["IconPixmap"][0][0] in (22, 32, 48, 64)
        t.set_state("connecting")
        assert props()["IconName"].endswith("-connecting")
        t.set_state("online")
        assert props()["IconName"] == "io.github.smiley_mcsmiles.Ward"
        t.set_state("online", "dnd")
        assert props()["IconName"].endswith("-dnd")
        t.set_state("online", "available")
        t.set_unread(3)
        p = props()
        assert p["Status"] == "NeedsAttention" and p["AttentionIconName"].endswith("-unread")
        layout = call("/MenuBar", "com.canonical.dbusmenu", "GetLayout", GLib.Variant("(iias)", (0, -1, [])))
        labels = [c[1].get("label") for c in layout[1][2]]
        assert "Quit" in labels and "Show Ward" in labels and "Mute sounds" in labels
        status = next(c for c in layout[1][2] if c[1].get("label") == "Status")
        radios = {c[1]["label"]: c[1]["toggle-state"] for c in status[2]}
        assert radios["Away"] == 1 and radios["Available"] == 0 and len(radios) == 5
        t._clicked(10)
        assert app.presence == ["available"]
        sounds = app.core.settings["notify.sounds"]
        t._clicked(tray.MUTE)
        assert app.core.settings["notify.sounds"] is not sounds
        t.close()
    finally:
        Gio.bus_unown_name(owner)
        bus.unregister_object(oid)


def test_status_icons_follow_presence():
    from ward.gui import tray

    t = tray.Tray.__new__(tray.Tray)
    t.state, t.unread = "online", 0
    expected = {"available": "", "away": "-away", "dnd": "-dnd", "xa": "-xa", "offline": "-offline"}
    for show, suffix in expected.items():
        t.show = show
        assert t._icon_name() == tray.APP_ID + suffix
    t.show, t.unread = "dnd", 2
    assert t._icon_name() == tray.APP_ID + "-dnd-unread"
