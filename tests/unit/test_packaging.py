"""install.sh staging, the package writers, the .mo compiler and the BSD packing lists."""

import gettext
import json
import os
import shutil
import subprocess
import sys
import tarfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APPID = "io.github.smiley_mcsmiles.Ward"


def _sh() -> str:
    return shutil.which("dash") or shutil.which("sh")


@pytest.fixture(scope="module")
def staged(tmp_path_factory):
    dest = tmp_path_factory.mktemp("stage")
    env = dict(os.environ, DESTDIR=str(dest))
    subprocess.run([_sh(), os.path.join(ROOT, "install.sh"), "--prefix", "/usr", "--no-post"], check=True, env=env,
                   capture_output=True)
    return dest


def test_install_stages_everything(staged):
    usr = staged / "usr"
    for rel in ("bin/ward", "lib/ward/ward/__init__.py", "lib/ward/ward/gui/style.css",
                "lib/ward/ward/core/storage/migrations/0001_init.sql",
                "share/applications/%s.desktop" % APPID, "share/metainfo/%s.metainfo.xml" % APPID,
                "share/icons/hicolor/scalable/apps/%s.svg" % APPID,
                "share/icons/hicolor/symbolic/apps/%s-symbolic.svg" % APPID,
                "share/icons/hicolor/256x256/apps/%s.png" % APPID, "share/man/man1/ward.1",
                "share/bash-completion/completions/ward", "share/zsh/site-functions/_ward",
                "share/fish/vendor_completions.d/ward.fish", "share/ward/icons",
                "share/ward/runit/run", "share/ward/runit/log/run",
                "share/ward/sounds/message-new-instant.oga",
                "share/doc/ward/LICENSE", "share/doc/ward/THIRD_PARTY_NOTICES.md",
                "share/doc/ward/LICENSES/AGPL-3.0.txt",
                "share/doc/ward/LICENSES/sound-theme-freedesktop-CREDITS.txt"):
        assert (usr / rel).exists(), rel
    # the systemd unit is only staged on Linux; the BSDs do not have systemd
    if sys.platform.startswith("linux"):
        assert (usr / "lib/systemd/user/ward.service").exists(), "lib/systemd/user/ward.service"
    assert os.access(usr / "bin/ward", os.X_OK) and os.access(usr / "share/ward/runit/run", os.X_OK)
    assert "exec /usr/bin/ward --background" in (usr / "share/ward/runit/run").read_text()
    if sys.platform.startswith("linux"):
        assert "ExecStart=/usr/bin/ward --background" in (usr / "lib/systemd/user/ward.service").read_text()
    assert not list(usr.rglob("__pycache__"))
    desktop = (usr / ("share/applications/%s.desktop" % APPID)).read_text()
    assert "Exec=/usr/bin/ward %U" in desktop and "x-scheme-handler/xmpp" in desktop
    launcher = (usr / "bin/ward").read_text()
    assert 'PYTHONPATH="/usr/lib/ward' in launcher and "-m ward" in launcher


def test_flatpak_manifest_bundles_appimage_extras():
    """The Flatpak runtime has GStreamer/NSS, but not GtkSourceView's language data or libspelling."""
    with open(os.path.join(ROOT, "packaging/flatpak", "%s.json" % APPID)) as f:
        manifest = json.load(f)
    assert manifest["runtime-version"] == "50"
    modules = {m["name"]: m for m in manifest["modules"] if isinstance(m, dict)}
    assert modules["gtksourceview-data"]["buildsystem"] == "simple"
    assert any("data/language-specs" in command for command in modules["gtksourceview-data"]["build-commands"])
    assert "libspelling" in modules
    assert modules["libspelling"]["buildsystem"] == "meson"
    assert modules["libspelling"]["config-opts"] == ["-Ddocs=false", "-Dvapi=false", "-Dsysprof=false"]
    with open(os.path.join(ROOT, "packaging/flatpak/python3-deps.json")) as f:
        deps = json.load(f)
    assert not any("cp312" in source["url"] for source in deps["sources"])


