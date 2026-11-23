"""Small building blocks shared by the windows: async helpers, avatars, time and presence formatting."""

from __future__ import annotations

import datetime as _dt
import logging
import os
import stat
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable, Coroutine

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk

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
            elif widget is not None and widget.get_root() is not None:
                compat.toast(widget, "%s%s" % ((error + ": ") if error else "", e))
            return None
        if done is not None:
            done(res)
        return res
    task = aio.spawn(wrapper())
    if loads and widget is not None:
        from . import lifetime

        task_ref = weakref.ref(task)
        w_ref = weakref.ref(widget)
        hid = lifetime.when_unrooted(widget, lambda: (t := task_ref()) is not None and (t.done() or t.cancel()))

        def detach(_t) -> None:
            w = w_ref()
            if w is not None and GObject.signal_handler_is_connected(w, hid):
                w.disconnect(hid)

        task.add_done_callback(detach)
    return task


# ------------------------------------------------------------------------------------------------ avatars
_textures: OrderedDict[tuple[str, int], Gdk.Texture] = OrderedDict()
_texture_bytes: int = 0
# key -> (monotonic_ts, mtime_ns, file_size); mtime_ns is None when the file was absent
_negative_textures: OrderedDict[tuple[str, int], tuple[float, int | None, int | None]] = OrderedDict()
_pending_textures: dict[tuple[str, int], list[Callable[[Gdk.Texture | None], None]]] = {}

AVATAR_PIXELS = 192        # the largest avatar shown is 96 px: twice that for high-resolution screens
IMAGE_PIXELS = 640         # images in the chat are shown at most 320 px wide
MAX_TEXTURE_COUNT = 200
MAX_TEXTURE_BYTES = 48 * 1024 * 1024
NEGATIVE_TTL = 2.0
MAX_NEGATIVE_COUNT = 512
MAX_RAW_IMAGE_BYTES = 64 * 1024 * 1024


def _texture_size(t: Gdk.Texture) -> int:
    return max(0, int(t.get_width()) * int(t.get_height()) * 4)


def _pixbuf_to_texture(pb) -> Gdk.Texture:
    fmt = Gdk.MemoryFormat.R8G8B8A8 if pb.get_has_alpha() else Gdk.MemoryFormat.R8G8B8
    return Gdk.MemoryTexture.new(pb.get_width(), pb.get_height(), fmt, pb.read_pixel_bytes(), pb.get_rowstride())


def _stat_regular(path: str) -> os.stat_result | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st if stat.S_ISREG(st.st_mode) else None


def _cache_get(key: tuple[str, int]) -> Gdk.Texture | None:
    t = _textures.get(key)
    if t is not None:
        _textures.move_to_end(key)
    return t


def _cache_put(key: tuple[str, int], t: Gdk.Texture) -> None:
    global _texture_bytes
    _negative_textures.pop(key, None)
    size = _texture_size(t)
    if not _textures:
        _texture_bytes = 0
    old = _textures.pop(key, None)
    if old is not None:
        _texture_bytes = max(0, _texture_bytes - _texture_size(old))
    if size > MAX_TEXTURE_BYTES:
        return
    _textures[key] = t
    _texture_bytes += size
    while _textures and (len(_textures) > MAX_TEXTURE_COUNT or _texture_bytes > MAX_TEXTURE_BYTES):
        _k, evicted = _textures.popitem(last=False)
        _texture_bytes = max(0, _texture_bytes - _texture_size(evicted))


def _is_negative(key: tuple[str, int]) -> bool:
    entry = _negative_textures.get(key)
    if entry is None:
        return False
    ts, mtime_ns, size = entry
    now = time.monotonic()
    if now - ts < NEGATIVE_TTL:
        _negative_textures.move_to_end(key)
        return True
    if mtime_ns is None:
        _negative_textures.pop(key, None)
        return False
    st = _stat_regular(key[0])
    if st is not None and st.st_mtime_ns == mtime_ns and st.st_size == size:
        _negative_textures[key] = (now, mtime_ns, size)
        _negative_textures.move_to_end(key)
        return True
    _negative_textures.pop(key, None)
    return False


