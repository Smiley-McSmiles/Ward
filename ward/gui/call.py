"""Calls on screen: the call window (ringing, connecting, in a call), the incoming-call notification and ringtone,
and starting a call from a chat."""

from __future__ import annotations

from gi.repository import Adw, Gio, GLib, Gtk

from ..i18n import _
from . import widgets


class CallUI:
    """Follows the core's calls: a window for each, a notification and a ringtone while one rings here."""

    def __init__(self, app):
        self.app = app
        self.core = app.core
        self.windows: dict[tuple[int, str], CallWindow] = {}
        self._ring_source = 0
        calls = self.core.calls
        calls.connect("added", lambda _s, call: self._added(call))
        calls.connect("incoming", lambda _s, call: self._incoming(call))
        calls.connect("ended", lambda _s, call: self._ended(call))
        for name, cb in (("call-accept", self._accept), ("call-accept-video", self._accept_video),
                         ("call-decline", self._decline)):
            a = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            a.connect("activate", lambda _a, p, cb=cb: self._with_call(p.get_string(), cb))
            app.add_action(a)

    @staticmethod
    def key(call) -> str:
        return "%d:%s" % (call.account_id, call.sid)

    def _with_call(self, key: str, cb) -> None:
        acc, _sep, sid = key.partition(":")
        call = self.core.calls.find(int(acc or 0), sid)
        if call is not None:
            cb(call)

    # ------------------------------------------------------------------ starting
    def start(self, chat, video: bool) -> None:
        acc = self.core.accounts.get(chat.account_id)
        if acc is None:
            return
        if self.core.calls.active_call() is not None:
            win = next(iter(self.windows.values()), None)
            if win is not None:
                win.present()
            return
        widgets.run(self.app.window, self.core.calls.start(acc, chat.jid, video), error=_("Could not call"))

    # ------------------------------------------------------------------ following calls
    def _added(self, call) -> None:
        win = CallWindow(self, call)
        self.windows[(call.account_id, call.sid)] = win
        if call.outgoing:
            win.present()

    def _incoming(self, call) -> None:
        win = self.windows.get((call.account_id, call.sid))
        if win is not None:
            win.present()
        name = self.core.names.contact_name(call.account_id, call.peer) or call.peer
        n = Gio.Notification.new(_("%s is calling") % name)
        n.set_body(_("Video call") if call.video else _("Audio call"))
        if hasattr(n, "set_category"):
            n.set_category("call.incoming")
        n.set_priority(Gio.NotificationPriority.URGENT)
        target = GLib.Variant("s", self.key(call))
        n.set_default_action("app.show")
        n.add_button_with_target(_("Decline"), "app.call-decline", target)
        if call.video:
            n.add_button_with_target(_("Video"), "app.call-accept-video", target)
        n.add_button_with_target(_("Accept"), "app.call-accept", target)
        self.app.send_notification("call-" + self.key(call), n)
        self._ring(True)

    def _ended(self, call) -> None:
        self.app.withdraw_notification("call-" + self.key(call))
        if not any(c.state == "incoming" for c in self.core.calls.calls.values()):
            self._ring(False)
        win = self.windows.pop((call.account_id, call.sid), None)
        if win is not None:
            win.ended()

    def _ring(self, on: bool) -> None:
        if on and not self._ring_source:
            def ring() -> bool:
                if self.core.settings["notify.sounds"]:
                    self.app.play_sound(self.core.settings["notify.sound_call"] or "phone-incoming-call")
                return True
            ring()
            self._ring_source = GLib.timeout_add_seconds(3, ring)
        elif not on and self._ring_source:
            GLib.source_remove(self._ring_source)
            self._ring_source = 0

    # ------------------------------------------------------------------ answering
    def _accept(self, call, video: bool = False) -> None:
        self._ring(False)
        self.app.withdraw_notification("call-" + self.key(call))
        win = self.windows.get((call.account_id, call.sid))
        if win is not None:
            win.present()
        widgets.run(self.app.window, self.core.calls.accept(call, video), error=_("Could not answer"))

    def _accept_video(self, call) -> None:
        self._accept(call, True)

    def _decline(self, call) -> None:
        self._ring(False)
        widgets.run(self.app.window, self.core.calls.decline(call))


