"""A tray icon through StatusNotifierItem (KDE, XFCE, Cinnamon, MATE, LXQt, Budgie, the GNOME AppIndicator
extension), with a small com.canonical.dbusmenu menu.  Plain D-Bus through Gio: no extra library, and nothing at
all happens when no tray is running."""

from __future__ import annotations

import logging
import os

from gi.repository import Gio, GLib

from .. import APP_ID, APP_NAME
from ..i18n import N_, _

log = logging.getLogger("ward.tray")

SNI_XML = """<node><interface name="org.kde.StatusNotifierItem">
<property name="Category" type="s" access="read"/><property name="Id" type="s" access="read"/>
<property name="Title" type="s" access="read"/><property name="Status" type="s" access="read"/>
<property name="IconName" type="s" access="read"/><property name="AttentionIconName" type="s" access="read"/>
<property name="IconThemePath" type="s" access="read"/><property name="ItemIsMenu" type="b" access="read"/>
<property name="Menu" type="o" access="read"/><property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
<property name="IconPixmap" type="a(iiay)" access="read"/>
<property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
<property name="OverlayIconName" type="s" access="read"/><property name="WindowId" type="i" access="read"/>
<method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
<signal name="NewIcon"/><signal name="NewStatus"><arg type="s"/></signal><signal name="NewToolTip"/>
<signal name="NewTitle"/><signal name="NewAttentionIcon"/></interface></node>"""

MENU_XML = """<node><interface name="com.canonical.dbusmenu">
<property name="Version" type="u" access="read"/><property name="TextDirection" type="s" access="read"/>
<property name="Status" type="s" access="read"/><property name="IconThemePath" type="as" access="read"/>
<method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/>
<arg type="as" direction="in"/><arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
<method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/>
<arg type="a(ia{sv})" direction="out"/></method>
<method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/>
<arg type="v" direction="out"/></method>
<method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/>
<arg type="u" direction="in"/></method>
<method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
<method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
<method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/>
<arg type="ai" direction="out"/></method>
<signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
<signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
</interface></node>"""

# dbusmenu ids: fixed ones, the status entries from 10 up
SHOW, QUIT, SEPARATOR, PREFERENCES, STATUS, MUTE, SEPARATOR2 = 1, 3, 2, 4, 5, 6, 7
STATUSES = (("available", N_("Available")), ("away", N_("Away")), ("dnd", N_("Busy")),
            ("xa", N_("Away for a while")), ("offline", N_("Offline")))


def register(bus, path: str, info, method_call, get_property) -> int:
    """Export an object with Python callbacks.  The closure-taking variant is called register_object in most
    PyGObject versions (rename-to), register_object_with_closures in some."""
    fn = getattr(bus, "register_object_with_closures", None) or bus.register_object
    return fn(path, info, method_call, get_property, None)


def watcher_running(bus) -> bool:
    """A tray host is listening: KDE Plasma, Cinnamon (xapp-sn-watcher), XFCE, LXQt, MATE, Budgie, or GNOME with the
    AppIndicator extension."""
    try:
        res = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                            GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)), GLib.VariantType("(b)"),
                            Gio.DBusCallFlags.NONE, 2000, None)
        return bool(res.unpack()[0])
    except GLib.Error:
        return False


def wanted(setting: str, bus=None) -> bool:
    if setting == "never":
        return False
    if setting == "always":
        return True
    # automatic: show it wherever a tray host runs (plain GNOME has none, GNOME + AppIndicator has one)
    return bus is not None and watcher_running(bus)


