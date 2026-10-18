"""GnuPG engine: runs the gpg program (in the worker pool, never on the main thread).

Ward shares its keyring with GPGMan (https://github.com/Smiley-McSmiles/GPGMan): when "Share GPGMan's keyring" is
on, the gpg binary and GnuPG home chosen in GPGMan (~/.config/gpgman/settings.json) are used, so a key generated
or imported in GPGMan can be picked in Ward right away, and keys Ward imports show up in GPGMan."""

from __future__ import annotations

import json
import os
import shutil


def gpgman_settings_file() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    # GPGMan itself always uses ~/.config/gpgman; honour XDG_CONFIG_HOME too for sandboxed setups
    for cand in (os.path.join(os.path.expanduser("~"), ".config", "gpgman", "settings.json"),
                 os.path.join(base, "gpgman", "settings.json")):
        if os.path.isfile(cand):
            return cand
    return os.path.join(os.path.expanduser("~"), ".config", "gpgman", "settings.json")


def gpgman_settings() -> tuple[str | None, str | None]:
    """(gpg_binary, gnupg_home) as configured in GPGMan, each None when unset or unusable."""
    try:
        with open(gpgman_settings_file(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    binary = data.get("gpg_binary")
    home = data.get("gnupg_home")
    if not (isinstance(binary, str) and os.path.isfile(binary) and os.access(binary, os.X_OK)):
        binary = None
    if not (isinstance(home, str) and home.strip()):
        home = None
    else:
        home = os.path.abspath(os.path.expanduser(home))
    return binary, home


def gpgman_installed() -> str | None:
    return shutil.which("gpgman")


def find_gpg(preferred: str = "", use_gpgman: bool = True) -> str | None:
    """The gpg program to use: an explicit setting, then GPGMan's choice, then gpg2/gpg on PATH."""
    if preferred and os.path.isfile(preferred) and os.access(preferred, os.X_OK):
        return preferred
    if use_gpgman:
        binary, _home = gpgman_settings()
        if binary:
            return binary
    for name in ("gpg", "gpg2"):
        p = shutil.which(name)
        if p:
            return p
    for p in ("/usr/local/bin/gpg", "/usr/pkg/bin/gpg2", "/usr/pkg/bin/gpg", "/usr/bin/gpg"):
        if os.access(p, os.X_OK):
            return p
    return None
