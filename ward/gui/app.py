"""The application: one window, one Core.  Handles the command line and xmpp: links, runs in the background when
the window is closed, and turns the core's notifications into desktop notifications with sounds and a badge."""

from __future__ import annotations

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
        self.core.chats.connect("unread-changed", lambda *_: self._update_badge())
        self.core.chats.connect("activity", lambda *_: self._update_badge())
        settings.connect("changed", self._on_setting)
        self._install_actions()
        if settings["general.background"]:
            self._hold(True)
        aio.spawn(self._start_core())
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
            from .window import MainWindow

            self.window = MainWindow(self)
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

    def _on_setting(self, settings, key: str) -> None:
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
        try:
            from .tray import Tray

            self.tray = Tray(self)
        except Exception as e:
            log.debug("no tray: %s", e)
            self.tray = None

    # ================================================================== notifications
    def _on_notification(self, _notifier, note) -> None:
        n = Gio.Notification.new(note.title)
        n.set_body(note.body)
        if hasattr(Gio, "NotificationPriority"):
            n.set_priority(Gio.NotificationPriority.HIGH if note.priority == "high"
                           else Gio.NotificationPriority.NORMAL)
        if note.icon and os.path.exists(note.icon):
            n.set_icon(Gio.FileIcon.new(Gio.File.new_for_path(note.icon)))
        n.set_default_action_and_target_value("app.open-chat", GLib.Variant("s", str(note.chat_id)))
        if not note.is_request:
            n.add_button_with_target_value(_("Mark as read"), "app.mark-read", GLib.Variant("s", str(note.chat_id)))
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
        if not name:
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

    # ================================================================== windows
    def show_preferences(self, page: str = "") -> None:
        self.activate()
        from .prefs import Preferences

        Preferences(self.window, page).present(self.window)

    def show_about(self) -> None:
        self.activate()
        kw = dict(application_name=APP_NAME, application_icon=APP_ID, version=__version__, website=URL,
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
