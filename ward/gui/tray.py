"""A tray icon through StatusNotifierItem (KDE, XFCE, Cinnamon, MATE, LXQt, Budgie, the GNOME AppIndicator
extension), with a small com.canonical.dbusmenu menu.  Plain D-Bus through Gio: no extra library, and nothing at
all happens when no tray is running."""

from __future__ import annotations

import logging
import os

from gi.repository import Gio, GLib

from .. import APP_ID, APP_NAME
from ..i18n import _

log = logging.getLogger("ward.tray")

SNI_XML = """<node><interface name="org.kde.StatusNotifierItem">
<property name="Category" type="s" access="read"/><property name="Id" type="s" access="read"/>
<property name="Title" type="s" access="read"/><property name="Status" type="s" access="read"/>
<property name="IconName" type="s" access="read"/><property name="AttentionIconName" type="s" access="read"/>
<property name="IconThemePath" type="s" access="read"/><property name="ItemIsMenu" type="b" access="read"/>
<property name="Menu" type="o" access="read"/><property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
<method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
<signal name="NewIcon"/><signal name="NewStatus"><arg type="s"/></signal><signal name="NewToolTip"/>
<signal name="NewTitle"/></interface></node>"""

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

ITEMS = ((1, "show"), (4, "preferences"), (2, "separator"), (3, "quit"))
LABELS = {"show": "Show Ward", "preferences": "Preferences", "quit": "Quit"}


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
        from .. import paths

        icons = os.path.join(paths.share_dir(), "icons") if paths.share_dir() else ""
        self.icon_path = icons if icons and os.path.isdir(icons) else ""
        self.name = "org.kde.StatusNotifierItem-%d-1" % os.getpid()
        self._ids = []
        sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self._ids.append(self.bus.register_object_with_closures("/StatusNotifierItem", sni, self._sni_call,
                                                                self._sni_prop, None))
        self._ids.append(self.bus.register_object_with_closures("/MenuBar", menu, self._menu_call, self._menu_prop,
                                                                None))
        self._owner = Gio.bus_own_name_on_connection(self.bus, self.name, Gio.BusNameOwnerFlags.NONE, None, None)
        self._watch = Gio.bus_watch_name_on_connection(self.bus, "org.kde.StatusNotifierWatcher",
                                                       Gio.BusNameWatcherFlags.NONE, self._watcher_up, None)
        self.revision = 1

    def _watcher_up(self, *_a) -> None:
        self.bus.call("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher",
                      "RegisterStatusNotifierItem", GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE,
                      -1, None, None)

    def close(self) -> None:
        for i in self._ids:
            self.bus.unregister_object(i)
        self._ids = []
        Gio.bus_unwatch_name(self._watch)
        Gio.bus_unown_name(self._owner)

    def set_unread(self, n: int) -> None:
        if n == self.unread:
            return
        self.unread = n
        for sig, args in (("NewStatus", GLib.Variant("(s)", (self._status(),))), ("NewToolTip", None),
                          ("NewIcon", None)):
            try:
                self.bus.emit_signal(None, "/StatusNotifierItem", "org.kde.StatusNotifierItem", sig, args)
            except GLib.Error:
                pass

    def _status(self) -> str:
        return "NeedsAttention" if self.unread else "Active"

    def _toggle(self) -> None:
        w = self.app.window
        if w is not None and w.get_visible() and w.is_active():
            w.close()
        else:
            self.app.activate()

    # ------------------------------------------------------------------ StatusNotifierItem
    def _sni_prop(self, _conn, _sender, _path, _iface, name):
        tip = _("%d unread") % self.unread if self.unread else ""
        values = {
            "Category": GLib.Variant("s", "Communications"), "Id": GLib.Variant("s", APP_ID),
            "Title": GLib.Variant("s", APP_NAME), "Status": GLib.Variant("s", self._status()),
            "IconName": GLib.Variant("s", APP_ID), "AttentionIconName": GLib.Variant("s", APP_ID),
            "IconThemePath": GLib.Variant("s", self.icon_path), "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", "/MenuBar"),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (APP_ID, [], APP_NAME, tip)),
        }
        return values.get(name)

    def _sni_call(self, _conn, _sender, _path, _iface, method, _params, invocation) -> None:
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(lambda: self._toggle() and False)
        invocation.return_value(None)

    # ------------------------------------------------------------------ dbusmenu
    def _item(self, item_id: int, kind: str) -> GLib.Variant:
        if kind == "separator":
            props = {"type": GLib.Variant("s", "separator")}
        else:
            props = {"label": GLib.Variant("s", _(LABELS[kind])),
                     "enabled": GLib.Variant("b", True), "visible": GLib.Variant("b", True)}
        return GLib.Variant("(ia{sv}av)", (item_id, props, []))

    def _menu_prop(self, _conn, _sender, _path, _iface, name):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}.get(name)

    def _menu_call(self, _conn, _sender, _path, _iface, method, params, invocation) -> None:
        if method == "GetLayout":
            children = [self._item(i, k) for i, k in ITEMS]
            root = (0, {"children-display": GLib.Variant("s", "submenu")}, children)
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, root)))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            out = []
            for i, k in ITEMS:
                if not ids or i in ids:
                    out.append((i, self._item(i, k).unpack()[1]))
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
        if item_id == 1:
            self.app.activate()
        elif item_id == 4:
            self.app.show_preferences()
        elif item_id == 3:
            self.app.quit_app(force=True)


def _wrap(v) -> GLib.Variant:
    if isinstance(v, bool):
        return GLib.Variant("b", v)
    return GLib.Variant("s", str(v))
