"""Small building blocks shared by the windows: async helpers, avatars, time and presence formatting."""

from __future__ import annotations

import datetime as _dt
import logging
import os
from collections import OrderedDict
from collections.abc import Callable, Coroutine

from gi.repository import Adw, Gdk, GLib, Gtk

from .. import aio, util
from ..core import colors
from ..i18n import _
from . import compat
from .lifetime import release, setup_row, teardown_row  # noqa: F401 - used through widgets

log = logging.getLogger("ward.gui")


# ------------------------------------------------------------------------------------------------ async
def run(widget: Gtk.Widget | None, coro: Coroutine, done: Callable | None = None, error: str = "",
        on_error: Callable | None = None, loads: bool = False):
    """Run a core coroutine from a GTK callback; errors become a toast on the widget's window.  loads=True: it only
    fills the widget, so it stops when the widget leaves its window (a closed dialog is not kept waiting on the
    network)."""
    async def wrapper():
        try:
            res = await coro
        except Exception as e:
            log.info("action failed: %s", e, exc_info=log.isEnabledFor(logging.DEBUG))
            if on_error is not None:
                on_error(e)
            elif widget is not None:
                compat.toast(widget, "%s%s" % ((error + ": ") if error else "", e))
            return None
        if done is not None:
            done(res)
        return res
    task = aio.spawn(wrapper())
    if loads and widget is not None:
        from . import lifetime

        lifetime.when_unrooted(widget, lambda: task.done() or task.cancel())
    return task


# ------------------------------------------------------------------------------------------------ avatars
_textures: OrderedDict[tuple[str, int], Gdk.Texture] = OrderedDict()
AVATAR_PIXELS = 192        # the largest avatar shown is 96 px: twice that for high-resolution screens
IMAGE_PIXELS = 640         # images in the chat are shown at most 320 px wide


def texture(path: str, max_side: int = AVATAR_PIXELS) -> Gdk.Texture | None:
    """An image as a texture, decoded at no more than max_side pixels a side (0: full size, not cached).  A photo
    used as an avatar decoded at full size takes tens of MB, in memory and again on the graphics card."""
    if not path or not os.path.exists(path):
        return None
    if not max_side:
        try:
            return Gdk.Texture.new_from_filename(path)
        except GLib.Error:
            pass
        try:
            from gi.repository import GdkPixbuf

            return Gdk.Texture.new_for_pixbuf(GdkPixbuf.Pixbuf.new_from_file(path))
        except (GLib.Error, ImportError, ValueError):
            pass
        try:
            # Last resort for formats GTK/GdkPixbuf does not load (for example JPEG XL when Pillow has a plugin).
            from PIL import Image, ImageOps

            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im).convert("RGBA")
                data = im.tobytes()
                return Gdk.MemoryTexture.new(im.width, im.height, Gdk.MemoryFormat.R8G8B8A8,
                                             GLib.Bytes.new(data), im.width * 4)
        except (ImportError, OSError, ValueError):
            return None
    key = (path, max_side)
    t = _textures.get(key)
    if t is not None:
        _textures.move_to_end(key)
        return t
    try:
        from gi.repository import GdkPixbuf

        info = GdkPixbuf.Pixbuf.get_file_info(path)
        w, h = (info[1], info[2]) if info and info[0] is not None else (0, 0)
        if w > max_side or h > max_side:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, max_side, max_side, True)
            t = Gdk.Texture.new_for_pixbuf(pb)
        else:
            t = Gdk.Texture.new_from_filename(path)
    except (GLib.Error, ImportError, ValueError):
        try:
            t = Gdk.Texture.new_from_filename(path)
        except GLib.Error:
            return None
    _textures[key] = t
    if len(_textures) > 200:
        _textures.popitem(last=False)
    return t


def emoji_button(button: Gtk.MenuButton, settings, picked, closed=None) -> None:
    """Give the menu button an emoji picker, built when it opens and dropped when it closes (see emoji_picker for why
    not Gtk.EmojiChooser, which is only the fallback)."""
    # the callbacks live on the button, where the garbage collector sees them (it cannot see into the popup func:
    # a closure there referring back to the row would keep the row alive for good)
    button._ward_emoji = (picked, closed, settings)
    button.set_create_popup_func(_create_emoji_chooser)


