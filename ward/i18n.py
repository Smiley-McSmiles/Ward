"""gettext setup.  Every user-visible string is marked with _() (or N_() where it is translated later), so
translations can be added in po/ without touching the code."""

from __future__ import annotations

import gettext as _gettext
import locale

from . import paths

DOMAIN = "ward"
_translation: _gettext.NullTranslations = _gettext.NullTranslations()


def setup() -> None:
    global _translation
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    d = paths.locale_dir()
    try:
        _translation = _gettext.translation(DOMAIN, localedir=d, fallback=True)
    except OSError:
        _translation = _gettext.NullTranslations()
    try:  # GtkBuilder / C libraries look the domain up through libintl
        locale.bindtextdomain(DOMAIN, d)  # type: ignore[attr-defined]
        locale.textdomain(DOMAIN)  # type: ignore[attr-defined]
    except (AttributeError, OSError, TypeError):
        pass


def _(message: str) -> str:
    return _translation.gettext(message)


def N_(message: str) -> str:
    """Mark a string for translation without translating it yet (tables built at import time)."""
    return message


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translation.ngettext(singular, plural, n)


def pgettext(context: str, message: str) -> str:
    return _translation.pgettext(context, message)