def test_package_flatpak_creates_its_build_directory():
    with open(os.path.join(ROOT, "package.sh")) as f:
        text = f.read()
    assert 'mkdir -p "$DIST/recipes/flatpak" "$BUILD/flatpak"' in text
    assert 'flatpak-builder --user --force-clean' in text


def test_launcher_runs(staged, tmp_path):
    env = dict(os.environ, WARD_HOME=str(tmp_path), WARD_PYTHON=sys.executable)
    # the launcher points at /usr/lib/ward; run the staged copy through the same wrapper logic
    text = (staged / "usr/bin/ward").read_text().replace("/usr/lib/ward", str(staged / "usr/lib/ward"))
    launcher = tmp_path / "ward"
    launcher.write_text(text)
    launcher.chmod(0o755)
    out = subprocess.run([str(launcher), "--version"], env=env, capture_output=True, text=True, check=True)
    assert out.stdout.startswith("Ward ")


def test_mkdeb(staged, tmp_path):
    root = tmp_path / "deb"
    shutil.copytree(staged, root)
    (root / "DEBIAN").mkdir()
    (root / "DEBIAN/control").write_text("Package: ward\nVersion: 1.0.0\nArchitecture: all\nMaintainer: x <x@y>\n"
                                         "Description: test\n test\n")
    out = tmp_path / "ward.deb"
    subprocess.run([sys.executable, os.path.join(ROOT, "packaging/mkdeb.py"), str(root), str(out)], check=True)
    data = out.read_bytes()
    assert data.startswith(b"!<arch>\ndebian-binary")
    if shutil.which("dpkg-deb"):
        listing = subprocess.run(["dpkg-deb", "-c", str(out)], capture_output=True, text=True, check=True).stdout
        assert "./usr/bin/ward" in listing and "root/root" in listing
        assert subprocess.run(["dpkg-deb", "-f", str(out), "Package"], capture_output=True, text=True).stdout.strip() \
            == "ward"


def test_mkarch(staged, tmp_path):
    out = tmp_path / "ward.pkg.tar.xz"
    subprocess.run([sys.executable, os.path.join(ROOT, "packaging/mkarch.py"), str(staged), str(out), "--name", "ward",
                    "--version", "1.0.0", "--desc", "test", "--url", "https://example.org", "--depend", "python"],
                   check=True, capture_output=True)
    with tarfile.open(out) as t:
        names = t.getnames()
        assert names[0] == ".PKGINFO" and ".MTREE" in names[:3]
        info = t.extractfile(".PKGINFO").read().decode()
        assert "pkgver = 1.0.0-1" in info and "depend = python" in info
        assert "usr/bin/ward" in names
        assert all(m.uid == 0 and m.gid == 0 for m in t.getmembers())


def test_msgfmt(tmp_path):
    po = tmp_path / "de.po"
    po.write_text('msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
                  '"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n\n'
                  'msgid "Send"\nmsgstr "Senden"\n\n'
                  'msgctxt "verb"\nmsgid "Block"\nmsgstr "Blockieren"\n\n'
                  'msgid "%d member"\nmsgid_plural "%d members"\nmsgstr[0] "%d Mitglied"\nmsgstr[1] "%d Mitglieder"\n\n'
                  '#, fuzzy\nmsgid "Quit"\nmsgstr "Beenden?"\n', encoding="utf-8")
    mo = tmp_path / "de.mo"
    subprocess.run([sys.executable, os.path.join(ROOT, "packaging/msgfmt.py"), str(po), str(mo)], check=True)
    with open(mo, "rb") as fh:
        t = gettext.GNUTranslations(fh)
    assert t.gettext("Send") == "Senden"
    assert t.pgettext("verb", "Block") == "Blockieren"
    assert t.ngettext("%d member", "%d members", 1) == "%d Mitglied"
    assert t.ngettext("%d member", "%d members", 5) == "%d Mitglieder"
    assert t.gettext("Quit") == "Quit"       # fuzzy entries are not used


