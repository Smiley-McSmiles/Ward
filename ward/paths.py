"""Filesystem locations.  Everything follows the XDG base directory spec, which also makes Flatpak work without
special cases (the sandbox points XDG_* at ~/.var/app/<id>/).  WARD_HOME moves all of them under one directory,
which is what the test-suite and portable setups use; WARD_SHARE_DIR is set by the installed launcher."""

from __future__ import annotations

import os

from . import APP_ID

NAME = "ward"


def _home() -> str:
    return os.path.expanduser("~")


def _xdg(var: str, fallback: str) -> str:
    base = os.environ.get("WARD_HOME")
    if base:
        sub = {"XDG_CONFIG_HOME": "config", "XDG_DATA_HOME": "data", "XDG_CACHE_HOME": "cache",
               "XDG_STATE_HOME": "state"}[var]
        return os.path.join(base, sub)
    v = os.environ.get(var)
    root = v if v and os.path.isabs(v) else os.path.join(_home(), fallback)
    return os.path.join(root, NAME)


def config_dir() -> str:
    return _xdg("XDG_CONFIG_HOME", ".config")


def data_dir() -> str:
    return _xdg("XDG_DATA_HOME", ".local/share")


def cache_dir() -> str:
    return _xdg("XDG_CACHE_HOME", ".cache")


def state_dir() -> str:
    return _xdg("XDG_STATE_HOME", ".local/state")


def ensure(path: str, private: bool = True) -> str:
    os.makedirs(path, mode=0o700 if private else 0o755, exist_ok=True)
    return path


def settings_file() -> str:
    return os.path.join(config_dir(), "settings.json")


def custom_css() -> str:
    return os.path.join(config_dir(), "custom.css")


def db_file() -> str:
    return os.path.join(data_dir(), "ward.db")


def secrets_file() -> str:
    """Only used when the user explicitly allows storing passwords in a file (no keyring available)."""
    return os.path.join(data_dir(), "passwords.json")


def log_file() -> str:
    return os.path.join(state_dir(), "ward.log")


def files_cache() -> str:
    return os.path.join(cache_dir(), "files")


def thumbnails_cache() -> str:
    return os.path.join(cache_dir(), "thumbnails")


def avatars_cache() -> str:
    return os.path.join(cache_dir(), "avatars")


def previews_cache() -> str:
    return os.path.join(cache_dir(), "previews")


def drafts_dir() -> str:
    return os.path.join(data_dir(), "drafts")


def backups_dir() -> str:
    return os.path.join(data_dir(), "backups")


def autostart_file() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(_home(), ".config")
    return os.path.join(base, "autostart", APP_ID + ".desktop")


def package_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def share_dir() -> str:
    """Installed read-only data (icons, sounds, locale).  The source tree's data/ is used when running from git."""
    d = os.environ.get("WARD_SHARE_DIR")
    if d and os.path.isdir(d):
        return d
    src = os.path.join(os.path.dirname(package_dir()), "data")
    return src if os.path.isdir(src) else ""


def locale_dir() -> str | None:
    d = os.environ.get("WARD_LOCALE_DIR")
    if d:
        return d
    share = share_dir()
    for cand in (os.path.join(share, "..", "locale"), os.path.join(share, "locale"),
                 os.path.join(os.path.dirname(package_dir()), "build", "locale")):
        if share and os.path.isdir(cand):
            return os.path.abspath(cand)
    return None


def in_flatpak() -> bool:
    return os.path.exists("/.flatpak-info")


def appimage() -> str:
    """The AppImage Ward runs from (its AppRun sets this), or ""."""
    return os.environ.get("WARD_APPIMAGE", "")


def user_downloads() -> str:
    try:
        from gi.repository import GLib

        d = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
        if d:
            return d
    except Exception:
        pass
    return os.path.join(_home(), "Downloads")