def _record_negative(key: tuple[str, int], mtime_ns: int | None = None, size: int | None = None) -> None:
    _negative_textures[key] = (time.monotonic(), mtime_ns, size)
    _negative_textures.move_to_end(key)
    while len(_negative_textures) > MAX_NEGATIVE_COUNT:
        _negative_textures.popitem(last=False)


def cached_texture(path: str, max_side: int = AVATAR_PIXELS) -> Gdk.Texture | None:
    """Return a cached texture without touching the filesystem, or None if not cached."""
    if not path or not max_side:
        return None
    return _cache_get((path, max_side))


def invalidate_texture(path: str) -> None:
    """Drop cached and negative-cached texture entries for a file path."""
    global _texture_bytes
    if not path:
        return
    for key in [k for k in _textures if k[0] == path]:
        evicted = _textures.pop(key)
        _texture_bytes = max(0, _texture_bytes - _texture_size(evicted))
    for key in [k for k in _negative_textures if k[0] == path]:
        _negative_textures.pop(key, None)


def clear_texture_cache() -> None:
    """Release all cached decoded textures and negative lookup entries."""
    global _texture_bytes
    _textures.clear()
    _negative_textures.clear()
    _texture_bytes = 0


def _decode_pil_rgba(path: str, max_side: int) -> tuple[int, int, bytes] | None:
    """Thread-safe image decode via Pillow (never touches GTK/GDK objects)."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(path) as im:
            if max_side > 0:
                try:
                    im.draft(None, (max_side * 2, max_side * 2))
                except Exception:
                    pass
            im = ImageOps.exif_transpose(im)
            if max_side > 0 and (im.width > max_side or im.height > max_side):
                im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            if im.width <= 0 or im.height <= 0:
                return None
            if im.mode != "RGBA":
                im = im.convert("RGBA")
            return im.width, im.height, im.tobytes()
    except Exception:
        return None


def _decode_bytes_on_main(blob: bytes, max_side: int) -> Gdk.Texture | None:
    """Decode raw file bytes into a Gdk.Texture on the GTK main thread."""
    gbytes = GLib.Bytes.new(blob)
    if not max_side:
        try:
            return Gdk.Texture.new_from_bytes(gbytes)
        except GLib.Error:
            pass
        try:
            from gi.repository import GdkPixbuf, Gio

            stream = Gio.MemoryInputStream.new_from_bytes(gbytes)
            pb = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
            if pb is not None:
                pb = pb.apply_embedded_orientation() or pb
                return _pixbuf_to_texture(pb)
        except (GLib.Error, ImportError, ValueError):
            pass
        return None
    try:
        from gi.repository import GdkPixbuf, Gio

        stream = Gio.MemoryInputStream.new_from_bytes(gbytes)
        pb = GdkPixbuf.Pixbuf.new_from_stream_at_scale(stream, max_side, max_side, True, None)
        if pb is not None:
            pb = pb.apply_embedded_orientation() or pb
            return _pixbuf_to_texture(pb)
    except (GLib.Error, ImportError, ValueError):
        pass
    try:
        t = Gdk.Texture.new_from_bytes(gbytes)
        if t.get_width() <= max_side and t.get_height() <= max_side:
            return t
    except GLib.Error:
        pass
    return None


def _decode_worker(path: str, max_side: int) -> tuple[str, tuple]:
    """Read and decode image data in a background worker thread without touching GTK/GDK."""
    st = _stat_regular(path)
    if st is None:
        return ("missing", ())
    rgba = _decode_pil_rgba(path, max_side)
    if rgba is not None:
        w, h, data = rgba
        return ("rgba", (w, h, data, st.st_mtime_ns, st.st_size))
    if st.st_size > MAX_RAW_IMAGE_BYTES:
        return ("failed", (st.st_mtime_ns, st.st_size))
    try:
        with open(path, "rb") as fh:
            blob = fh.read(MAX_RAW_IMAGE_BYTES + 1)
        if len(blob) > MAX_RAW_IMAGE_BYTES:
            return ("failed", (st.st_mtime_ns, st.st_size))
        return ("bytes", (blob, st.st_mtime_ns, st.st_size))
    except OSError:
        return ("missing", ())


def texture_async(path: str, max_side: int = AVATAR_PIXELS,
                  callback: Callable[[Gdk.Texture | None], None] | None = None) -> Gdk.Texture | None:
    """Return a cached texture immediately if available, or decode it off the main thread and call callback on the
    GTK main thread via GLib.idle_add.  Multiple concurrent requests for the same (path, max_side) share one job."""
    if not path:
        if callback is not None:
            callback(None)
        return None
    key = (path, max_side)
    if max_side:
        t = _cache_get(key)
        if t is not None:
            if callback is not None:
                callback(t)
            return t
    if _is_negative(key):
        if callback is not None:
            callback(None)
        return None

    waiters = _pending_textures.get(key)
    if waiters is not None:
        if callback is not None:
            waiters.append(callback)
        return None
    _pending_textures[key] = [callback] if callback is not None else []

    def run_job() -> None:
        try:
            kind, payload = _decode_worker(path, max_side)
        except Exception:
            kind, payload = ("missing", ())

        def finish() -> bool:
            cbs = _pending_textures.pop(key, [])
            tex: Gdk.Texture | None = None
            if kind == "missing":
                _record_negative(key, None, None)
            elif kind == "failed":
                mtime_ns, size = payload
                _record_negative(key, mtime_ns, size)
            elif kind == "rgba":
                w, h, data, mtime_ns, size = payload
                try:
                    tex = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(data), w * 4)
                except GLib.Error:
                    tex = None
                if tex is not None:
                    if max_side:
                        _cache_put(key, tex)
                else:
                    _record_negative(key, mtime_ns, size)
            elif kind == "bytes":
                blob, mtime_ns, size = payload
                tex = _decode_bytes_on_main(blob, max_side)
                if tex is not None:
                    if max_side:
                        _cache_put(key, tex)
                else:
                    _record_negative(key, mtime_ns, size)
            for cb in cbs:
                try:
                    cb(tex)
                except Exception:
                    log.exception("texture_async callback failed")
            return False

        GLib.idle_add(finish)

    try:
        aio.workers().submit(run_job)
    except Exception:
        _pending_textures.pop(key, None)
        if callback is not None:
            callback(None)
    return None


def texture(path: str, max_side: int = AVATAR_PIXELS) -> Gdk.Texture | None:
    """An image as a texture, decoded at no more than max_side pixels a side (0: full size, not cached).  A photo
    used as an avatar decoded at full size takes tens of MB, in memory and again on the graphics card."""
    if not path:
        return None
    # Cache first: a cached avatar is looked up for every row that is built, and it used to stat the file
    # (or three) before finding it in the cache.
    key = (path, max_side)
    if max_side:
        t = _cache_get(key)
        if t is not None:
            return t
    if _is_negative(key):
        return None
    st = _stat_regular(path)
    if st is None:
        _record_negative(key, None, None)
        return None
    if not max_side:
        try:
            return Gdk.Texture.new_from_filename(path)
        except GLib.Error:
            pass
        try:
            from gi.repository import GdkPixbuf

            pb = GdkPixbuf.Pixbuf.new_from_file(path)
            if pb is not None:
                pb = pb.apply_embedded_orientation() or pb
                return _pixbuf_to_texture(pb)
        except (GLib.Error, ImportError, ValueError):
            pass
        rgba = _decode_pil_rgba(path, 0)
        if rgba is not None:
            w, h, data = rgba
            return Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(data), w * 4)
        _record_negative(key, st.st_mtime_ns, st.st_size)
        return None
    t: Gdk.Texture | None = None
    try:
        from gi.repository import GdkPixbuf

        info = GdkPixbuf.Pixbuf.get_file_info(path)
        w, h = (info[1], info[2]) if info and info[0] is not None else (0, 0)
        if w > max_side or h > max_side:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, max_side, max_side, True)
            if pb is not None:
                pb = pb.apply_embedded_orientation() or pb
                t = _pixbuf_to_texture(pb)
        elif w > 0 and h > 0:
            t = Gdk.Texture.new_from_filename(path)
    except (GLib.Error, ImportError, ValueError):
        t = None
    if t is None:
        try:
            cand = Gdk.Texture.new_from_filename(path)
            if cand.get_width() <= max_side and cand.get_height() <= max_side:
                t = cand
        except GLib.Error:
            t = None
    if t is None:
        rgba = _decode_pil_rgba(path, max_side)
        if rgba is not None:
            w, h, data = rgba
            t = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(data), w * 4)
    if t is None:
        _record_negative(key, st.st_mtime_ns, st.st_size)
        return None
    _cache_put(key, t)
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
    info = getattr(button, "_ward_emoji", None)
    if info is None:
        return
    from . import emoji_picker, lifetime

    if emoji_picker.available():
        chooser: Gtk.Popover = emoji_picker.EmojiPicker(info[2])
    else:
        chooser = Gtk.EmojiChooser()

    def on_picked(_c, e: str) -> None:
        cur = getattr(button, "_ward_emoji", None)
        if cur is not None and cur[0] is not None:
            cur[0](e)

    chooser.connect("emoji-picked", on_picked)

    def on_closed(*_a) -> None:
        cur = getattr(button, "_ward_emoji", None)
        if cur is not None and cur[1] is not None:
            cur[1]()

        def drop() -> bool:
            if button.get_popover() is chooser and not button.get_active():
                button.set_popover(None)
                if isinstance(chooser, emoji_picker.EmojiPicker):
                    lifetime.release(chooser)
                    lifetime.dismantle(chooser)
                else:
                    lifetime.disconnect_all(chooser)
            return False

        GLib.idle_add(drop)

    chooser.connect("closed", on_closed)
    button.set_popover(chooser)


class Avatar(Gtk.Overlay):
    """Adw.Avatar with an image (when we have one), initials otherwise, and an optional presence dot."""

    def __init__(self, size: int = 40, text: str = "", path: str = "", show: str | None = None):
        super().__init__()
        self._avatar_text: str | None = None
        self._avatar_path: str = ""
        self._avatar_show: str | None = ...  # sentinel so first update applies show state
        self.avatar = Adw.Avatar(size=size, show_initials=True)
        self.set_child(self.avatar)
        self.dot = Gtk.Box(css_classes=["presence-dot"], halign=Gtk.Align.END, valign=Gtk.Align.END,
                           visible=False)
        self.add_overlay(self.dot)
        self.set_valign(Gtk.Align.START)
        self.update(text, path, show)

    def update(self, text: str = "", path: str = "", show: str | None = None) -> None:
        label_text = text or "?"
        if label_text != self._avatar_text:
            self._avatar_text = label_text
            self.avatar.set_text(label_text)
        path = path or ""
        if not path:
            if self._avatar_path or self.avatar.get_custom_image() is not None:
                self._avatar_path = ""
                self.avatar.set_custom_image(None)
        else:
            tex = cached_texture(path)
            if tex is not None:
                self._avatar_path = path
                if self.avatar.get_custom_image() is not tex:
                    self.avatar.set_custom_image(tex)
            elif path != self._avatar_path or self.avatar.get_custom_image() is not None:
                self._avatar_path = path
                if self.avatar.get_custom_image() is not None:
                    self.avatar.set_custom_image(None)
                ref = weakref.ref(self)

                def apply(loaded: Gdk.Texture | None) -> None:
                    av = ref()
                    if av is not None and getattr(av, "_avatar_path", None) == path and hasattr(av, "avatar"):
                        av.avatar.set_custom_image(loaded)

                texture_async(path, AVATAR_PIXELS, apply)
        if show != self._avatar_show:
            self._avatar_show = show
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


_desktop_iface = None
_desktop_iface_checked = False


def _system_clock_format() -> str:
    global _desktop_iface, _desktop_iface_checked
    if not _desktop_iface_checked:
        _desktop_iface_checked = True
        try:
            from gi.repository import Gio

            schema = Gio.SettingsSchemaSource.get_default()
            if schema is not None and schema.lookup("org.gnome.desktop.interface", True) is not None:
                _desktop_iface = Gio.Settings.new("org.gnome.desktop.interface")
        except Exception:
            _desktop_iface = None
    if _desktop_iface is not None:
        try:
            return "12" if _desktop_iface.get_string("clock-format") == "12h" else "24"
        except Exception:
            pass
    return "24"


def clock(ms: int, settings=None, seconds: bool | None = None) -> str:
    d = _dt_local(ms)
    fmt = settings["appearance.time_format"] if settings is not None else "system"
    secs = settings["appearance.time_seconds"] if seconds is None and settings is not None else bool(seconds)
    if fmt == "system":
        fmt = _system_clock_format() if Gtk.Settings.get_default() is not None else "24"
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
        release(child)
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


def _content_type_for(path: str, mime: str = "") -> str:
    """The content type of a cached file: its name/contents, or the attachment's MIME when the name says nothing."""
    from gi.repository import Gio

    try:
        content_type, _uncertain = Gio.content_type_guess(path, None)
    except Exception:
        content_type = ""
    # A cached file can have a random name without an extension: then the attachment's MIME type is better than
    # the "application/octet-stream" the guess gives back.
    if mime and (not content_type or content_type == "application/octet-stream"):
        return mime
    return content_type or mime