@pytest.mark.parametrize("style", ["freebsd", "openbsd", "netbsd"])
def test_plists(staged, style):
    out = subprocess.run([sys.executable, os.path.join(ROOT, "packaging/plist.py"), str(staged), "/usr", style],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    assert "bin/ward" in out
    if style == "freebsd":
        assert "share/man/man1/ward.1.gz" in out
    if style == "openbsd":
        assert out[0] == "@comment $OpenBSD$" and "lib/ward/" in out and "@tag update-desktop-database" in out
    assert not any(line.startswith("/") for line in out)


def test_extras_dirs(tmp_path, monkeypatch):
    from ward import extras

    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "wardtestmod.py").write_text("X = 1\n")
    monkeypatch.setenv("WARD_VENDOR", str(vendor))
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert str(vendor) in extras.dirs()
    extras.activate()
    assert sys.path[-1] == str(vendor) or str(vendor) in sys.path
    import wardtestmod

    assert wardtestmod.X == 1


def test_version_spellings_per_format():
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    try:
        import set_version
    finally:
        sys.path.pop(0)
    assert set_version.package_versions("1.1.0") == {"deb": "1.1.0", "alpine": "1.1.0", "plain": "1.1.0"}
    assert set_version.package_versions("1.1.0-rc1") == {"deb": "1.1.0~rc1", "alpine": "1.1.0_rc1",
                                                         "plain": "1.1.0rc1"}
    if shutil.which("dpkg"):
        assert subprocess.run(["dpkg", "--compare-versions", "1.1.0~rc1", "lt", "1.1.0"]).returncode == 0


def test_set_version_everywhere(tmp_path):
    for rel in ("ward/__init__.py", "README.md", "CHANGELOG.md", "data/ward.1", "po/ward.pot",
                "data/%s.metainfo.xml" % APPID, "tools/set_version.py", "tools/check_version.py"):
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(os.path.join(ROOT, rel), dest)
    run = [sys.executable, str(tmp_path / "tools/set_version.py")]
    subprocess.run([*run, "9.8.7-rc2", "--note", "Testing"], check=True, capture_output=True)
    subprocess.run([sys.executable, str(tmp_path / "tools/check_version.py"), "v9.8.7-rc2"], check=True,
                   capture_output=True)
    readme = (tmp_path / "README.md").read_text()
    assert "badge/version-9.8.7-rc2-blue.svg" in readme
    assert 'alt="Version 9.8.7-rc2"' in readme
    assert "ward-9.8.7-rc2-x86_64.AppImage" in readme and "ward-9.8.7-rc2.flatpak" in readme
    assert "## v9.8.7-rc2" in (tmp_path / "CHANGELOG.md").read_text()
    assert subprocess.run([*run, "nonsense"], capture_output=True).returncode == 2


# Every Void package name Ward asks for, checked against void-linux/void-packages (srcpkgs/) on 2026-10-19.
# xbps-install refuses a whole install when one name is unknown: a name that is not in this list must be checked
# there before it is added here.
VOID_PACKAGES = {
    "python3", "python3-gobject", "gtk4", "libadwaita", "python3-slixmpp", "python3-markdown-it",
    "python3-cryptography", "gtksourceview5", "libspelling", "libsecret", "python3-Pillow", "python3-mdit-py-plugins",
    "python3-pip", "gnupg", "gst-plugins-good1", "desktop-file-utils", "gtk4-update-icon-cache", "zbar", "libnice",
    "gst-plugins-base1", "gst-plugins-bad1", "gst-plugins-rs1", "xdg-desktop-portal",
    "xdg-desktop-portal-gtk",
}


