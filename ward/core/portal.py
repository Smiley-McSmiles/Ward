"""XDG ScreenCast portal: hand PipeWire a screen stream for Wayland screen sharing.

The portal asks the user which screen or window to share.  Its methods return a request handle and deliver the
answer on org.freedesktop.portal.Request.Response; we use a small nested GLib loop while that dialog is up, then
pass the PipeWire node and file descriptor to GStreamer's pipewiresrc.
"""

from __future__ import annotations

import os
import secrets

from gi.repository import Gio, GLib

PORTAL = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"
REQUEST = "org.freedesktop.portal.Request"
SESSION = "org.freedesktop.portal.Session"


class PortalError(Exception):
    pass


def _token() -> str:
    return "ward" + secrets.token_hex(6)


def _sender(bus) -> str:
    return (bus.get_unique_name() or "").lstrip(":").replace(".", "_")


def _parent_window() -> str:
    """The portal needs the focused window, or its chooser cannot be associated with Ward."""
    try:
        import gi
        from gi.repository import Gtk

        app = Gtk.Application.get_default()
        window = app.get_active_window() if app is not None else None
        surface = window.get_surface() if window is not None else None
        if surface is None:
            return ""
        try:
            gi.require_version("GdkX11", "4.0")
            from gi.repository import GdkX11

            if isinstance(surface, GdkX11.X11Surface):
                return "x11:%d" % surface.get_xid()
        except (ImportError, ValueError):
            pass
        try:
            gi.require_version("GdkWayland", "4.0")
            from gi.repository import GdkWayland

            if isinstance(surface, GdkWayland.WaylandToplevel):
                loop = GLib.MainLoop()
                handles: dict[str, str] = {}

                def exported(_toplevel, handle, _data) -> bool:
                    if handle:
                        handles["handle"] = handle
                    loop.quit()
                    return True

                if surface.export_handle(exported, None):
                    timed_out = {"value": False}

                    def stop() -> bool:
                        timed_out["value"] = True
                        loop.quit()
                        return False
                    timeout = GLib.timeout_add_seconds(2, stop)
                    loop.run()
                    if not timed_out["value"]:
                        GLib.source_remove(timeout)
                    if handles.get("handle"):
                        return "wayland:" + handles["handle"]
        except (ImportError, ValueError):
            pass
    except Exception:
        pass
    return ""


def _request(bus, proxy, method: str, parameters, timeout: int = 180):
    # The portal answers on /request/<sender>/<handle_token>; use the token carried in the method options.
    values = parameters.unpack()
    options = values[-1] if values else {}
    token = options.get("handle_token") if isinstance(options, dict) else None
    if isinstance(token, GLib.Variant):
        token = token.unpack()
    token = token or _token()
    path = "/org/freedesktop/portal/desktop/request/%s/%s" % (_sender(bus), token)
    loop = GLib.MainLoop()
    result = {}
    response = {"code": 2, "done": False}

    def on_response(_conn, _sender_name, _path, _interface, _signal, params, _data):
        code, values = params.unpack()
        response["code"] = code
        response["done"] = True
        result.update(values)
        loop.quit()

    subscription = bus.signal_subscribe(PORTAL, REQUEST, "Response", path, None,
                                        Gio.DBusSignalFlags.NONE, on_response, None)
    timeout_id = 0
    try:
        proxy.call_sync(method, parameters, Gio.DBusCallFlags.NONE, timeout * 1000, None)
        if not response["done"]:
            timeout_id = GLib.timeout_add_seconds(timeout, loop.quit)
            loop.run()
    finally:
        if timeout_id:
            GLib.source_remove(timeout_id)
        bus.signal_unsubscribe(subscription)
    if response["code"] != 0:
        raise PortalError("The screen-sharing dialog was cancelled" if response["code"] == 1
                          else "The screen-sharing portal failed")
    return result


def screencast(parent_window: str | None = None) -> tuple[int, int, str]:
    """Return (PipeWire fd, node id, portal session handle).  The fd is a duplicate owned by the caller."""
    if parent_window is None:
        parent_window = _parent_window()
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        proxy = Gio.DBusProxy.new_sync(bus, Gio.DBusProxyFlags.NONE, None, PORTAL, PATH, SCREENCAST, None)
    except GLib.Error as e:
        raise PortalError("The screen-sharing portal is not available") from e
    if proxy.get_name_owner() is None:
        raise PortalError("The screen-sharing portal is not running")

    session_token = _token()
    created = _request(bus, proxy, "CreateSession", GLib.Variant("(a{sv})", ({
        "handle_token": GLib.Variant("s", _token()),
        "session_handle_token": GLib.Variant("s", session_token),
    },)))
    session = created.get("session_handle")
    if not session:
        raise PortalError("The screen-sharing portal did not create a session")

    _request(bus, proxy, "SelectSources", GLib.Variant("(oa{sv})", (session, {
        "handle_token": GLib.Variant("s", _token()),
        "types": GLib.Variant("u", 3),             # monitors and windows
        "multiple": GLib.Variant("b", False),
        "cursor_mode": GLib.Variant("u", 2),       # embedded cursor
    })))

    started = _request(bus, proxy, "Start", GLib.Variant("(osa{sv})", (session, parent_window, {
        "handle_token": GLib.Variant("s", _token()),
    })))
    streams = started.get("streams") or []
    if not streams:
        raise PortalError("The screen-sharing portal returned no stream")
    node = int(streams[0][0])

    try:
        variant, fds = proxy.call_with_unix_fd_list_sync(
            "OpenPipeWireRemote", GLib.Variant("(oa{sv})", (session, {})), Gio.DBusCallFlags.NONE, -1, None, None)
        index = variant.unpack()[0]
        fd = os.dup(fds.get(index))
    except (GLib.Error, TypeError, IndexError) as e:
        raise PortalError("Could not open the PipeWire screen stream") from e
    return fd, node, session


def close_session(session: str) -> None:
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(PORTAL, session, SESSION, "Close", None, None, Gio.DBusCallFlags.NONE, 1000, None)
    except GLib.Error:
        pass
