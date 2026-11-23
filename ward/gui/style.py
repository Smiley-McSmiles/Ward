"""Style sheets: style.css (shipped), a generated sheet for the appearance preferences (accent colour, text size,
fonts, line spacing, avatar shape), and the user's optional custom.css, loaded last."""

from __future__ import annotations

import os

from gi.repository import Adw, Gdk, Gtk

from .. import paths, schema

HERE = os.path.dirname(os.path.abspath(__file__))
_base: Gtk.CssProvider | None = None
_dynamic: Gtk.CssProvider | None = None
_custom: Gtk.CssProvider | None = None
_last_dynamic_css: str | None = None
_state = {"accent": "system", "scale": 100, "font": "", "code_font": "", "spacing": "normal", "shape": "circle"}


def _load(provider: Gtk.CssProvider, css: str) -> None:
    try:
        provider.load_from_string(css)          # GTK 4.12
    except AttributeError:
        provider.load_from_data(css, -1)


def install() -> None:
    global _base, _dynamic, _custom, _last_dynamic_css
    display = Gdk.Display.get_default()
    if display is None:
        return
    if _base is None:
        _base = Gtk.CssProvider()
        _base.load_from_path(os.path.join(HERE, "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, _base, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        _dynamic = Gtk.CssProvider()
        _last_dynamic_css = None
        Gtk.StyleContext.add_provider_for_display(display, _dynamic, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
    _apply()


def _apply() -> None:
    global _last_dynamic_css
    if _dynamic is None:
        return
    css = []
    acc = schema.ACCENT_COLORS.get(_state["accent"])
    if acc:
        css.append("@define-color accent_bg_color %s; @define-color accent_color %s; "
                   "@define-color accent_fg_color #ffffff;" % (acc, acc))
    scale = int(_state["scale"])
    if scale != 100:
        css.append(".message-text, .chat-row-title, .chat-row-preview { font-size: %d%%; }" % scale)
    if _state["font"]:
        css.append(".message-text { font-family: \"%s\"; }" % _state["font"].replace('"', ""))
    if _state["code_font"]:
        css.append(".message-code, .message-text .code { font-family: \"%s\"; }" % _state["code_font"].replace('"', ""))
    lh = {"compact": 1.1, "normal": 1.3, "relaxed": 1.6}.get(_state["spacing"], 1.3)
    css.append(".message-text { line-height: %.2f; }" % lh)
    if _state["shape"] == "rounded":
        css.append("avatar { border-radius: 22%; }")
    new_css = "\n".join(css)
    if new_css != _last_dynamic_css:
        _last_dynamic_css = new_css
        _load(_dynamic, new_css)


def set_accent(name: str) -> None:
    _state["accent"] = name
    _apply()


def set_appearance(settings) -> None:
    _state.update(accent=settings["appearance.accent"], scale=settings["appearance.text_scale"],
                  font=settings["appearance.message_font"], code_font=settings["appearance.code_font"],
                  spacing=settings["appearance.line_spacing"], shape=settings["appearance.avatar_shape"])
    _apply()
    sm = Adw.StyleManager.get_default()
    scheme = {"light": Adw.ColorScheme.FORCE_LIGHT, "dark": Adw.ColorScheme.FORCE_DARK}.get(
        settings["appearance.style"], Adw.ColorScheme.DEFAULT)
    if sm.get_color_scheme() != scheme:
        sm.set_color_scheme(scheme)


def load_custom(enabled: bool) -> str:
    """custom.css from the config folder, loaded last.  Returns an error message or ''."""
    global _custom
    display = Gdk.Display.get_default()
    if display is None:
        return ""
    if _custom is not None:
        Gtk.StyleContext.remove_provider_for_display(display, _custom)
        _custom = None
    path = paths.custom_css()
    if not enabled or not os.path.isfile(path):
        return ""
    _custom = Gtk.CssProvider()
    errors: list[str] = []
    hid = _custom.connect("parsing-error", lambda _p, _s, err: errors.append(err.message))
    try:
        _custom.load_from_path(path)
    finally:
        _custom.disconnect(hid)
    Gtk.StyleContext.add_provider_for_display(display, _custom, Gtk.STYLE_PROVIDER_PRIORITY_USER)
    return errors[0] if errors else ""


def is_dark() -> bool:
    return Adw.StyleManager.get_default().get_dark()
