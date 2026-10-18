"""Audio and video attachments in a message, and a fullscreen view for videos.

The fullscreen view shows the same media stream as the message, so the video carries on where it was, and the
message's player is where it left off when fullscreen ends."""

from __future__ import annotations

from gi.repository import Gdk, Gtk

from ..i18n import _


def attachment(path: str, is_video: bool) -> Gtk.Widget:
    """The player for an attachment: a video with a fullscreen button (and double click), or an audio bar."""
    vid = Gtk.Video.new_for_filename(path)
    vid.set_size_request(320 if is_video else 280, 220 if is_video else -1)
    if not is_video:
        vid.set_halign(Gtk.Align.START)
        return vid
    overlay = Gtk.Overlay(halign=Gtk.Align.START)
    overlay.set_child(vid)
    full = Gtk.Button(icon_name="view-fullscreen-symbolic", css_classes=["osd", "circular"],
                      tooltip_text=_("Fullscreen"), halign=Gtk.Align.END, valign=Gtk.Align.START,
                      margin_top=6, margin_end=6)
    full.connect("clicked", lambda *_: fullscreen(vid))
    overlay.add_overlay(full)
    clicks = Gtk.GestureClick()
    clicks.connect("pressed", lambda _g, n, _x, _y: n == 2 and fullscreen(vid))
    vid.add_controller(clicks)
    return overlay


def fullscreen(vid: Gtk.Video) -> None:
    stream = vid.get_media_stream()
    if stream is None:
        return
    root = vid.get_root()
    win = Gtk.Window(title=_("Video"), css_classes=["video-fullscreen"],
                     transient_for=root if isinstance(root, Gtk.Window) else None)
    big = Gtk.Video(media_stream=stream, vexpand=True, hexpand=True)
    overlay = Gtk.Overlay()
    overlay.set_child(big)
    leave = Gtk.Button(icon_name="view-restore-symbolic", css_classes=["osd", "circular"],
                       tooltip_text=_("Leave fullscreen"), halign=Gtk.Align.END, valign=Gtk.Align.START,
                       margin_top=12, margin_end=12)
    leave.connect("clicked", lambda *_: win.close())
    overlay.add_overlay(leave)
    win.set_child(overlay)

    def on_key(_c, keyval, _code, _state) -> bool:
        if keyval in (Gdk.KEY_Escape, Gdk.KEY_f, Gdk.KEY_F, Gdk.KEY_F11):
            win.close()
            return True
        if keyval == Gdk.KEY_space:
            stream.set_playing(not stream.get_playing())
            return True
        return False
    keys = Gtk.EventControllerKey()
    keys.connect("key-pressed", on_key)
    win.add_controller(keys)
    clicks = Gtk.GestureClick()
    clicks.connect("pressed", lambda _g, n, _x, _y: n == 2 and win.close())
    big.add_controller(clicks)
    # the stream stays the message's: the fullscreen player lets go of it when it closes
    win.connect("close-request", lambda *_: big.set_media_stream(None) or False)
    win.fullscreen()
    win.present()
    if not stream.get_playing() and not stream.get_ended():
        stream.play()