class CallWindow(Adw.Window):
    """One call: who, its state or length, the video both ways, and the buttons for the moment."""

    def __init__(self, ui: CallUI, call):
        super().__init__(title=_("Call"), default_width=720 if call.video else 380,
                         default_height=540 if call.video else 520, css_classes=["call-window"])
        self.ui = ui
        self.core = ui.core
        self.call = call
        self._timer = 0
        name = self.core.names.contact_name(call.account_id, call.peer) or call.peer
        self.set_title(name)
        contact = self.core.roster.contacts.get(call.account_id, call.peer)
        avatar_path = contact.avatar if contact is not None else ""

        overlay = Gtk.Overlay()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True, hexpand=True)
        who = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
        who.append(widgets.Avatar(128, name, avatar_path))
        who.get_first_child().set_halign(Gtk.Align.CENTER)
        who.append(Gtk.Label(label=name, css_classes=["title-1"], wrap=True, justify=Gtk.Justification.CENTER))
        self.status = Gtk.Label(css_classes=["dim-label"])
        who.append(self.status)
        self.stack.add_named(who, "who")
        self.remote = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, css_classes=["call-remote"])
        self.stack.add_named(self.remote, "video")
        overlay.set_child(self.stack)

        # our camera, small in a corner: a picture's natural size is the video's, so a clamp keeps it small
        self.local = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, css_classes=["call-local"], can_shrink=True)
        self.local_box = Adw.Clamp(maximum_size=180, tightening_threshold=180, child=self.local, visible=False,
                                   halign=Gtk.Align.END, valign=Gtk.Align.START, margin_top=12, margin_end=12)
        overlay.add_overlay(self.local_box)
        self.video_status = Gtk.Label(css_classes=["osd", "call-video-status"], halign=Gtk.Align.START,
                                      valign=Gtk.Align.START, margin_top=12, margin_start=12, visible=False)
        overlay.add_overlay(self.video_status)

        self.buttons = Gtk.Box(spacing=18, halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=24)
        overlay.add_overlay(self.buttons)
        self.set_content(overlay)

        self.connect("close-request", self._on_close)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self._handlers = [call.connect("notify::state", lambda *_: self._update()),
                          call.connect("media", lambda *_: self._media())]
        self._update()

    # ------------------------------------------------------------------ state
    def _button(self, icon: str, tip: str, css: str, cb, toggle: bool = False) -> Gtk.Widget:
        cls = Gtk.ToggleButton if toggle else Gtk.Button
        b = cls(icon_name=icon, tooltip_text=tip, css_classes=["circular", "call-button", css] if css else
                ["circular", "call-button"])
        b.connect("toggled" if toggle else "clicked", cb)
        self.buttons.append(b)
        return b

    def _update(self) -> None:
        call = self.call
        widgets.clear(self.buttons)
        state = call.state
        if state == "incoming":
            self._button("call-stop-symbolic", _("Decline"), "destructive-action", lambda *_: self.ui._decline(call))
            if "video" in call.media:
                self._button("camera-video-symbolic", _("Answer with video"), "suggested-action",
                             lambda *_: self.ui._accept(call, True))
            self._button("call-start-symbolic", _("Answer"), "success", lambda *_: self.ui._accept(call, False))
        elif state != "ended":
            self.mute = self._button("audio-input-microphone-symbolic", _("Mute"), "", self._toggle_mute, toggle=True)
            if call.video:
                self.camera = self._button("camera-video-symbolic", _("Turn off camera"), "", self._toggle_camera,
                                           toggle=True)
            self._button("call-stop-symbolic", _("Hang up"), "destructive-action",
                         lambda *_: widgets.run(self, self.core.calls.hang_up(call)))
        if state == "active" and not self._timer:
            self._timer = GLib.timeout_add_seconds(1, self._tick)
        self._show_media()
        self._tick()

    def _status_text(self) -> str:
        call = self.call
        return {"proposing": _("Calling…"), "ringing": _("Ringing…"), "connecting": _("Connecting…"),
                "incoming": _("Incoming video call") if "video" in call.media else _("Incoming call"),
                }.get(call.state) or (self._length() if call.state == "active" else "")

    def _length(self) -> str:
        d = self.call.duration
        return "%d:%02d:%02d" % (d // 3600, d // 60 % 60, d % 60) if d >= 3600 else "%d:%02d" % (d // 60, d % 60)

    def _tick(self) -> bool:
        text = self._status_text()
        self.status.set_text(text)
        self.video_status.set_text(text)
        return self.call.state == "active"

    # ------------------------------------------------------------------ video
    def _media(self) -> None:
        ms = self.call.ms
        if ms is not None:
            ms.connect("remote-video", lambda _m, paintable: self._show_media())

    def _show_media(self) -> None:
        ms = self.call.ms
        remote = getattr(ms, "remote_paintable", None) if ms is not None else None
        local = getattr(ms, "local_paintable", None) if ms is not None else None
        if remote is not None and self.remote.get_paintable() is not remote:
            self.remote.set_paintable(remote)
        if local is not None and self.local.get_paintable() is not local:
            self.local.set_paintable(local)
        video = remote is not None and self.call.state == "active"
        self.stack.set_visible_child_name("video" if video else "who")
        self.video_status.set_visible(video)
        self.local_box.set_visible(local is not None and self.call.camera and self.call.state != "ended")

    def _toggle_mute(self, button) -> None:
        muted = button.get_active()
        self.core.calls.set_muted(self.call, muted)
        button.set_icon_name("microphone-disabled-symbolic" if muted else "audio-input-microphone-symbolic")
        button.set_tooltip_text(_("Unmute") if muted else _("Mute"))

    def _toggle_camera(self, button) -> None:
        off = button.get_active()
        self.core.calls.set_camera(self.call, not off)
        button.set_icon_name("camera-disabled-symbolic" if off else "camera-video-symbolic")
        button.set_tooltip_text(_("Turn on camera") if off else _("Turn off camera"))
        self._show_media()

    def _on_key(self, _c, keyval, _code, _state) -> bool:
        from gi.repository import Gdk

        if keyval in (Gdk.KEY_m, Gdk.KEY_M) and getattr(self, "mute", None) is not None:
            self.mute.set_active(not self.mute.get_active())
            return True
        return False

    # ------------------------------------------------------------------ ending
    def _on_close(self, *_a) -> bool:
        if self.call.active:
            widgets.run(self, self.core.calls.hang_up(self.call))
        self._cleanup()
        return False

    def ended(self) -> None:
        """The call ended: say how, then go."""
        self.status.set_text(self.core.calls.summary({"call": {"end": self.call.end, "media": sorted(
            self.call.media), "duration": self.call.duration, "outgoing": self.call.outgoing}}))
        self.stack.set_visible_child_name("who")
        self.local_box.set_visible(False)
        self.video_status.set_visible(False)
        widgets.clear(self.buttons)
        GLib.timeout_add(1500, lambda: self.close() and False)

    def _cleanup(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        for h in self._handlers:
            self.call.disconnect(h)
        self._handlers = []
        self.remote.set_paintable(None)
        self.local.set_paintable(None)