class Tray:
    def __init__(self, app):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        if not wanted(app.core.settings["general.tray"], self.bus):
            raise RuntimeError("no tray host on this desktop")
        self.app = app
        self.unread = 0
        self.state = "offline"         # online | connecting | offline
        self._pixmaps: dict[str, list] = {}
        self._pixmap_variants: dict[str, GLib.Variant] = {}
        self._registered = False
        from .. import paths

        icons = os.path.join(paths.share_dir(), "icons") if paths.share_dir() else ""
        self.icon_path = icons if icons and os.path.isdir(icons) else ""
        self.name = "org.kde.StatusNotifierItem-%d-1" % os.getpid()
        self._ids = []
        sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self._ids.append(register(self.bus, "/StatusNotifierItem", sni, self._sni_call, self._sni_prop))
        self._ids.append(register(self.bus, "/MenuBar", menu, self._menu_call, self._menu_prop))
        self.revision = 1
        self._watch = 0
        # register with the watcher only once we own our name: the GNOME AppIndicator extension looks the item up
        # right away and forgets it if the name has no owner yet
        self._owner = Gio.bus_own_name_on_connection(self.bus, self.name, Gio.BusNameOwnerFlags.NONE,
                                                     self._name_acquired, None)

    def _name_acquired(self, *_a) -> None:
        if not self._watch:
            self._watch = Gio.bus_watch_name_on_connection(self.bus, "org.kde.StatusNotifierWatcher",
                                                           Gio.BusNameWatcherFlags.NONE, self._watcher_up,
                                                           self._watcher_gone)

    def _watcher_gone(self, *_a) -> None:
        self._registered = False

    def _watcher_up(self, *_a) -> None:
        def done(bus, res):
            try:
                bus.call_finish(res)
                self._registered = True
            except GLib.Error as e:
                log.info("tray host refused the icon: %s", e.message)
        self.bus.call("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher",
                      "RegisterStatusNotifierItem", GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE,
                      -1, None, done)

    # ------------------------------------------------------------------ icons
    def _icon_name(self, attention: bool = False) -> str:
        if self.state == "offline":
            return APP_ID + "-offline"
        if self.state == "connecting":
            return APP_ID + "-connecting"
        return APP_ID + "-unread" if attention or self.unread else APP_ID

    def _pixmap(self, name: str) -> list:
        """The icon as ARGB32 pixels too: hosts that can't find our icon theme (GNOME's extension, Flatpak,
        /usr/local installs) draw these instead."""
        if name in self._pixmaps:
            return self._pixmaps[name]
        out = []
        from .. import paths

        candidates = [os.path.join(d, "hicolor", "scalable", "apps", name + ".svg")
                      for d in (self.icon_path, os.path.join(paths.share_dir(), "icons"),
                                os.path.join(os.path.dirname(paths.package_dir()), "data", "icons"))
                      if d]
        src = next((c for c in candidates if os.path.isfile(c)), None)
        if src is not None:
            try:
                import gi

                gi.require_version("GdkPixbuf", "2.0")
                from gi.repository import GdkPixbuf

                for size in (22, 32, 48, 64):
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_size(src, size, size)
                    if not pb.get_has_alpha():
                        pb = pb.add_alpha(False, 0, 0, 0)
                    w, h, stride = pb.get_width(), pb.get_height(), pb.get_rowstride()
                    raw = pb.get_pixels()
                    rgba = b"".join(raw[y * stride: y * stride + w * 4] for y in range(h))
                    argb = bytearray(len(rgba))     # network byte order ARGB
                    argb[0::4], argb[1::4], argb[2::4], argb[3::4] = rgba[3::4], rgba[0::4], rgba[1::4], rgba[2::4]
                    out.append((w, h, bytes(argb)))
            except Exception as e:
                log.debug("tray pixmap: %s", e)
        self._pixmaps[name] = out
        return out

    def _pixmap_variant(self, name: str) -> GLib.Variant:
        """The icon pixels as a(iiay), built once per icon.  GLib.Variant('a(iiay)', ...) wraps every byte in a
        Python object (about 60 ms for our four sizes), and a tray host asks for this property at every state
        change; new_from_bytes keeps the bytes as they are."""
        v = self._pixmap_variants.get(name)
        if v is None:
            ay_type = GLib.VariantType.new("ay")
            children = [
                GLib.Variant.new_tuple(
                    GLib.Variant.new_int32(w),
                    GLib.Variant.new_int32(h),
                    GLib.Variant.new_from_bytes(ay_type, GLib.Bytes.new(buf), True),
                )
                for w, h, buf in self._pixmap(name)
            ]
            v = GLib.Variant.new_array(GLib.VariantType.new("(iiay)"), children)
            self._pixmap_variants[name] = v
        return v

    def set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self._changed()

    def _changed(self) -> None:
        for sig, args in (("NewStatus", GLib.Variant("(s)", (self._status(),))), ("NewToolTip", None),
                          ("NewIcon", None), ("NewAttentionIcon", None)):
            try:
                self.bus.emit_signal(None, "/StatusNotifierItem", "org.kde.StatusNotifierItem", sig, args)
            except GLib.Error:
                pass

    def close(self) -> None:
        for i in self._ids:
            self.bus.unregister_object(i)
        self._ids = []
        if self._watch:
            Gio.bus_unwatch_name(self._watch)
        Gio.bus_unown_name(self._owner)

    def set_unread(self, n: int) -> None:
        if n == self.unread:
            return
        self.unread = n
        self._changed()

    def _status(self) -> str:
        return "NeedsAttention" if self.unread else "Active"

    def _toggle(self) -> None:
        w = self.app.window
        if w is not None and w.get_visible() and w.is_active():
            w.close()
        else:
            self.app.activate()

    # ------------------------------------------------------------------ StatusNotifierItem
    # One property at a time: a tray host asks for all of them in a row (GetAll), and answering with one dictionary
    # that rasterizes and packs both icons made every one of those calls pay for the pixels.
    def _sni_prop(self, _conn, _sender, _path, _iface, name):
        if name == "Category":
            return GLib.Variant("s", "Communications")
        if name == "Id":
            return GLib.Variant("s", APP_ID)
        if name == "Title":
            return GLib.Variant("s", APP_NAME)
        if name == "Status":
            return GLib.Variant("s", self._status())
        if name == "IconName":
            return GLib.Variant("s", self._icon_name())
        if name == "AttentionIconName":
            return GLib.Variant("s", self._icon_name(True))
        if name == "IconPixmap":
            return self._pixmap_variant(self._icon_name())
        if name == "AttentionIconPixmap":
            return self._pixmap_variant(self._icon_name(True))
        if name == "OverlayIconName":
            return GLib.Variant("s", "")
        if name == "WindowId":
            return GLib.Variant("i", 0)
        if name == "IconThemePath":
            return GLib.Variant("s", self.icon_path)
        if name == "ItemIsMenu":
            return GLib.Variant("b", False)
        if name == "Menu":
            return GLib.Variant("o", "/MenuBar")
        if name == "ToolTip":
            state = {"online": _("Connected"), "connecting": _("Connecting…"), "offline": _("Offline")}[self.state]
            tip = state + (" · " + _("%d unread") % self.unread if self.unread else "")
            return GLib.Variant("(sa(iiay)ss)", (self._icon_name(), [], APP_NAME, tip))
        return None

    def _sni_call(self, _conn, _sender, _path, _iface, method, _params, invocation) -> None:
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(lambda: self._toggle() and False)
        invocation.return_value(None)

    # ------------------------------------------------------------------ dbusmenu
    def _tree(self) -> list[tuple[int, dict, list]]:
        """The menu, as Gajim has it: show, status, mute sounds, preferences, quit."""
        current = self.app.common_show()
        statuses = [(10 + i, {"label": _(label), "toggle-type": "radio", "toggle-state": int(show == current)}, [])
                    for i, (show, label) in enumerate(STATUSES)]
        muted = not self.app.core.settings["notify.sounds"] if self.app.core is not None else False
        return [(SHOW, {"label": _("Show Ward")}, []),
                (SEPARATOR, {"type": "separator"}, []),
                (STATUS, {"label": _("Status"), "children-display": "submenu"}, statuses),
                (MUTE, {"label": _("Mute sounds"), "toggle-type": "checkmark", "toggle-state": int(muted)}, []),
                (SEPARATOR2, {"type": "separator"}, []),
                (PREFERENCES, {"label": _("Preferences")}, []),
                (QUIT, {"label": _("Quit")}, [])]

    def _variant(self, item: tuple[int, dict, list]) -> GLib.Variant:
        item_id, props, children = item
        return GLib.Variant("(ia{sv}av)", (item_id, {k: _wrap(v) for k, v in props.items()},
                                           [self._variant(c) for c in children]))

    def _flat(self) -> list[tuple[int, dict]]:
        out = []
        for item_id, props, children in self._tree():
            out.append((item_id, props))
            out.extend((c[0], c[1]) for c in children)
        return out

    def menu_changed(self) -> None:
        """Status or sounds changed: hosts fetch the menu again."""
        self.revision += 1
        try:
            self.bus.emit_signal(None, "/MenuBar", "com.canonical.dbusmenu", "LayoutUpdated",
                                 GLib.Variant("(ui)", (self.revision, 0)))
        except GLib.Error:
            pass

    def _menu_prop(self, _conn, _sender, _path, _iface, name):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}.get(name)

    def _menu_call(self, _conn, _sender, _path, _iface, method, params, invocation) -> None:
        if method == "GetLayout":
            children = [self._variant(i) for i in self._tree()]
            root = (0, {"children-display": GLib.Variant("s", "submenu")}, children)
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, root)))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            out = [(i, p) for i, p in self._flat() if not ids or i in ids]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (
                [(i, {k: _wrap(v) for k, v in p.items()}) for i, p in out],)))
        elif method == "GetProperty":
            invocation.return_value(GLib.Variant("(v)", (GLib.Variant("s", ""),)))
        elif method == "Event":
            item_id, event = params.unpack()[0], params.unpack()[1]
            if event == "clicked":
                GLib.idle_add(lambda: self._clicked(item_id) and False)
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event, _d, _t in params.unpack()[0]:
                if event == "clicked":
                    GLib.idle_add(lambda i=item_id: self._clicked(i) and False)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_value(None)

    def _clicked(self, item_id: int) -> None:
        if item_id == SHOW:
            self.app.activate()
        elif item_id == PREFERENCES:
            self.app.show_preferences()
        elif item_id == QUIT:
            self.app.quit_app(force=True)
        elif item_id == MUTE and self.app.core is not None:
            settings = self.app.core.settings
            settings.set("notify.sounds", not settings["notify.sounds"])
            self.menu_changed()
        elif 10 <= item_id < 10 + len(STATUSES):
            self.app.set_all_presence(STATUSES[item_id - 10][0])


def _wrap(v) -> GLib.Variant:
    if isinstance(v, bool):
        return GLib.Variant("b", v)
    if isinstance(v, int):
        return GLib.Variant("i", v)
    return GLib.Variant("s", str(v))