def test_void_package_names():
    """install.sh, ward --check and the package template asked Void for python3-markdown-it-py and
    python3-linkify-it-py, which Void does not have (markdown-it-py is python3-markdown-it there)."""
    import re

    from ward import deps

    names = set()
    with open(os.path.join(ROOT, "install.sh")) as f:
        text = f.read()
    for m in re.finditer(r'xbps-install\) (?:REQ|CALLS)="([^"]*)"(?:\s*OPT="([^"]*)")?', text):
        for group in m.groups():
            names.update((group or "").split())
    with open(os.path.join(ROOT, "package.sh")) as f:
        names.update(re.search(r'VOID_DEPENDS="([^"]*)"', f.read()).group(1).split())
    names.update(n for v in deps.PACKAGES["xbps"].values() for n in v.split() if n != "-")
    assert "python3-markdown-it" in names and "gst-plugins-bad1" in names     # the parsing found them
    assert names <= VOID_PACKAGES, "not Void package names (or not checked yet): %s" % sorted(names - VOID_PACKAGES)


def _sh_function(name: str) -> str:
    """One shell function from install.sh, to run on its own."""
    import re

    with open(os.path.join(ROOT, "install.sh")) as f:
        text = f.read()
    m = re.search(r"^%s\(\) \{\n.*?^\}\n" % re.escape(name), text, re.S | re.M)
    assert m, name
    return m.group(0)


def _fake_bin(tmp_path, name: str, body: str) -> None:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def test_xbps_install_skips_installed_packages(tmp_path):
    """xbps-install refuses the whole transaction over one package that is already installed ("Package `python3'
    already installed"), so nothing new was installed on a Void machine that already had any of them."""
    log = tmp_path / "log"
    _fake_bin(tmp_path, "xbps-query", 'case "$1" in python3|gtk4) exit 0 ;; *) exit 2 ;; esac\n')
    _fake_bin(tmp_path, "xbps-install", 'echo "$@" >> "%s"\n' % log)
    script = _sh_function("xbps_install") + 'AS_ROOT=""\nxbps_install python3 libnice gtk4 gst-plugins-bad1\n' \
        'xbps_install python3 gtk4\n'
    env = dict(os.environ, PATH="%s:%s" % (tmp_path, os.environ["PATH"]))
    subprocess.run([_sh(), "-c", script], check=True, env=env)
    assert log.read_text().split("\n")[0].split() == ["-y", "libnice", "gst-plugins-bad1"]
    assert len(log.read_text().strip().splitlines()) == 1         # nothing left to install: not run at all


def test_calls_reason_on_void(tmp_path):
    _fake_bin(tmp_path, "xbps-query", 'echo libnice-0.1.22_1\n')
    script = "pm_name() { echo xbps-install; }\n" + _sh_function("calls_reason") + "calls_reason\n"
    env = dict(os.environ, PATH="%s:%s" % (tmp_path, os.environ["PATH"]))
    out = subprocess.run([_sh(), "-c", script], check=True, env=env, capture_output=True, text=True).stdout
    assert "built without webrtcbin" in out and "(0.1.22)" in out and "0.1.23" in out


def test_check_explains_gstreamer_without_webrtcbin(monkeypatch):
    """ward --check offered "xbps-install -S gst-plugins-bad1 libnice gst-plugins-good1" on Void although they
    were installed: Void's gst-plugins-bad1 has no webrtcbin, since its libnice is older than GStreamer needs."""
    from ward import deps

    monkeypatch.setattr(deps, "_calls_missing", lambda: ["webrtcbin"])
    monkeypatch.setattr(deps, "package_manager", lambda: "xbps")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "libnice-0.1.22_1\n", ""))
    f = deps._calls_feature()
    assert not f.available and f.hint == ""
    assert "built without webrtcbin" in f.detail and "0.1.22" in f.detail
    monkeypatch.setattr(deps, "_calls_missing", lambda: ["webrtcbin", "nicesrc"])
    assert "xbps-install" in deps._calls_feature().hint          # really missing packages: the usual hint