def _create_emoji_chooser(button: Gtk.MenuButton, *_a) -> None:
    if button.get_popover() is not None:
        return
    from . import emoji_picker

    if emoji_picker.available():
        chooser: Gtk.Popover = emoji_picker.EmojiPicker(button._ward_emoji[2])
    else:
        chooser = Gtk.EmojiChooser()
    chooser.connect("emoji-picked", lambda _c, e: button._ward_emoji[0](e))

    def on_closed(*_a) -> None:
        closed = button._ward_emoji[1]
        if closed is not None:
            closed()
        GLib.idle_add(lambda: button.get_popover() is chooser and not button.get_active()
                      and button.set_popover(None))
    chooser.connect("closed", on_closed)
    button.set_popover(chooser)


class Avatar(Gtk.Overlay):
    """Adw.Avatar with an image (when we have one), initials otherwise, and an optional presence dot."""

    def __init__(self, size: int = 40, text: str = "", path: str = "", show: str | None = None):
        super().__init__()
        self.avatar = Adw.Avatar(size=size, show_initials=True)
        self.set_child(self.avatar)
        self.dot = Gtk.Box(css_classes=["presence-dot"], halign=Gtk.Align.END, valign=Gtk.Align.END,
                           visible=False)
        self.add_overlay(self.dot)
        self.set_valign(Gtk.Align.START)
        self.update(text, path, show)

    def update(self, text: str = "", path: str = "", show: str | None = None) -> None:
        self.avatar.set_text(text or "?")
        self.avatar.set_custom_image(texture(path))
        if show is None:
            self.dot.set_visible(False)
        else:
            for c in ("available", "chat", "away", "xa", "dnd", "offline"):
                self.dot.remove_css_class(c)
            self.dot.add_css_class(show)
            self.dot.set_visible(show != "offline")

    def set_size(self, size: int) -> None:
        self.avatar.set_size(size)


