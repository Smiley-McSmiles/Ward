"""What is installed, and how to install what is missing.

Ward needs only Python 3.11, PyGObject, GTK 4.12, libadwaita 1.4, slixmpp, markdown-it-py and cryptography.
Everything else is optional and switches a feature on: the OMEMO libraries (encryption), gpg (OpenPGP),
GtkSourceView (code highlighting and the Markdown composer), libspelling, libsecret (keyring), Pillow, segno (QR
codes)...  `ward --check` prints this report; the app shows the same hints where a feature is unavailable.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from functools import cache

from .i18n import N_

PY = "py%d%d" % sys.version_info[:2]

# package manager -> component -> package name(s).  "-" means: not packaged there, install.sh vendors it with pip.
PACKAGES: dict[str, dict[str, str]] = {
    "apt": {"python": "python3", "gi": "python3-gi", "gtk": "gir1.2-gtk-4.0", "adw": "gir1.2-adw-1",
            "source": "gir1.2-gtksource-5", "spelling": "gir1.2-spelling-1", "secret": "gir1.2-secret-1",
            "slixmpp": "python3-slixmpp", "markdown": "python3-markdown-it", "mdit": "python3-mdit-py-plugins",
            "linkify": "python3-linkify-it", "crypto": "python3-cryptography", "pillow": "python3-pil",
            "gpg": "gnupg", "media": "libgtk-4-media-gstreamer", "zbar": "zbar-tools", "pip": "python3-pip",
            "omemo": "-", "segno": "python3-segno"},
    "dnf": {"python": "python3", "gi": "python3-gobject", "gtk": "gtk4", "adw": "libadwaita",
            "source": "gtksourceview5", "spelling": "libspelling", "secret": "libsecret",
            "slixmpp": "python3-slixmpp", "markdown": "python3-markdown-it-py", "mdit": "python3-mdit-py-plugins",
            "linkify": "python3-linkify-it-py", "crypto": "python3-cryptography", "pillow": "python3-pillow",
            "gpg": "gnupg2", "media": "gstreamer1-plugins-good", "zbar": "zbar", "pip": "python3-pip",
            "omemo": "-", "segno": "python3-segno"},
    "pacman": {"python": "python", "gi": "python-gobject", "gtk": "gtk4", "adw": "libadwaita",
               "source": "gtksourceview5", "spelling": "libspelling", "secret": "libsecret",
               "slixmpp": "python-slixmpp", "markdown": "python-markdown-it-py", "mdit": "python-mdit_py_plugins",
               "linkify": "python-linkify-it-py", "crypto": "python-cryptography", "pillow": "python-pillow",
               "gpg": "gnupg", "media": "gst-plugins-good", "zbar": "zbar", "pip": "python-pip",
               "omemo": "-", "segno": "python-segno"},
    "zypper": {"python": "python3", "gi": "python3-gobject", "gtk": "typelib-1_0-Gtk-4_0",
               "adw": "typelib-1_0-Adw-1", "source": "typelib-1_0-GtkSource-5",
               "spelling": "typelib-1_0-Spelling-1", "secret": "typelib-1_0-Secret-1",
               "slixmpp": "python3-slixmpp", "markdown": "python3-markdown-it-py",
               "mdit": "python3-mdit-py-plugins", "linkify": "python3-linkify-it-py",
               "crypto": "python3-cryptography", "pillow": "python3-Pillow", "gpg": "gpg2",
               "media": "gstreamer-plugins-good", "zbar": "zbar", "pip": "python3-pip", "omemo": "-",
               "segno": "python3-segno"},
    "xbps": {"python": "python3", "gi": "python3-gobject", "gtk": "gtk4", "adw": "libadwaita",
             "source": "gtksourceview5", "spelling": "libspelling", "secret": "libsecret",
             "slixmpp": "python3-slixmpp", "markdown": "python3-markdown-it-py", "mdit": "python3-mdit-py-plugins",
             "linkify": "python3-linkify-it-py", "crypto": "python3-cryptography", "pillow": "python3-Pillow",
             "gpg": "gnupg", "media": "gst-plugins-good1", "zbar": "zbar", "pip": "python3-pip", "omemo": "-",
             "segno": "-"},
    "apk": {"python": "python3", "gi": "py3-gobject3", "gtk": "gtk4.0", "adw": "libadwaita",
            "source": "gtksourceview5", "spelling": "libspelling", "secret": "libsecret",
            "slixmpp": "py3-slixmpp", "markdown": "py3-markdown-it-py", "mdit": "py3-mdit-py-plugins",
            "linkify": "py3-linkify-it-py", "crypto": "py3-cryptography", "pillow": "py3-pillow", "gpg": "gnupg",
            "media": "gst-plugins-good", "zbar": "zbar", "pip": "py3-pip", "omemo": "-", "segno": "py3-segno"},
    "emerge": {"python": "dev-lang/python", "gi": "dev-python/pygobject", "gtk": "gui-libs/gtk",
               "adw": "gui-libs/libadwaita", "source": "x11-libs/gtksourceview:5",
               "spelling": "app-text/libspelling", "secret": "app-crypt/libsecret",
               "slixmpp": "dev-python/slixmpp", "markdown": "dev-python/markdown-it-py",
               "mdit": "dev-python/mdit-py-plugins", "linkify": "dev-python/linkify-it-py",
               "crypto": "dev-python/cryptography", "pillow": "dev-python/pillow", "gpg": "app-crypt/gnupg",
               "media": "media-plugins/gst-plugins-meta", "zbar": "media-gfx/zbar", "pip": "dev-python/pip",
               "omemo": "-", "segno": "-"},
    "pkg": {"python": "python3", "gi": PY + "-gobject3", "gtk": "gtk4", "adw": "libadwaita",
            "source": "gtksourceview5", "spelling": "libspelling", "secret": "libsecret",
            "slixmpp": PY + "-slixmpp", "markdown": PY + "-markdown-it-py", "mdit": PY + "-mdit-py-plugins",
            "linkify": PY + "-linkify-it-py", "crypto": PY + "-cryptography", "pillow": PY + "-pillow",
            "gpg": "gnupg", "media": "gstreamer1-plugins-good", "zbar": "zbar", "pip": PY + "-pip",
            "omemo": "-", "segno": PY + "-segno"},
    "pkg_add": {"python": "python%3", "gi": "py3-gobject3", "gtk": "gtk+4", "adw": "libadwaita",
                "source": "gtksourceview5", "spelling": "libspelling", "secret": "libsecret",
                "slixmpp": "py3-slixmpp", "markdown": "py3-markdown-it-py", "mdit": "py3-mdit-py-plugins",
                "linkify": "py3-linkify-it-py", "crypto": "py3-cryptography", "pillow": "py3-Pillow",
                "gpg": "gnupg", "media": "gstreamer1-plugins-good", "zbar": "zbar", "pip": "py3-pip",
                "omemo": "-", "segno": "-"},
    "pkgin": {"python": "python311", "gi": PY + "-gobject3", "gtk": "gtk4", "adw": "libadwaita",
              "source": "gtksourceview5", "spelling": "-", "secret": "libsecret", "slixmpp": PY + "-slixmpp",
              "markdown": PY + "-markdown-it-py", "mdit": PY + "-mdit-py-plugins", "linkify": "-",
              "crypto": PY + "-cryptography", "pillow": PY + "-Pillow", "gpg": "gnupg2",
              "media": "gst-plugins1-good", "zbar": "zbar", "pip": PY + "-pip", "omemo": "-", "segno": "-"},
}

INSTALL_CMD = {"apt": "sudo apt install", "dnf": "sudo dnf install", "pacman": "sudo pacman -S",
               "zypper": "sudo zypper install", "xbps": "sudo xbps-install -S", "apk": "doas apk add",
               "emerge": "sudo emerge --ask", "pkg": "sudo pkg install", "pkg_add": "doas pkg_add",
               "pkgin": "sudo pkgin install"}

_PM_BINARIES = (("apt", "apt-get"), ("dnf", "dnf"), ("pacman", "pacman"), ("zypper", "zypper"),
                ("xbps", "xbps-install"), ("apk", "apk"), ("emerge", "emerge"), ("pkg_add", "pkg_add"),
                ("pkgin", "pkgin"), ("pkg", "pkg"))


@cache
def package_manager() -> str | None:
    if os.path.exists("/.flatpak-info"):
        return None
    sysname = platform.system()
    for name, binary in _PM_BINARIES:
        if name == "pkg" and sysname not in ("FreeBSD", "DragonFly"):
            continue
        if name == "pkg_add" and sysname != "OpenBSD":
            continue
        if shutil.which(binary):
            return name
    return None


def install_hint(*components: str) -> str:
    """A ready-to-paste command for this system, or a pip/install.sh hint for what is not packaged."""
    pm = package_manager()
    if pm is None:
        return ""
    names = [PACKAGES[pm].get(c, "-") for c in components]
    pkgs = [n for n in names if n and n != "-"]
    vendored = [c for c, n in zip(components, names, strict=True) if n == "-"]
    parts = []
    if pkgs:
        parts.append("%s %s" % (INSTALL_CMD[pm], " ".join(pkgs)))
    if vendored:
        parts.append("Ward installs %s itself (Preferences > Encryption), or ./install.sh --vendor"
                     % ", ".join(vendored))
    return "; ".join(parts)


@dataclass
class Feature:
    key: str
    title: str
    available: bool
    detail: str = ""
    components: tuple[str, ...] = ()
    required: bool = False

    @property
    def hint(self) -> str:
        return "" if self.available else install_hint(*self.components)


def _module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _typelib(namespace: str, version: str) -> tuple[bool, str]:
    try:
        import gi

        gi.require_version(namespace, version)
        mod = importlib.import_module("gi.repository." + namespace)
    except (ImportError, ValueError):
        return False, ""
    ver = ""
    for a, b, c in (("MAJOR_VERSION", "MINOR_VERSION", "MICRO_VERSION"),
                    ("get_major_version", "get_minor_version", "get_micro_version")):
        try:
            parts = [getattr(mod, x) for x in (a, b, c)]
            parts = [p() if callable(p) else p for p in parts]
            ver = "%d.%d.%d" % tuple(parts)
            break
        except (AttributeError, TypeError):
            continue
    return True, ver


@cache
def gtk_version() -> tuple[int, int]:
    ok, v = _typelib("Gtk", "4.0")
    return tuple(int(x) for x in v.split(".")[:2]) if ok and v else (0, 0)  # type: ignore[return-value]


@cache
def adw_version() -> tuple[int, int]:
    ok, v = _typelib("Adw", "1")
    return tuple(int(x) for x in v.split(".")[:2]) if ok and v else (0, 0)  # type: ignore[return-value]


IMPORT_ERRORS: dict[str, str] = {}


def _imports(*names: str) -> bool:
    """Really import them: a module that is present but broken (say, by an older system protobuf or pydantic)
    must count as missing, with the reason kept for the user."""
    for n in names:
        try:
            importlib.import_module(n)
        except Exception as e:
            IMPORT_ERRORS[n] = "%s: %s" % (type(e).__name__, e)
            return False
    return True


@cache
def omemo_available() -> bool:
    return _imports("omemo", "twomemo", "xeddsa", "x3dh", "doubleratchet", "twomemo.etree")


@cache
def legacy_omemo_available() -> bool:
    return omemo_available() and _imports("oldmemo", "oldmemo.etree")


@cache
def have(name: str) -> bool:
    """Optional Python modules and typelibs by short name."""
    if name == "source":
        return _typelib("GtkSource", "5")[0]
    if name == "spelling":
        # some distributions ship the typelib without the library (or the other way round)
        import ctypes.util

        return ctypes.util.find_library("spelling-1") is not None and _typelib("Spelling", "1")[0]
    if name == "secret":
        return _typelib("Secret", "1")[0]
    if name == "pillow":
        return _module("PIL")
    if name == "segno":
        return _module("segno")
    if name == "linkify":
        return _module("linkify_it")
    if name == "mdit":
        return _module("mdit_py_plugins")
    if name == "zbar":
        return shutil.which("zbarimg") is not None or _module("pyzbar")
    return _module(name)


def features() -> list[Feature]:
    gtk_ok, gtk_v = _typelib("Gtk", "4.0")
    adw_ok, adw_v = _typelib("Adw", "1")
    slix = _module("slixmpp")
    slix_v = ""
    if slix:
        try:
            slix_v = importlib.import_module("slixmpp").__version__
        except Exception:
            slix_v = "?"
    from .core.pgp import gnupg

    gpg = gnupg.find_gpg()
    out = [
        Feature("python", N_("Python 3.11 or newer"), sys.version_info >= (3, 11), platform.python_version(),
                ("python",), True),
        Feature("gi", N_("PyGObject"), _module("gi"), _gi_version(), ("gi",), True),
        Feature("gtk", N_("GTK 4.12 or newer"), gtk_ok and gtk_version() >= (4, 12), gtk_v, ("gtk",), True),
        Feature("adw", N_("libadwaita 1.4 or newer"), adw_ok and adw_version() >= (1, 4), adw_v, ("adw",), True),
        Feature("slixmpp", N_("slixmpp 1.8 or newer (XMPP)"), slix, slix_v, ("slixmpp",), True),
        Feature("markdown", N_("markdown-it-py (Markdown)"), _module("markdown_it"), "", ("markdown",), True),
        Feature("crypto", N_("cryptography (encrypted files)"), _module("cryptography"), "", ("crypto",), True),
        Feature("omemo", N_("OMEMO 2 encryption (python-omemo, twomemo)"), omemo_available(), "", ("omemo",)),
        Feature("oldmemo", N_("Legacy OMEMO 0.3 for today's apps (oldmemo, AGPL-3.0)"),
                legacy_omemo_available(), "", ("omemo",)),
        Feature("gpg", N_("OpenPGP (GnuPG)"), gpg is not None, gpg or "", ("gpg",)),
        Feature("source", N_("Code highlighting and Markdown composer (GtkSourceView 5)"), have("source"), "",
                ("source",)),
        Feature("spelling", N_("Spell check (libspelling)"), have("spelling"), "", ("spelling",)),
        Feature("secret", N_("System keyring (libsecret)"), have("secret"), "", ("secret",)),
        Feature("pillow", N_("Image resizing and metadata stripping (Pillow)"), have("pillow"),
                N_("GdkPixbuf is used without it"), ("pillow",)),
        Feature("segno", N_("QR codes (segno)"), have("segno"), "", ("segno",)),
        Feature("linkify", N_("Better link detection (linkify-it-py)"), have("linkify"), "", ("linkify",)),
        Feature("zbar", N_("Reading QR codes from images (zbar)"), have("zbar"), "", ("zbar",)),
    ]
    return out


def _gi_version() -> str:
    try:
        import gi

        return gi.__version__
    except Exception:
        return ""


def missing_required() -> list[Feature]:
    return [f for f in features() if f.required and not f.available]


def report() -> str:
    from .i18n import _

    lines = ["Ward v%s on %s %s, Python %s" % (_ward_version(), platform.system(), platform.release(),
                                               platform.python_version())]
    pm = package_manager()
    lines.append("Package manager: %s" % (pm or ("Flatpak sandbox" if os.path.exists("/.flatpak-info")
                                                  else "unknown")))
    lines.append("")
    for f in features():
        mark = "ok " if f.available else ("MISSING" if f.required else "-- ")
        detail = (" (%s)" % f.detail) if f.detail else ""
        lines.append("  [%s] %s%s" % (mark, _(f.title), detail))
        if not f.available and f.hint:
            lines.append("          install: %s" % f.hint)
    return "\n".join(lines)


def _ward_version() -> str:
    from . import __version__

    return __version__