def _default_app_for(path: str, mime: str = ""):
    """The desktop's default handler for the file's content type, or None."""
    from gi.repository import Gio

    content_type = _content_type_for(path, mime)
    try:
        return Gio.AppInfo.get_default_for_type(content_type, False) if content_type else None
    except Exception:
        return None


def _launch_with_default(path: str, mime: str = "") -> bool:
    """Open a file with the desktop's default app for its content type, bypassing GTK's app chooser.

    Gtk.FileLauncher can end up in an app chooser (through the OpenURI portal) even when a default app is set;
    launching the default ourselves is what "double click in the file manager" does."""
    from gi.repository import Gio

    app = _default_app_for(path, mime)
    if app is None:
        return False
    display = Gdk.Display.get_default()
    context = display.get_app_launch_context() if display is not None else None
    if context is not None:
        context.set_timestamp(Gdk.CURRENT_TIME)
    gfile = Gio.File.new_for_path(path)
    try:
        if app.supports_uris():
            return bool(app.launch_uris([gfile.get_uri()], context))
        if app.supports_files():
            return bool(app.launch([gfile], context))
    except GLib.Error:
        return False
    return False


def open_file(widget: Gtk.Widget, path: str, mime: str = "") -> None:
    from gi.repository import Gio

    if _launch_with_default(path, mime):
        return
    root = widget.get_root() if widget is not None else None
    try:
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(path))
    except AttributeError:
        open_uri(widget, Gio.File.new_for_path(path).get_uri())
        return
    if hasattr(launcher, "set_always_ask"):
        launcher.set_always_ask(False)

    def done(launcher, result, _data=None):
        try:
            launcher.launch_finish(result)
        except GLib.Error as e:
            compat.toast(widget, _("Could not open the file: %s") % e.message)

    launcher.launch(root if isinstance(root, Gtk.Window) else None, None, done, None)


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