class RowAvatar(Avatar):
    """An avatar for a list row's prefix, centred on the row's height."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_valign(Gtk.Align.CENTER)


# ------------------------------------------------------------------------------------------------ time
def _dt_local(ms: int) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(ms / 1000)


def clock(ms: int, settings=None, seconds: bool | None = None) -> str:
    d = _dt_local(ms)
    fmt = settings["appearance.time_format"] if settings is not None else "system"
    secs = settings["appearance.time_seconds"] if seconds is None and settings is not None else bool(seconds)
    if fmt == "system":
        try:
            gs = Gtk.Settings.get_default()
            fmt = "24"
            if gs is not None:
                from gi.repository import Gio

                schema = Gio.SettingsSchemaSource.get_default()
                if schema is not None and schema.lookup("org.gnome.desktop.interface", True) is not None:
                    fmt = "12" if Gio.Settings.new("org.gnome.desktop.interface").get_string("clock-format") == \
                        "12h" else "24"
        except Exception:
            fmt = "24"
    if fmt == "12":
        return d.strftime("%I:%M:%S %p" if secs else "%I:%M %p").lstrip("0")
    return d.strftime("%H:%M:%S" if secs else "%H:%M")


def day_label(ms: int) -> str:
    d = _dt_local(ms).date()
    today = _dt.date.today()
    if d == today:
        return _("Today")
    if d == today - _dt.timedelta(days=1):
        return _("Yesterday")
    if (today - d).days < 7:
        return d.strftime("%A")
    if d.year == today.year:
        return d.strftime("%B %-d") if os.name != "nt" else d.strftime("%B %d")
    return d.strftime("%B %-d, %Y") if os.name != "nt" else d.strftime("%B %d, %Y")


def short_time(ms: int, settings=None) -> str:
    """For the chat list: a time today, a weekday this week, a date before."""
    if not ms:
        return ""
    d = _dt_local(ms)
    today = _dt.date.today()
    if d.date() == today:
        return clock(ms, settings, seconds=False)
    if (today - d.date()).days < 7:
        return d.strftime("%a")
    return d.strftime("%x")


def same_day(a: int, b: int) -> bool:
    return _dt_local(a).date() == _dt_local(b).date()


def relative_seen(ms: int) -> str:
    if not ms:
        return ""
    delta = (_dt.datetime.now() - _dt_local(ms)).total_seconds()
    if delta < 120:
        return _("just now")
    if delta < 3600:
        return _("%d minutes ago") % (delta // 60)
    if delta < 86400:
        return _("%d hours ago") % (delta // 3600)
    return _dt_local(ms).strftime("%x")


# ------------------------------------------------------------------------------------------------ presence
SHOW_TEXT = {"available": "Available", "chat": "Available", "away": "Away", "xa": "Away for a while",
             "dnd": "Busy", "offline": "Offline"}


def presence_text(show: str, status: str = "") -> str:
    base = _(SHOW_TEXT.get(show, "Available"))
    return "%s — %s" % (base, status) if status else base


# ------------------------------------------------------------------------------------------------ trust
TRUST = {
    "verified": ("ward-shield-verified-symbolic", "trust-verified", "Verified"),
    "trusted": ("ward-shield-symbolic", "trust-trusted", "Trusted"),
    "undecided": ("ward-shield-warning-symbolic", "trust-undecided", "New device"),
    "distrusted": ("ward-shield-off-symbolic", "trust-distrusted", "Distrusted"),
    "inactive": ("ward-shield-symbolic", "trust-inactive", "Inactive"),
}


def trust_badge(trust: str) -> Gtk.Widget:
    icon, css, label = TRUST.get(trust, TRUST["undecided"])
    box = Gtk.Box(spacing=4, css_classes=[css])
    box.append(Gtk.Image.new_from_icon_name(icon))
    box.append(Gtk.Label(label=_(label)))
    box.set_tooltip_text(_(label))
    return box


def fingerprint_widget(fp: str, size: int = 8) -> Gtk.Widget:
    """Groups of 8 hex characters, each in its XEP-0392 colour, as Conversations and Gajim show them."""
    from . import style

    flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4, min_children_per_line=2,
                       column_spacing=8, row_spacing=2, homogeneous=True)
    dark = style.is_dark()
    for g in [fp[i:i + size] for i in range(0, len(fp), size)]:
        lbl = Gtk.Label(css_classes=["fingerprint-group"], selectable=True)
        lbl.set_markup("<span foreground='%s'>%s</span>" % (colors.hex_color(g, dark), GLib.markup_escape_text(g)))
        flow.append(lbl)
    return flow


# ------------------------------------------------------------------------------------------------ misc
def escape(text: str) -> str:
    return GLib.markup_escape_text(text or "")


def label(text: str = "", css: list[str] | None = None, xalign: float = 0.0, wrap: bool = False,
          ellipsize: bool = False, selectable: bool = False, markup: bool = False) -> Gtk.Label:
    lbl = Gtk.Label(xalign=xalign, css_classes=css or [], selectable=selectable)
    if wrap:
        lbl.set_wrap(True)
        from gi.repository import Pango

        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if ellipsize:
        from gi.repository import Pango

        lbl.set_ellipsize(Pango.EllipsizeMode.END)
    if markup:
        lbl.set_markup(text)
    else:
        lbl.set_text(text)
    return lbl


def clear(box: Gtk.Widget) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def copy_text(widget: Gtk.Widget, text: str, toast: str | None = None) -> None:
    widget.get_clipboard().set(text)
    compat.toast(widget, toast or _("Copied"))


def open_uri(widget: Gtk.Widget, uri: str) -> None:
    root = widget.get_root() if widget is not None else None
    try:
        launcher = Gtk.UriLauncher.new(uri)            # GTK 4.10
        launcher.launch(root if isinstance(root, Gtk.Window) else None, None, None, None)
    except AttributeError:
        Gtk.show_uri(root if isinstance(root, Gtk.Window) else None, uri, Gdk.CURRENT_TIME)


def open_file(widget: Gtk.Widget, path: str) -> None:
    from gi.repository import Gio

    root = widget.get_root() if widget is not None else None
    try:
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(path))
        launcher.launch(root if isinstance(root, Gtk.Window) else None, None, None, None)
    except AttributeError:
        open_uri(widget, Gio.File.new_for_path(path).get_uri())


def write_private(path: str, data: bytes) -> None:
    """Write a key backup so only the user can read it (0600), whatever the umask is."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def initials_name(name: str) -> str:
    return name or "?"


def human(n: int) -> str:
    return util.human_size(n)