# ---------------------------------------------------------------------------------------------- AppImage
def _mkappimage():
    import importlib.util

    spec = importlib.util.spec_from_file_location("mkappimage", os.path.join(ROOT, "packaging", "mkappimage.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/self/fd")
def test_apprun_environment(tmp_path):
    """AppRun points GTK, GStreamer, GdkPixbuf and Enchant into the image, tells Ward how to start it again and leaves
    LD_LIBRARY_PATH alone, so the programs Ward starts get the system's libraries."""
    app = tmp_path / "Ward.AppDir"
    lib = app / "usr" / "lib" / "x86_64-linux-gnu"
    (lib / "girepository-1.0").mkdir(parents=True)
    (lib / "enchant-2").mkdir()
    (lib / "gdk-pixbuf-2.0" / "2.10.0").mkdir(parents=True)
    (lib / "gdk-pixbuf-2.0" / "2.10.0" / "loaders.cache.in").write_text('"@APPDIR@/usr/lib/svg.so"\n')
    (app / "usr" / "bin").mkdir(parents=True)
    out = tmp_path / "out"
    _fake_bin(app / "usr" / "bin", "python3", 'for v in WARD_APPIMAGE WARD_SHARE_DIR GI_TYPELIB_PATH GTK_PATH '
              'GST_PLUGIN_SYSTEM_PATH_1_0 GST_PLUGIN_SCANNER_1_0 GDK_PIXBUF_MODULE_FILE PYTHONPATH '
              'LD_LIBRARY_PATH; do\n'
              '    eval "echo $v=\\${$v-unset}"\ndone > "%s"\necho "fd9=$(readlink /proc/$$/fd/9)" >> "%s"\n'
              'echo "args=$*" >> "%s"\n' % (out, out, out))
    shutil.copy(os.path.join(ROOT, "packaging", "appimage", "AppRun"), app / "AppRun")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path),
           "APPIMAGE": "/home/me/Ward-1.0.AppImage", "PYTHONPATH": "/somewhere/else"}
    subprocess.run([_sh(), str(app / "AppRun"), "--background"], check=True, env=env)
    got = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert got["args"] == "-m ward --background"
    assert got["WARD_APPIMAGE"] == "/home/me/Ward-1.0.AppImage"
    assert got["WARD_SHARE_DIR"] == "%s/usr/share/ward" % app
    assert got["GI_TYPELIB_PATH"] == "%s/girepository-1.0" % lib
    assert got["GST_PLUGIN_SYSTEM_PATH_1_0"] == "%s/gstreamer-1.0" % lib
    assert got["GTK_PATH"] == "%s/gtk-4.0" % lib
    assert got["PYTHONPATH"] == got["LD_LIBRARY_PATH"] == "unset"
    assert got["fd9"] == str(lib / "enchant-2")
    with open(got["GDK_PIXBUF_MODULE_FILE"]) as f:
        assert f.read() == '"%s/usr/lib/svg.so"\n' % app


def test_apprun_finds_certificates():
    """The bundled OpenSSL looks where Debian keeps certificates; the other systems' places are tried in turn."""
    with open(os.path.join(ROOT, "packaging", "appimage", "AppRun")) as f:
        text = f.read()
    for path in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/ca-bundle.pem",
                 "/etc/ssl/cert.pem"):
        assert path in text


def test_appimage_enchant_looks_in_descriptor_9(tmp_path):
    """Enchant only loads providers from the folder it was built for: the bundled copy is pointed at the one AppRun
    opens as descriptor 9, with the string's length kept."""
    mk = _mkappimage()
    b = mk.Bundle.__new__(mk.Bundle)
    b.ma = "x86_64-linux-gnu"
    b.lib = str(tmp_path)
    (tmp_path / "enchant-2").mkdir()
    lib = tmp_path / "libenchant-2.so.2"
    before = b"\x7fELF...\0/usr/lib/x86_64-linux-gnu/enchant-2\0/usr/share/enchant-2\0"
    lib.write_bytes(before)
    b.libs = {"libenchant-2.so.2": str(lib)}
    b.enchant()
    after = lib.read_bytes()
    assert len(after) == len(before)
    assert b"\0/proc/self/fd/9\0" in after and b"/usr/lib/x86_64-linux-gnu/enchant-2" not in after
    assert b"/usr/share/enchant-2\0" in after


