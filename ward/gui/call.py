"""The call windows: an incoming-call prompt and the active-call controls."""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from ..i18n import _
from . import widgets


def _state_text(call) -> str:
    if call.state == "incoming":
        return _("Incoming call")
    if call.state == "ringing":
        return _("Ringing…")
    if call.state == "connecting":
        return _("Connecting…")
    if call.state == "active":
        return _("Connected")
    if call.error:
        return call.error
    return _("Call ended")


class IncomingCallWindow(Adw.Window):
    """The ringing prompt; it closes by itself if the caller retracts or the timeout expires."""

    def __init__(self, app, call):
        super().__init__(application=app, title=_("Incoming call"), modal=True)
        self.call = call
        self.calls = app.core.calls
        self.set_default_size(380, 300)
        self.set_resizable(False)
        self._accepting = False
        self._handler = call.connect("notify::state", lambda *_: self._changed())
        self.connect("close-request", self._closed)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar(show_title=False))
        self.set_content(view)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin_top=18, margin_bottom=18,
                      margin_start=24, margin_end=24, valign=Gtk.Align.CENTER)
        box.append(widgets.Avatar(80, call.name))
        name = Gtk.Label(label=widgets.escape(call.name), css_classes=["title-1"], wrap=True)
        box.append(name)
        kind = _("Video call") if call.media == "video" else _("Audio call")
        box.append(Gtk.Label(label=kind, css_classes=["dim-label"]))
        actions = Gtk.Box(spacing=12, homogeneous=True, margin_top=10)
        decline = Gtk.Button(label=_("Decline"), css_classes=["destructive-action", "pill"])
        accept = Gtk.Button(label=_("Accept"), css_classes=["suggested-action", "pill"])
        decline.connect("clicked", lambda *_: widgets.run(self, self.calls.reject(call)))
        accept.connect("clicked", lambda *_: self._accept())
        actions.append(decline)
        actions.append(accept)
        box.append(actions)
        view.set_content(box)

    def _accept(self) -> None:
        self._accepting = True
        self.destroy()
        widgets.run(self, self.calls.accept(self.call))

    def _changed(self) -> None:
        if self.call.state == "ended":
            self.destroy()

    def _closed(self, *_a) -> bool:
        try:
            self.call.disconnect(self._handler)
        except TypeError:
            pass
        if self.call.state == "incoming" and not self._accepting:
            widgets.run(self, self.calls.reject(self.call))
        return False


class CallWindow(Adw.Window):
    def __init__(self, app, call):
        super().__init__(application=app, title=call.name or _("Call"))
        self.call = call
        self.core = app.core
        self.calls = app.core.calls
        self.set_default_size(520, 600)
        self._handlers = [
            call.connect("notify::state", lambda *_: self._update()),
            call.connect("notify::muted", lambda *_: self._update()),
            call.connect("notify::video", lambda *_: self._update()),
            call.connect("notify::screen", lambda *_: self._update()),
            call.connect("notify::paintable", lambda *_: self._update()),
        ]
        self.connect("close-request", self._closed)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self.set_content(toolbar)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=18, margin_end=18,
                          margin_bottom=18)
        toolbar.set_content(content)
        self.stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.avatar = Adw.StatusPage(icon_name="avatar-default-symbolic")
        self.picture = Gtk.Picture(can_shrink=True, hexpand=True, vexpand=True)
        self.stack.add_named(self.avatar, "avatar")
        self.stack.add_named(self.picture, "video")
        content.append(self.stack)
        self.name = Gtk.Label(label=widgets.escape(call.name), css_classes=["title-1"], wrap=True)
        content.append(self.name)
        self.status = Gtk.Label(css_classes=["dim-label"])
        content.append(self.status)
        controls = Gtk.Box(spacing=8, homogeneous=True, margin_top=6)
        self.mute = Gtk.ToggleButton(icon_name="audio-input-microphone-symbolic", active=call.muted,
                                     tooltip_text=_("Mute"))
        self.mute.connect("toggled", lambda b: self.calls.toggle_mute(call) if b.get_active() != call.muted else None)
        self.video = Gtk.ToggleButton(icon_name="camera-video-symbolic", active=call.video and call.media == "video",
                                      tooltip_text=_("Camera"), visible=call.media == "video")
        self.video.connect("toggled", lambda b: self.calls.toggle_video(call) if b.get_active() != call.video else None)
        self.screen = Gtk.ToggleButton(icon_name="video-display-symbolic", active=call.screen,
                                       tooltip_text=_("Share screen"), visible=call.media == "video")
        self.screen.connect("toggled", lambda b: self.calls.toggle_screen(call)
                            if b.get_active() != call.screen else None)
        hang = Gtk.Button(icon_name="call-stop-symbolic", css_classes=["destructive-action", "circular"],
                          tooltip_text=_("Hang up"))
        hang.connect("clicked", lambda *_: widgets.run(self, self.calls.hangup(call)))
        for button in (self.mute, self.video, self.screen, hang):
            controls.append(button)
        content.append(controls)
        self._update()

    def _update(self) -> None:
        call = self.call
        self.status.set_text(_state_text(call))
        self.mute.set_active(call.muted)
        self.video.set_active(call.video and call.media == "video")
        self.screen.set_active(call.screen)
        if call.paintable is not None and call.media == "video":
            self.picture.set_paintable(call.paintable)
            self.stack.set_visible_child_name("video")
        else:
            self.stack.set_visible_child_name("avatar")
        if call.state == "ended":
            GLib.timeout_add(1500, self._close_soon)

    def _close_soon(self) -> bool:
        self.destroy()
        return False

    def _closed(self, *_a) -> bool:
        for h in self._handlers:
            try:
                self.call.disconnect(h)
            except TypeError:
                pass
        if self.call.state in ("incoming", "ringing", "connecting", "active"):
            widgets.run(self, self.calls.hangup(self.call))
        return False
