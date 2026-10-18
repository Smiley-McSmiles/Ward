#!/usr/bin/env python3
"""Packing lists for the BSD ports trees, generated from a staged install.

usage: plist.py <staged-root> <prefix> freebsd|openbsd|netbsd

FreeBSD (pkg-plist) and NetBSD (PLIST) list files relative to PREFIX; FreeBSD ports compress man pages, so they
are listed with .gz.  OpenBSD (pkg/PLIST) lists the directories the package owns too, marks the man page and asks
for the desktop and icon caches to be refreshed."""

from __future__ import annotations

import os
import sys


def main(stage: str, prefix: str, style: str) -> int:
    base = os.path.join(stage, prefix.lstrip("/"))
    files, dirs = [], set()
    for dirpath, dnames, fnames in os.walk(base):
        dnames.sort()
        for f in sorted(fnames):
            rel = os.path.relpath(os.path.join(dirpath, f), base)
            files.append(rel)
            d = os.path.dirname(rel)
            while d:
                dirs.add(d)
                d = os.path.dirname(d)
    out = []
    if style == "freebsd":
        for f in files:
            out.append(f + ".gz" if "/man/man" in "/" + f and not f.endswith(".gz") else f)
    elif style == "netbsd":
        out = ["@comment $NetBSD$", *files]
    elif style == "openbsd":
        out.append("@comment $OpenBSD$")
        owned = {d for d in dirs if d.startswith(("lib/ward", "share/ward"))}
        listed = set()
        for f in files:
            d = os.path.dirname(f)
            parents = []
            while d and d in owned and d not in listed:
                parents.append(d)
                d = os.path.dirname(d)
            for p in reversed(parents):
                out.append(p + "/")
                listed.add(p)
            out.append("@man " + f if f.startswith("man/") else f)
        out.append("@tag update-desktop-database")
        out.append("@tag gtk-update-icon-cache %D/share/icons/hicolor")
    else:
        sys.exit("unknown style: %s" % style)
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    sys.exit(main(*sys.argv[1:]))
