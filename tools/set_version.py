#!/usr/bin/env python3
"""Set the version everywhere it is written (the counterpart of check_version.py).

    tools/set_version.py 1.1.0 [--note "what changed"]      (package.sh --version 1.1.0 does this first)

Pre-releases are written like 1.1.0-rc1 (also -alpha2, -beta1).  Updates ward/__init__.py, the README (title and the
file names in the install table), the man page's .TH line, the .pot header, and, when the version is new, a release
entry in the metainfo and a section in CHANGELOG.md, both with the note (default "Release X.Y.Z"): the GitHub
release notes come from CHANGELOG.md, so edit that section afterwards.
"""

from __future__ import annotations

import os
import re
import sys
import time

VERSION_RE = r"\d+\.\d+\.\d+(?:-(?:rc|alpha|beta|pre|dev)\d*)?"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
META = "data/io.github.smiley_mcsmiles.Ward.metainfo.xml"


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def write(rel: str, text: str) -> None:
    with open(os.path.join(ROOT, rel), "w", encoding="utf-8") as fh:
        fh.write(text)


def sub(rel: str, pattern: str, repl, count: int = 1, flags: int = 0) -> None:
    text = read(rel)
    new, n = re.subn(pattern, repl, text, count=count, flags=flags)
    if n == 0:
        raise SystemExit("set_version: could not find the version in %s" % rel)
    write(rel, new)


def package_versions(version: str) -> dict[str, str]:
    """The version as each package format spells it.  deb and rpm sort "~" before the release (1.1.0~rc1 < 1.1.0);
    Alpine and Gentoo use "_rc1"; Arch, Void and the BSDs take no "-" in a version."""
    m = re.fullmatch(r"(\d+\.\d+\.\d+)(?:-(\w+))?", version)
    if not m:
        raise SystemExit("set_version: %s is not a version" % version)
    base, pre = m.group(1), m.group(2)
    if not pre:
        return {"deb": base, "alpine": base, "plain": base}
    return {"deb": "%s~%s" % (base, pre), "alpine": "%s_%s" % (base, pre), "plain": base + pre}


def main(argv: list[str]) -> int:
    args = argv[1:]
    note = None
    if "--note" in args:
        i = args.index("--note")
        note = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    if len(args) != 1 or not re.fullmatch(VERSION_RE, args[0].lstrip("v")):
        print("usage: set_version.py MAJOR.MINOR.PATCH[-rc1] [--note TEXT]", file=sys.stderr)
        return 2
    version = args[0].lstrip("v")
    old = re.search(r'^__version__ = "([^"]+)"', read("ward/__init__.py"), re.M).group(1)
    pv, opv = package_versions(version), package_versions(old)
    today = time.strftime("%Y-%m-%d")

    sub("ward/__init__.py", r'^__version__ = "[^"]+"', '__version__ = "%s"' % version, flags=re.M)
    sub("data/ward.1", r'^\.TH WARD 1 "[^"]*" "Ward v?[^"]+"',
        '.TH WARD 1 "%s" "Ward v%s"' % (time.strftime("%B %Y"), version), flags=re.M)
    sub("po/ward.pot", r'"Project-Id-Version: Ward [^\\"]+', '"Project-Id-Version: Ward %s' % version)

    readme = read("README.md")
    readme = readme.replace("badge/version-%s-blue.svg" % old, "badge/version-%s-blue.svg" % version)
    readme = readme.replace('alt="Version %s"' % old, 'alt="Version %s"' % version)
    for a, b in (("ward-%s.flatpak" % old, "ward-%s.flatpak" % version),
                 ("ward-%s-x86_64.AppImage" % old, "ward-%s-x86_64.AppImage" % version),
                 ("ward_%s_all.deb" % opv["deb"], "ward_%s_all.deb" % pv["deb"]),
                 ("ward-%s-1.noarch.rpm" % opv["deb"], "ward-%s-1.noarch.rpm" % pv["deb"]),
                 ("ward-%s-1-any.pkg.tar.xz" % opv["plain"], "ward-%s-1-any.pkg.tar.xz" % pv["plain"])):
        readme = readme.replace(a, b)
    write("README.md", readme)

    meta = read(META)
    block = re.search(r'    <release version="%s".*?</release>\n' % re.escape(version), meta, re.S)
    if block and not meta.startswith(block.group(0), meta.index("  <releases>\n") + len("  <releases>\n")):
        # back to a version listed further down (a release after its release candidate): newest first again
        meta = meta.replace(block.group(0), "", 1).replace("  <releases>\n", "  <releases>\n" + block.group(0), 1)
        write(META, meta)
    if not block:
        entry = ('    <release version="%s" date="%s"%s>\n      <description>\n        <p>%s</p>\n'
                 '      </description>\n    </release>\n') % (
                     version, today, ' type="development"' if "-" in version else "",
                     note or "Release %s" % version)
        write(META, meta.replace("  <releases>\n", "  <releases>\n" + entry, 1))
        print("added release %s to the metainfo" % version)

    changelog = read("CHANGELOG.md")
    if not re.search(r"^## v?%s\b" % re.escape(version), changelog, re.M):
        section = "## v%s (%s)\n\n%s\n\n" % (version, today, note or "Release %s" % version)
        changelog = re.sub(r"^(## )", section.replace("\\", "\\\\") + r"\1", changelog, count=1, flags=re.M)
        write("CHANGELOG.md", changelog)
        print("added a CHANGELOG.md section for %s - write its notes" % version)
    print("version set to %s" % version)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
