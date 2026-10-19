"""The application: one window, one Core.  Handles the command line and xmpp: links, runs in the background when
the window is closed, and turns the core's notifications into desktop notifications with sounds and a badge."""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import APP_ID, APP_NAME, URL, __version__, aio, paths, util
from .. import log as wardlog
from ..core import uri as urimod
from ..core.core import Core
from ..i18n import _
from ..settings import Settings
from . import compat, style

log = logging.getLogger("ward.gui")
HERE = os.path.dirname(os.path.abspath(__file__))


SOUND_EXTENSIONS = (".oga", ".ogg", ".wav")


def _data_dirs() -> list[str]:
    home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return [home, *(os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")]


def sound_theme_has(name: str) -> bool:
    """Whether a sound theme (freedesktop, or one following it) has this sound, so playing it by name works."""
    for d in _data_dirs():
        base = os.path.join(d, "sounds")
        if not os.path.isdir(base):
            continue
        for theme in os.listdir(base):
            for sub in ("stereo", ""):
                if any(os.path.isfile(os.path.join(base, theme, sub, name + ext)) for ext in SOUND_EXTENSIONS):
                    return True
    return False


def bundled_sound(name: str) -> str:
    """The copy of a freedesktop sound that Ward ships (share/ward/sounds), or ""."""
    if not name or "/" in name:
        return ""
    share = paths.share_dir()
    for ext in SOUND_EXTENSIONS:
        path = os.path.join(share, "sounds", name + ext) if share else ""
        if path and os.path.isfile(path):
            return path
    return ""


class WardApplication(Adw.Application):
    __gtype_name__ = "WardApplication"

    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.core: Core | None = None
        self.window = None
        self.pending_uri = ""
        self._held = False
        self._quitting = False
        self._badge_count = -1
        self.tray = None

    # ================================================================== startup
    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        from ..core.omemo import preload as preload_omemo

        preload_omemo()     # about 170 ms of library imports, in the background while the window is built
        if GLib.get_application_name() != APP_NAME:
            GLib.set_application_name(APP_NAME)
        settings = Settings()
        if settings["advanced.debug_log"] != "off":
            wardlog.setup(settings["advanced.debug_log"], to_file=True)
        display = Gdk.Display.get_default()
        if display is not None:
            theme = Gtk.IconTheme.get_for_display(display)
            theme.add_search_path(os.path.join(HERE, "icons"))
            for d in (os.path.join(paths.share_dir(), "icons"),):
                if os.path.isdir(d):
                    theme.add_search_path(d)
        style.install()
        style.set_appearance(settings)
        err = style.load_custom(settings["advanced.custom_css"])
        if err:
            log.warning("custom.css: %s", err)
        Gtk.Window.set_default_icon_name(APP_ID)
        # WARD_OFFLINE=1 opens everything without connecting (screenshots, trying the interface)
        self.core = Core(settings, connect=not os.environ.get("WARD_OFFLINE"))
        self.core.notifier.connect("show-notification", self._on_notification)
        self.core.notifier.connect("withdraw-notification", lambda _n, nid: self.withdraw_notification(nid))
        from .call import CallUI

        self.calls_ui = CallUI(self)
        self.core.chats.connect("unread-changed", lambda *_: self._update_badge())
        self.core.chats.connect("activity", lambda *_: self._update_badge())
        self.core.accounts.store.connect("items-changed", lambda *_: self._watch_accounts())
        self._watched_accounts: set[int] = set()
        settings.connect("changed", self._on_setting)
        self._install_actions()
        if settings["general.background"]:
            self._hold(True)
        aio.spawn(self._start_core())
        import signal

        self._write_session_env()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_signal)
        from .. import memreport

        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, memreport.write)   # ward --memory-report
        if settings["general.tray"] != "never":
            self._setup_tray()

    async def _start_core(self) -> None:
        try:
            await self.core.start()
        except Exception as e:
            log.exception("could not start")
            self._fatal(str(e))
            return
        if self.window is not None:
            self.window.ready()
        self._watch_accounts()
        self._update_badge()
        if self.pending_uri and self.core.accounts.list():
            uri, self.pending_uri = self.pending_uri, ""
            self.open_uri(uri)

    def _fatal(self, text: str) -> None:
        self.activate()
        compat.alert(self.window, _("Ward could not start"), text, [("quit", _("Quit"), "")],
                     lambda _r: self.quit())

    def _install_actions(self) -> None:
        def add(name, cb, accels=None, param=None):
            a = Gio.SimpleAction.new(name, GLib.VariantType.new(param) if param else None)
            a.connect("activate", lambda _a, p: cb(p.unpack() if p is not None else None))
            self.add_action(a)
            if accels:
                self.set_accels_for_action("app." + name, accels)
        add("quit", lambda _p: self.quit_app(), ["<Ctrl>q"])
        add("preferences", lambda _p: self.show_preferences(), ["<Ctrl>comma"])
        add("about", lambda _p: self.show_about())
        add("show", lambda _p: self.activate())
        add("open-chat", lambda p: self._open_chat(p), param="s")
        add("mark-read", lambda p: self._mark_read(p), param="s")
        add("open-uri", lambda p: self.open_uri(p), param="s")

    # ================================================================== command line
    def do_command_line(self, cmdline: Gio.ApplicationCommandLine) -> int:
        args = cmdline.get_arguments()[1:]
        background = False
        uris = []
        for a in args:
            if a in ("--background", "-b", "--hidden"):
                background = True
            elif a in ("--quit", "-q"):
                self.quit_app(force=True)
                return 0
            elif a.startswith("-"):
                continue
            else:
                uris.append(a)
        if background and self.window is None:
            self._hold(True)
        else:
            self.activate()
        for u in uris:
            self.open_uri(u)
        return 0

    def do_activate(self) -> None:
        if self.window is None:
            from . import lifetime
            from .window import MainWindow

            with lifetime.scope() as handlers:
                self.window = MainWindow(self)
            self.window.built_handlers = handlers
        self.window.present()
        self.core.set_window_state(self.window.is_active(), True)

    # ================================================================== background
    def _hold(self, on: bool) -> None:
        if on and not self._held:
            self.hold()
            self._held = True
        elif not on and self._held:
            self.release()
            self._held = False

    def background_supported(self) -> bool:
        return True

    def quit_app(self, force: bool = False) -> None:
        if self._quitting:
            return
        if not force and self.core is not None and self.core.settings["general.confirm_quit"] and \
                self.window is not None:
            compat.alert(self.window, _("Quit Ward?"), _("You won't get messages until you start it again."),
                         [("cancel", _("Cancel"), ""), ("quit", _("Quit"), "destructive")],
                         lambda r: r == "quit" and self.quit_app(force=True))
            return
        self._quitting = True
        aio.spawn(self._shutdown())

    async def _shutdown(self) -> None:
        # under runit, tell the supervisor this exit is wanted, or runsv would start Ward again right away
        svc = os.environ.get("WARD_RUNIT_SERVICE")
        if svc and shutil.which("sv"):
            with contextlib.suppress(OSError):
                subprocess.Popen(["sv", "-w", "0", "down", svc], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            if self.core is not None and self.core.started:
                await self.core.shutdown()
        except Exception:
            log.exception("shutdown failed")
        self._set_badge(0)
        if self.tray is not None:
            self.tray.close()
        self._hold(False)
        if self.window is not None:
            self.window.destroy()
        self.quit()

    def release_window_later(self, window) -> None:
        """Running in the background with the window closed: let the window go after a while (reopening it soon
        just shows it again), with everything GTK and its renderer hold for it, and give the freed memory back to
        the system.  Opening Ward later builds a new window."""
        from . import lifetime

        def release() -> bool:
            if self.window is window and not window.get_visible():
                self.window = None
                lifetime.unwatch_all()
                window.teardown()
                window.destroy()
                GLib.timeout_add_seconds(2, self._trim_memory)
            return False
        GLib.timeout_add_seconds(lifetime.FORGET_AFTER, release)

    @staticmethod
    def _trim_memory() -> bool:
        """Hand freed heap memory back to the system (glibc keeps it otherwise); a no-op elsewhere."""
        import gc

        gc.collect()
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.CDLL("libc.so.6").malloc_trim(0)
        return False

    def _on_setting(self, settings, key: str) -> None:
        if key == "notify.sounds" and self.tray is not None:
            self.tray.menu_changed()
        if key == "general.background":
            self._hold(settings[key])
        elif key == "general.autostart":
            self._set_autostart(settings[key])
        elif key.startswith("appearance."):
            style.set_appearance(settings)
        elif key == "advanced.custom_css":
            err = style.load_custom(settings[key])
            if err and self.window is not None:
                compat.toast(self.window, _("custom.css: %s") % err)
        elif key == "advanced.debug_log":
            wardlog.setup(settings[key], to_file=settings[key] != "off")
        elif key == "notify.badge":
            self._update_badge()
        elif key == "general.tray":
            if settings[key] == "never" and self.tray is not None:
                self.tray.close()
                self.tray = None
            elif settings[key] != "never" and self.tray is None:
                self._setup_tray()

    def _set_autostart(self, on: bool) -> None:
        if paths.in_flatpak():
            self._portal_background(on)
            return
        path = paths.autostart_file()
        if on:
            exe = shutil.which("ward") or "ward"
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("[Desktop Entry]\nType=Application\nName=%s\nExec=%s --background\nIcon=%s\n"
                         "X-GNOME-Autostart-enabled=true\nNoDisplay=true\n" % (APP_NAME, exe, APP_ID))
        elif os.path.exists(path):
            os.unlink(path)

    def _portal_background(self, autostart: bool) -> None:
        """Flatpak: ask the Background portal to start us at login."""
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            opts = {"reason": GLib.Variant("s", _("Receive messages in the background")),
                    "autostart": GLib.Variant("b", autostart),
                    "commandline": GLib.Variant("as", ["ward", "--background"])}
            bus.call("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                     "org.freedesktop.portal.Background", "RequestBackground",
                     GLib.Variant("(sa{sv})", ("", opts)), None, Gio.DBusCallFlags.NONE, -1, None, None)
        except Exception as e:
            log.info("background portal: %s", e)

    def _setup_tray(self) -> None:
        if self.tray is not None:
            return
        try:
            from .tray import Tray

            self.tray = Tray(self)
            self._update_tray_state()
            self.tray.set_unread(max(self._badge_count, 0))
        except Exception as e:
            log.debug("no tray: %s", e)
            self.tray = None
        if not getattr(self, "_tray_watch", 0):
            # a tray host can appear later (Plasma restarting, the GNOME AppIndicator extension being switched on)
            self._tray_watch = Gio.bus_watch_name(Gio.BusType.SESSION, "org.kde.StatusNotifierWatcher",
                                                  Gio.BusNameWatcherFlags.NONE,
                                                  lambda *_: self.tray is None and self._tray_wanted()
                                                  and self._setup_tray(), None)

    @staticmethod
    def _write_session_env() -> None:
        """For the runit user service: runsvdir does not inherit the desktop session's variables, so a Ward started
        from the desktop leaves them where data/init/runit/run reads them."""
        rt = os.environ.get("XDG_RUNTIME_DIR")
        if not rt or os.environ.get("WARD_RUNIT_SERVICE") or not os.path.isdir(rt):
            return
        keep = ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE",
                "XAUTHORITY")
        lines = ["export %s='%s'" % (k, os.environ[k].replace("'", "")) for k in keep if os.environ.get(k)]
        with contextlib.suppress(OSError):
            path = os.path.join(rt, "ward.env")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            os.chmod(path, 0o600)

    def _watch_accounts(self) -> None:
        for acc in self.core.accounts.list():
            if acc.id not in self._watched_accounts:
                self._watched_accounts.add(acc.id)
                acc.connect("notify::state", lambda *_: self._update_tray_state())
                acc.connect("notify::enabled", lambda *_: self._update_tray_state())
                acc.connect("notify::show", self._on_account_show_changed)
        self._update_tray_state()

    def common_show(self) -> str:
        """The status all enabled accounts share, or "" when they differ."""
        shows = {a.show if a.state == "online" or a.show == "offline" else "offline"
                 for a in self.core.accounts.list() if a.enabled} if self.core is not None else set()
        return shows.pop() if len(shows) == 1 else ""

    def set_all_presence(self, show: str) -> None:
        """From the tray: the same status for every enabled account."""
        if self.core is None:
            return
        for acc in self.core.accounts.list():
            if acc.enabled:
                aio.spawn(self.core.set_presence(acc, show))

    def connection_state(self) -> str:
        """online when any enabled account is connected, connecting while one tries, offline otherwise."""
        states = [a.state for a in self.core.accounts.list() if a.enabled]
        if "online" in states:
            return "online"
        if "connecting" in states:
            return "connecting"
        return "offline"

    def _on_account_show_changed(self, *_a) -> None:
        self._update_tray_state()
        if self.tray is not None:
            self.tray.menu_changed()

    def _update_tray_state(self) -> None:
        if self.tray is not None and self.core is not None:
            self.tray.set_state(self.connection_state(), self.common_show())

    def _tray_wanted(self) -> bool:
        return self.core is not None and self.core.settings["general.tray"] != "never"

    def _on_signal(self) -> bool:
        """SIGTERM/SIGINT from systemd, runit (sv down) or a terminal: shut down cleanly."""
        self.quit_app(force=True)
        return False

    # ================================================================== notifications
    def _on_notification(self, _notifier, note) -> None:
        n = Gio.Notification.new(note.title)
        n.set_body(note.body)
        if hasattr(Gio, "NotificationPriority"):
            n.set_priority(Gio.NotificationPriority.HIGH if note.priority == "high"
                           else Gio.NotificationPriority.NORMAL)
        if note.icon and os.path.exists(note.icon):
            n.set_icon(Gio.FileIcon.new(Gio.File.new_for_path(note.icon)))
        n.set_default_action_and_target("app.open-chat", GLib.Variant("s", str(note.chat_id)))
        if not note.is_request:
            n.add_button_with_target(_("Mark as read"), "app.mark-read", GLib.Variant("s", str(note.chat_id)))
        self.send_notification(note.id, n)
        if note.sound:
            self.play_sound(self.core.settings[note.sound])

    def notify_simple(self, nid: str, title: str, body: str) -> None:
        n = Gio.Notification.new(title)
        n.set_body(body)
        n.set_default_action("app.show")
        self.send_notification(nid, n)

    def withdraw_chat_notification(self, chat) -> None:
        self.core.notifier.chat_read(chat)

    def play_sound(self, name: str) -> None:
        """A freedesktop sound by name, from the sound theme.  Systems without the theme (Void, minimal installs)
        get the copy Ward ships in share/ward/sounds."""
        if not name:
            return
        if os.path.isabs(name):         # a file chosen in the preferences
            if os.path.isfile(name):
                self._play_file(name)
                return
            name = "message-new-instant"
        if not sound_theme_has(name):
            # Ward ships one sound: it stands in for the others (contact requests) too
            path = bundled_sound(name) or bundled_sound("message-new-instant")
            if path:
                self._play_file(path)
                return
        try:
            import gi

            gi.require_version("GSound", "1.0")
            from gi.repository import GSound

            if not hasattr(self, "_gsound"):
                self._gsound = GSound.Context()
                self._gsound.init()
            self._gsound.play_simple({GSound.ATTR_EVENT_ID: name})
            return
        except Exception:
            pass
        player = shutil.which("canberra-gtk-play")
        if player:
            try:
                subprocess.Popen([player, "-i", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass

    def _play_file(self, path: str) -> None:
        try:
            import gi

            gi.require_version("GSound", "1.0")
            from gi.repository import GSound

            if not hasattr(self, "_gsound"):
                self._gsound = GSound.Context()
                self._gsound.init()
            self._gsound.play_simple({GSound.ATTR_MEDIA_FILENAME: path})
            return
        except Exception:
            pass
        # PipeWire, PulseAudio, libcanberra, then GStreamer (which plays .oga on its own, even with plain ALSA)
        for cmd in (["pw-play", path], ["paplay", path], ["canberra-gtk-play", "-f", path],
                    ["gst-play-1.0", "--no-interactive", "-q", path], ["ogg123", "-q", path]):
            if shutil.which(cmd[0]):
                try:
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
                except OSError:
                    continue
        log.info("no program to play %s (install pipewire, pulseaudio-utils or gst-plugins-base)", path)

    def _open_chat(self, chat_id: str) -> None:
        self.activate()
        try:
            self.window.open_chat_id(int(chat_id))
        except ValueError:
            pass

    def _mark_read(self, chat_id: str) -> None:
        try:
            chat = self.core.chats.by_id.get(int(chat_id))
        except ValueError:
            return
        if chat is not None:
            aio.spawn(self.core.messaging.mark_read(chat))
            self.core.notifier.chat_read(chat)

    def _update_badge(self) -> None:
        n = self.core.chats.total_unread() if self.core is not None and self.core.started else 0
        if not self.core.settings["notify.badge"]:
            n = 0
        self._set_badge(n)
        if self.tray is not None:
            self.tray.set_unread(n)

    def _set_badge(self, n: int) -> None:
        """The Unity launcher API: docks (GNOME Dash to Dock, KDE, Plank) show it on the app icon."""
        if n == self._badge_count:
            return
        self._badge_count = n
        try:
            bus = self.get_dbus_connection()
            if bus is None:
                return
            props = {"count": GLib.Variant("x", n), "count-visible": GLib.Variant("b", n > 0)}
            bus.emit_signal(None, "/com/canonical/unity/launcherentry/%d" % (abs(hash(APP_ID)) % 10**9),
                            "com.canonical.Unity.LauncherEntry", "Update",
                            GLib.Variant("(sa{sv})", ("application://%s.desktop" % APP_ID, props)))
        except Exception as e:
            log.debug("badge: %s", e)

    # ================================================================== links
    def open_uri(self, text: str) -> None:
        """xmpp: links from the browser or other apps (and from messages)."""
        text = (text or "").strip()
        if text.startswith("file://") or (text and not text.lower().startswith("xmpp:") and "#xmpp:" in text):
            text = text.split("#", 1)[1] if "#xmpp:" in text else text
        u = urimod.parse_xmpp(text)
        if u is None:
            log.info("not an xmpp: link: %s", text[:80])
            return
        if self.window is None:
            self.activate()
        if self.core is None or not self.core.started:
            self.pending_uri = text
            return
        w = self.window
        from . import dialogs

        accounts = [a for a in self.core.accounts.list() if a.enabled]
        if u.is_account_invite or not accounts:
            page = w.show_welcome(adding=bool(accounts))
            if page is not None and (u.is_account_invite or u.is_invite):
                page.use_invite(text)
            else:
                self.pending_uri = text
            return
        acc = next((a for a in accounts if self.core.contacts.get(a.id, u.jid) is not None), accounts[0])
        if u.omemo:
            s = self.core.sessions.get(acc.id)
            if s is not None and s.online and self.core.omemo is not None:
                async def verify():
                    ok, bad = await self.core.omemo.verify_uri(s, u)
                    if bad:
                        compat.toast(w, _("Some fingerprints in the link did not match"))
                    elif ok:
                        compat.toast(w, _("Verified %d devices") % ok)
                aio.spawn(verify())
        if util.bare(u.jid) == acc.jid:
            return
        if u.action == "join" or (not u.action and self.core.muc.is_room(acc.id, u.jid)):
            chat = self.core.chats.find(acc.id, util.bare(u.jid), "groupchat")
            if chat is not None and chat.joined:
                w.show_chat(chat)
            else:
                dialogs.JoinGroupDialog(w, u.jid, u.params.get("password", ""), acc).present(w)
        elif u.action in ("roster", "subscribe") or (u.is_invite and self.core.contacts.get(acc.id, u.jid) is None):
            dialogs.AddContactDialog(w, u.jid, u.params.get("name", "")).present(w)
        else:
            async def go():
                chat = await self.core.open_chat(acc, u.jid)
                w.show_chat(chat)
                body = u.params.get("body")
                if body:
                    w.conversation.composer.buffer.set_text(body)
            aio.spawn(go())

    def install_encryption(self, parent=None) -> None:
        from .dialogs import ExtrasDialog

        self.activate()
        ExtrasDialog(self.window).present(parent or self.window)

    def restart(self) -> None:
        """After installing libraries: start a fresh process with the same environment."""
        import sys

        async def go():
            if self.core is not None and self.core.started:
                await self.core.shutdown()
            os.execv(sys.executable, [sys.executable, "-m", "ward"])
        aio.spawn(go())

    # ================================================================== windows
    def show_preferences(self, page: str = "") -> None:
        self.activate()
        from .prefs import Preferences

        Preferences(self.window, page).present(self.window)

    def show_about(self) -> None:
        self.activate()
        kw = dict(application_name=APP_NAME, application_icon=APP_ID, version="v" + __version__, website=URL,
                  issue_url=URL + "/issues", license_type=Gtk.License.MIT_X11, developer_name="Ward contributors",
                  comments=_("A private, modern XMPP client"),
                  copyright="© Ward contributors")
        if hasattr(Adw, "AboutDialog"):
            d = Adw.AboutDialog(**kw)
            d.add_legal_section("OMEMO", "", Gtk.License.CUSTOM,
                                _("Legacy OMEMO support (oldmemo) is licensed under the AGPL-3.0 and is optional."))
            d.present(self.window)
        else:
            d = Adw.AboutWindow(transient_for=self.window, **kw)
            d.present()
