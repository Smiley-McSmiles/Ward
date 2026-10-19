"""install.sh staging, the package writers, the .mo compiler and the BSD packing lists."""

import gettext
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
                "share/ward/sounds/message-new-instant.oga"):
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
    assert "## v9.8.7-rc2" in (tmp_path / "CHANGELOG.md").read_text()
    assert subprocess.run([*run, "nonsense"], capture_output=True).returncode == 2