def test_appimage_bundles_nss_crypto_modules(tmp_path):
    """libsrtp2 is built against NSS, which loads libsoftokn3/libfreebl3 by name: ldd never sees them, and without
    them SRTP cannot start, so calls connect but carry no audio."""
    mk = _mkappimage()
    b = mk.Bundle.__new__(mk.Bundle)
    b.syslib = str(tmp_path / "sys")
    b.lib = str(tmp_path / "lib")
    b.elves = set()
    os.makedirs(b.syslib)
    for name in ("libsoftokn3.so", "libsoftokn3.chk", "libfreebl3.so", "libfreebl3.chk", "libnssckbi.so"):
        (tmp_path / "sys" / name).write_bytes(b"x")
    b.nss()
    assert (tmp_path / "lib" / "libsoftokn3.so").exists()
    assert (tmp_path / "lib" / "libfreebl3.so").exists()
    assert (tmp_path / "lib" / "libnssckbi.so").exists()
    assert (tmp_path / "lib" / "libsoftokn3.chk").exists()
    assert str(tmp_path / "lib" / "libsoftokn3.so") in b.elves


def test_appimage_bundles_no_system_libraries():
    mk = _mkappimage()
    for soname in ("libc.so.6", "libGL.so.1", "libEGL.so.1", "libX11.so.6", "libfontconfig.so.1", "libstdc++.so.6"):
        assert soname in mk.EXCLUDE
    # the small X11 libraries are not in AppImage's excludelist: GTK needs them, minimal systems lack them
    assert "libXi.so.6" not in mk.EXCLUDE and "libXcursor.so.1" not in mk.EXCLUDE


def test_appimage_has_no_package_manager(monkeypatch):
    """Inside the AppImage the system's packages add nothing: no "sudo apt install" hints, and --check says where
    Ward runs from."""
    from ward import deps

    monkeypatch.setenv("WARD_APPIMAGE", "/home/me/Ward.AppImage")
    deps.package_manager.cache_clear()
    try:
        assert deps.package_manager() is None
        assert deps.install_hint("spelling") == ""
        assert "Package manager: AppImage" in deps.report()
    finally:
        monkeypatch.delenv("WARD_APPIMAGE")
        deps.package_manager.cache_clear()


def test_appimage_does_not_let_an_older_vendor_folder_shadow_it(tmp_path, monkeypatch):
    """A distribution install's ~/.local/share/ward/python belongs to another Python (Void's 3.14, say); in the
    AppImage it must not shadow the image's 3.12 OMEMO libraries, or the window says encryption is not installed."""
    from ward import extras

    monkeypatch.setenv("WARD_APPIMAGE", "/home/me/Ward.AppImage")
    monkeypatch.delenv("WARD_VENDOR", raising=False)
    vendor = tmp_path / "ward" / "python"
    vendor.mkdir(parents=True)
    monkeypatch.setattr(extras, "user_dir", lambda: str(vendor))
    saved = sys.path[:]
    try:
        extras.activate()
        assert sys.path[-1] == str(vendor)          # after the image's own site-packages
        assert str(vendor) != sys.path[0]
    finally:
        sys.path[:] = saved


@pytest.mark.skipif(not shutil.which("bash"), reason="package.sh is a bash script")
def test_package_sh_builds_appimages():
    out = subprocess.run(["bash", os.path.join(ROOT, "package.sh"), "--help"], check=True, capture_output=True,
                         text=True).stdout
    assert "--appimage" in out
    with open(os.path.join(ROOT, "package.sh")) as f:
        text = f.read()
    assert "flatpak appimage recipes; do" in text and "appimage) build_appimage ;;" in text
