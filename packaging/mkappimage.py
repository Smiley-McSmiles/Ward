#!/usr/bin/env python3
"""Fill an AppDir with everything Ward needs to run on a distribution that has none of it: this Python, PyGObject,
GTK 4, libadwaita, GStreamer with WebRTC and libnice (calls), the media plugins (videos), the OMEMO libraries and the
rest from PyPI.

usage: mkappimage.py <AppDir with Ward staged under usr/ by install.sh>

Runs on a Debian or Ubuntu build machine (multiarch library folders) with the Python that is to be bundled; build on
the oldest distribution that still has GTK 4.12 and libadwaita 1.4 (Ubuntu 24.04): its glibc is the oldest the
AppImage runs on.  Libraries every desktop has (glibc, the graphics drivers, X11, ALSA, fontconfig...) stay the
system's, as AppImage's excludelist says; everything bundled finds its libraries through an $ORIGIN rpath, so nothing
leaks into the programs Ward starts (a browser, a player) through LD_LIBRARY_PATH.  package.sh packs the result."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import sysconfig

try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = None  # type: ignore[assignment]

APPID = "io.github.smiley_mcsmiles.Ward"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# GObject introspection namespaces Ward loads (with what they need in turn)
NAMESPACES = [("Gtk", "4.0"), ("Adw", "1"), ("GdkPixbuf", "2.0"), ("Gst", "1.0"), ("GstSdp", "1.0"),
              ("GstWebRTC", "1.0"), ("GstVideo", "1.0"), ("GstAudio", "1.0"), ("GstApp", "1.0"),
              ("GstPbutils", "1.0"), ("GtkSource", "5"), ("Secret", "1"), ("Spelling", "1")]

# GStreamer plugins: calls (capture, WebRTC, ICE, DTLS/SRTP, Opus, VP8, playback) and the videos people send
# (Matroska/WebM, MP4, Ogg, H.264 and friends through libav)
GST_PLUGINS = ["coreelements", "app", "audioconvert", "audioresample", "audiotestsrc", "videotestsrc", "volume",
               "videoconvertscale", "videorate", "playback", "typefindfunctions", "autodetect", "pulseaudio",
               "pipewire", "alsa", "video4linux2", "webrtc", "nice", "dtls", "srtp", "sctp", "rtp", "rtpmanager",
               "opus", "opusparse", "vpx", "pbtypes", "matroska", "isomp4", "ogg", "vorbis", "theora", "audioparsers",
               "videoparsersbad", "id3demux", "wavparse", "flac", "libav", "dav1d", "aom", "webrtcdsp", "gtk4"]

# AppImage's excludelist (github.com/AppImageCommunity/pkg2appimage): libraries that must come from the system
EXCLUDE = {"ld-linux.so.2", "ld-linux-x86-64.so.2", "ld-linux-aarch64.so.1", "libanl.so.1", "libBrokenLocale.so.1",
           "libcidn.so.1", "libc.so.6", "libdl.so.2", "libm.so.6", "libmvec.so.1", "libnss_compat.so.2",
           "libnss_dns.so.2", "libnss_files.so.2", "libnss_hesiod.so.2", "libnss_nisplus.so.2", "libnss_nis.so.2",
           "libpthread.so.0", "libresolv.so.2", "librt.so.1", "libthread_db.so.1", "libutil.so.1", "libstdc++.so.6",
           "libGL.so.1", "libEGL.so.1", "libGLdispatch.so.0", "libGLX.so.0", "libOpenGL.so.0", "libdrm.so.2",
           "libglapi.so.0", "libgbm.so.1", "libxcb.so.1", "libX11.so.6", "libX11-xcb.so.1", "libwayland-client.so.0",
           "libasound.so.2", "libfontconfig.so.1", "libfreetype.so.6", "libharfbuzz.so.0", "libcom_err.so.2",
           "libexpat.so.1", "libgcc_s.so.1", "libgpg-error.so.0", "libICE.so.6", "libSM.so.6", "libusb-1.0.so.0",
           "libuuid.so.1", "libz.so.1", "libjack.so.0", "libpipewire-0.3.so.0", "libxcb-dri3.so.0", "libxcb-dri2.so.0",
           "libfribidi.so.0", "libgmp.so.10"}

# the standard library modules a GUI app never imports
STDLIB_SKIP = {"test", "idlelib", "tkinter", "turtledemo", "ensurepip", "lib2to3", "pydoc_data", "site-packages",
               "dist-packages", "__pycache__", "EXTERNALLY-MANAGED", "turtle.py"}


def run(*cmd: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kw)


def log(msg: str) -> None:
    print("  " + msg, flush=True)


class Bundle:
    def __init__(self, appdir: str):
        self.appdir = os.path.abspath(appdir)
        self.usr = os.path.join(self.appdir, "usr")
        self.ma = sysconfig.get_config_var("MULTIARCH") or ""
        if not self.ma or not os.path.isdir("/usr/lib/" + self.ma):
            sys.exit("mkappimage.py: needs a Debian or Ubuntu build machine (multiarch library folders)")
        self.syslib = "/usr/lib/" + self.ma
        self.lib = os.path.join(self.usr, "lib", self.ma)
        self.pyver = "python%d.%d" % sys.version_info[:2]
        self.site = os.path.join(self.usr, "lib", "python3", "dist-packages")
        self.elves: set[str] = set()       # bundled ELF files whose libraries are looked up and rpath set
        self.libs: dict[str, str] = {}     # soname -> bundled path

    # ------------------------------------------------------------------ Python
    def python(self) -> None:
        log("Python %d.%d" % sys.version_info[:2])
        exe = os.path.realpath(sys.executable)
        bindir = os.path.join(self.usr, "bin")
        os.makedirs(bindir, exist_ok=True)
        shutil.copy2(exe, os.path.join(bindir, "python3"))
        self.elves.add(os.path.join(bindir, "python3"))
        stdlib = sysconfig.get_path("stdlib")
        dest = os.path.join(self.usr, "lib", self.pyver)

        def ignore(d: str, names: list[str]) -> list[str]:
            out = [n for n in names if n in STDLIB_SKIP or n.startswith("config-")]
            if os.path.basename(d) == "lib-dynload":
                out += [n for n in names if n.startswith(("_tkinter", "_test", "_xxtest"))]
            return out
        shutil.copytree(stdlib, dest, ignore=ignore, symlinks=False, dirs_exist_ok=True)
        os.makedirs(self.site, exist_ok=True)
        for f in self._files(os.path.join(dest, "lib-dynload")):
            self.elves.add(f)

    def gi(self) -> None:
        """PyGObject (and pycairo, and GStreamer's overrides) as the build machine has them for this Python."""
        import gi

        src = os.path.dirname(gi.__file__)
        tag = sysconfig.get_config_var("SOABI") or ""
        dest = os.path.join(self.site, "gi")

        def ignore(_d: str, names: list[str]) -> list[str]:
            return [n for n in names if n == "__pycache__" or (n.endswith(".so") and ".cpython-" in n and tag not in n)]
        shutil.copytree(src, dest, ignore=ignore, dirs_exist_ok=True)
        try:
            import cairo

            shutil.copytree(os.path.dirname(cairo.__file__), os.path.join(self.site, "cairo"), ignore=ignore,
                            dirs_exist_ok=True)
        except ImportError:
            pass
        for d in ("gi", "cairo"):
            self.elves.update(f for f in self._files(os.path.join(self.site, d)) if f.endswith(".so"))

    def pypi(self) -> None:
        """Ward's Python dependencies, OMEMO included, from PyPI (wheels: they carry their own libraries)."""
        reqs = ["slixmpp>=1.8.0", "markdown-it-py>=2.0", "cryptography>=3.3.2", "omemo>=2.0,<3", "twomemo[xml]>=2.0,<3",
                "oldmemo[xml]>=2.0,<3", "mdit-py-plugins>=0.3", "linkify-it-py>=2.0", "segno>=1.5", "Pillow>=9"]
        if tomllib is not None:
            with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
                project = tomllib.load(fh)["project"]
            opt = project.get("optional-dependencies", {})
            reqs = project["dependencies"] + [r for k in ("omemo", "legacy-omemo", "extras") for r in opt.get(k, [])]
        log("PyPI: " + " ".join(re.split(r"[<>=\[]", r)[0] for r in reqs))
        run(sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-compile",
            "--prefer-binary", "--no-warn-script-location", "--target", self.site, *reqs,
            env={**os.environ, "PIP_BREAK_SYSTEM_PACKAGES": "1", "PIP_USER": "0",
                                     "PIP_ROOT_USER_ACTION": "ignore"})
        shutil.rmtree(os.path.join(self.site, "bin"), ignore_errors=True)

    def ward(self) -> None:
        """install.sh put Ward in usr/lib/ward and a launcher in usr/bin: here Python finds it by itself."""
        src = os.path.join(self.usr, "lib", "ward", "ward")
        shutil.rmtree(os.path.join(self.site, "ward"), ignore_errors=True)
        shutil.move(src, os.path.join(self.site, "ward"))
        shutil.rmtree(os.path.join(self.usr, "lib", "ward"))
        os.unlink(os.path.join(self.usr, "bin", "ward"))
        shutil.rmtree(os.path.join(self.usr, "lib", "systemd"), ignore_errors=True)

    # ------------------------------------------------------------------ GTK, GStreamer and their modules
    def typelibs(self) -> None:
        code = ("import gi, json\n"
                "gi.require_version('GIRepository', '2.0')\n"
                "from gi.repository import GIRepository\n"
                "r = GIRepository.Repository.get_default()\n"
                "out = {}\n"
                "for ns, v in %r:\n"
                "    try:\n"
                "        r.require(ns, v, 0)\n"
                "    except Exception:\n"
                "        continue\n"
                "for ns in r.get_loaded_namespaces():\n"
                "    out[ns] = [r.get_typelib_path(ns), r.get_shared_library(ns) or '']\n"
                "print(json.dumps(out))\n" % (NAMESPACES,))
        import json

        found = json.loads(run(sys.executable, "-c", code, capture_output=True, text=True).stdout)
        dest = os.path.join(self.lib, "girepository-1.0")
        os.makedirs(dest, exist_ok=True)
        missing = [ns for ns, _v in NAMESPACES if ns not in found]
        log("typelibs: %d%s" % (len(found), (" (not on this machine: %s)" % ", ".join(missing)) if missing else ""))
        for ns, (path, libs) in sorted(found.items()):
            if ns == "GIRepository":        # only here to ask the questions above
                continue
            shutil.copy2(path, dest)
            for soname in filter(None, libs.split(",")):
                self.add_lib(soname)

    def gstreamer(self) -> None:
        src = os.path.join(self.syslib, "gstreamer-1.0")
        dest = os.path.join(self.lib, "gstreamer-1.0")
        os.makedirs(dest, exist_ok=True)
        have = []
        for name in GST_PLUGINS:
            f = os.path.join(src, "libgst%s.so" % name)
            if os.path.exists(f):
                shutil.copy2(f, dest)
                self.elves.add(os.path.join(dest, os.path.basename(f)))
                have.append(name)
        log("GStreamer plugins: %d (not on this machine: %s)" % (len(have), " ".join(
            sorted(set(GST_PLUGINS) - set(have))) or "none"))
        helpers = os.path.join(self.lib, "gstreamer1.0", "gstreamer-1.0")
        os.makedirs(helpers, exist_ok=True)
        scanner = os.path.join(self.syslib, "gstreamer1.0", "gstreamer-1.0", "gst-plugin-scanner")
        shutil.copy2(scanner, helpers)
        self.elves.add(os.path.join(helpers, "gst-plugin-scanner"))

    def nss(self) -> None:
        """NSS's crypto modules are loaded by name, not linked, so ldd and closure() never see them.  libsrtp2 on
        Ubuntu is built against NSS: without libsoftokn3/libfreebl3, srtp_init() fails ("Could not initialize
        SRTP encoder") and calls connect but carry no audio or video."""
        names = ("libnss3.so", "libnssutil3.so", "libnspr4.so", "libplc4.so", "libplds4.so",
                 "libsoftokn3.so", "libsoftokn3.chk", "libfreebl3.so", "libfreebl3.chk",
                 "libfreeblpriv3.so", "libfreeblpriv3.chk", "libnssdbm3.so", "libnssdbm3.chk",
                 "libnssckbi.so", "libnsssysinit.so", "libsmime3.so", "libssl3.so")
        copied = []
        for name in names:
            for directory in (self.syslib, os.path.join(self.syslib, "nss")):
                source = os.path.join(directory, name)
                if not os.path.exists(source) or os.path.exists(os.path.join(self.lib, name)):
                    continue
                os.makedirs(self.lib, exist_ok=True)
                shutil.copy2(source, os.path.join(self.lib, name))
                if name.endswith(".so"):
                    self.elves.add(os.path.join(self.lib, name))
                copied.append(name)
                break
        log("NSS modules: %d" % len(copied))

    def gtk_modules(self) -> None:
        """GTK's GStreamer media backend (videos in chats), GdkPixbuf's loaders (SVG icons), dconf and Enchant's
        Hunspell provider (spell check, with the system's dictionaries)."""
        for rel in ("gtk-4.0/4.0.0/media/libmedia-gstreamer.so", "gio/modules/libdconfsettings.so",
                    "enchant-2/enchant_hunspell.so"):
            f = os.path.join(self.syslib, rel)
            if os.path.exists(f):
                os.makedirs(os.path.dirname(os.path.join(self.lib, rel)), exist_ok=True)
                shutil.copy2(f, os.path.join(self.lib, rel))
                self.elves.add(os.path.join(self.lib, rel))
        src = os.path.join(self.syslib, "gdk-pixbuf-2.0", "2.10.0", "loaders")
        dest = os.path.join(self.lib, "gdk-pixbuf-2.0", "2.10.0", "loaders")
        os.makedirs(dest, exist_ok=True)
        for f in sorted(os.listdir(src)):
            if f.endswith(".so"):
                shutil.copy2(os.path.join(src, f), dest)
                self.elves.add(os.path.join(dest, f))
        # the cache names each loader by its full path: AppRun puts the image's mount point in for @APPDIR@
        cache = run(os.path.join(self.syslib, "gdk-pixbuf-2.0", "gdk-pixbuf-query-loaders"),
                    *sorted(os.path.join(src, f) for f in os.listdir(src) if f.endswith(".so")),
                    capture_output=True, text=True).stdout
        rel = "@APPDIR@/usr/lib/%s/gdk-pixbuf-2.0/2.10.0/loaders" % self.ma
        with open(os.path.join(self.lib, "gdk-pixbuf-2.0", "2.10.0", "loaders.cache.in"), "w") as fh:
            fh.write(cache.replace(src, rel))

    def enchant(self) -> None:
        """Enchant looks for its providers only in the folder it was built for.  The bundled copy is pointed at
        /proc/self/fd/9 instead, a folder AppRun opens as descriptor 9: the image's enchant-2 folder."""
        lib = self.libs.get("libenchant-2.so.2")
        if lib is None or not os.path.exists(os.path.join(self.lib, "enchant-2")):
            return
        old = ("/usr/lib/%s/enchant-2" % self.ma).encode() + b"\0"
        new = b"/proc/self/fd/9"
        with open(lib, "rb") as fh:
            data = fh.read()
        if data.count(old) != 1:
            log("warning: Enchant's provider folder not found in %s: spell check uses the system's" % lib)
            return
        with open(lib, "wb") as fh:
            fh.write(data.replace(old, new.ljust(len(old), b"\0")))

    def data(self) -> None:
        """Compiled GSettings schemas (GTK's file chooser), the Adwaita icons libadwaita draws with and the MIME
        database (GdkPixbuf recognises SVG icons by it; nearly every system has one, the image does not rely on it)."""
        share = os.path.join(self.usr, "share")
        if os.path.exists("/usr/share/mime/mime.cache"):
            os.makedirs(os.path.join(share, "mime"), exist_ok=True)
            for f in sorted(os.listdir("/usr/share/mime")):
                if os.path.isfile(os.path.join("/usr/share/mime", f)):
                    shutil.copy2(os.path.join("/usr/share/mime", f), os.path.join(share, "mime"))
        schemas = "/usr/share/glib-2.0/schemas/gschemas.compiled"
        if os.path.exists(schemas):
            os.makedirs(os.path.join(share, "glib-2.0", "schemas"), exist_ok=True)
            shutil.copy2(schemas, os.path.join(share, "glib-2.0", "schemas"))
        for theme in ("Adwaita", "hicolor"):
            src = os.path.join("/usr/share/icons", theme)
            if not os.path.isdir(src):
                continue
            dest = os.path.join(share, "icons", theme)
            os.makedirs(dest, exist_ok=True)
            shutil.copy2(os.path.join(src, "index.theme"), dest)
            if theme == "Adwaita":
                for d in ("symbolic", "symbolic-up-to-32", "scalable"):
                    if os.path.isdir(os.path.join(src, d)):
                        shutil.copytree(os.path.join(src, d), os.path.join(dest, d), dirs_exist_ok=True)

    # ------------------------------------------------------------------ libraries
    def _files(self, d: str):
        for dirpath, _dirs, files in os.walk(d):
            for f in files:
                yield os.path.join(dirpath, f)

    def _wanted(self, soname: str) -> bool:
        return soname not in EXCLUDE

    def add_lib(self, soname: str, path: str = "") -> None:
        if soname in self.libs or not self._wanted(soname):
            return
        if not path:
            for d in (self.syslib, "/lib/" + self.ma, "/usr/lib", "/lib"):
                if os.path.exists(os.path.join(d, soname)):
                    path = os.path.join(d, soname)
                    break
            else:
                log("warning: %s not found" % soname)
                return
        os.makedirs(self.lib, exist_ok=True)
        dest = os.path.join(self.lib, soname)
        shutil.copyfile(os.path.realpath(path), dest)
        os.chmod(dest, 0o755)
        self.libs[soname] = dest
        self.elves.add(dest)

    def closure(self) -> None:
        """Every library the bundled files load, and theirs in turn, except the excluded ones."""
        done: set[str] = set()
        while True:
            todo = sorted(self.elves - done)
            if not todo:
                break
            for f in todo:
                done.add(f)
                r = subprocess.run(["ldd", f], capture_output=True, text=True)
                for line in r.stdout.splitlines():
                    m = re.match(r"\s*(\S+) => (/\S+)", line)
                    if m:
                        self.add_lib(m.group(1), m.group(2))
                    elif "not found" in line:
                        log("warning: %s: %s" % (os.path.relpath(f, self.appdir), line.strip()))
        log("libraries: %d" % len(self.libs))

    def rpaths(self) -> None:
        """$ORIGIN-relative DT_RPATH: unlike RUNPATH it also covers what GModule and GStreamer dlopen()."""
        for f in sorted(self.elves):
            rel = os.path.relpath(self.lib, os.path.dirname(f))
            rpath = "$ORIGIN" if rel == "." else "$ORIGIN/" + rel
            run("patchelf", "--force-rpath", "--set-rpath", rpath, f)

    # ------------------------------------------------------------------ the image's top level
    def top(self) -> None:
        shutil.copy2(os.path.join(ROOT, "packaging", "appimage", "AppRun"), os.path.join(self.appdir, "AppRun"))
        os.chmod(os.path.join(self.appdir, "AppRun"), 0o755)
        share = os.path.join(self.usr, "share")
        desktop = os.path.join(share, "applications", APPID + ".desktop")
        shutil.copy2(desktop, self.appdir)
        icon = os.path.join(share, "icons", "hicolor", "scalable", "apps", APPID + ".svg")
        shutil.copy2(icon, self.appdir)
        if os.path.lexists(os.path.join(self.appdir, ".DirIcon")):
            os.unlink(os.path.join(self.appdir, ".DirIcon"))
        os.symlink(APPID + ".svg", os.path.join(self.appdir, ".DirIcon"))
        # AppImage tools look for the metainfo under its old name
        meta = os.path.join(share, "metainfo", APPID + ".metainfo.xml")
        if os.path.exists(meta):
            shutil.copy2(meta, os.path.join(share, "metainfo", APPID + ".appdata.xml"))

    def compile(self) -> None:
        """Bytecode now: the image is read-only, so Python could not cache it later and would compile every start."""
        run(sys.executable, "-m", "compileall", "-q", "-j0", "--invalidation-mode", "unchecked-hash",
            os.path.join(self.usr, "lib", self.pyver), self.site, stdout=subprocess.DEVNULL)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    if not shutil.which("patchelf"):
        sys.exit("mkappimage.py: patchelf not found")
    b = Bundle(sys.argv[1])
    b.python()
    b.gi()
    b.pypi()
    b.ward()
    b.typelibs()
    b.gstreamer()
    b.nss()
    b.gtk_modules()
    b.data()
    b.closure()
    b.enchant()
    b.rpaths()
    b.top()
    b.compile()


if __name__ == "__main__":
    main()
