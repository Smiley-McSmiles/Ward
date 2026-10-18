#!/usr/bin/env python3
"""Build a .deb from a staged directory tree without dpkg-deb, so packages build on any distribution.

usage: mkdeb.py <staged-root-with-DEBIAN-dir> <output.deb>

A .deb is an ar archive of debian-binary, control.tar.gz and data.tar.xz.  Every entry is owned by root, and
SOURCE_DATE_EPOCH (when set) makes the result reproducible."""

from __future__ import annotations

import gzip
import io
import lzma
import os
import sys
import tarfile
import time


def _norm(now: int):
    def f(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = "root"
        ti.mtime = now
        return ti
    return f


def _tar(base: str, now: int, skip_top: str | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        tf.add(base, arcname=".", filter=_norm(now), recursive=False)
        for dirpath, dirs, files in os.walk(base):
            dirs.sort()
            if dirpath == base and skip_top:
                dirs[:] = [d for d in dirs if d != skip_top]
            for name in sorted(dirs + files):
                full = os.path.join(dirpath, name)
                tf.add(full, arcname="./" + os.path.relpath(full, base), filter=_norm(now), recursive=False)
    return buf.getvalue()


def _ar_member(name: str, data: bytes, now: int) -> bytes:
    hdr = "%-16s%-12d%-6d%-6d%-8s%-10d`\n" % (name, now, 0, 0, "100644", len(data))
    out = hdr.encode() + data
    if len(data) % 2:
        out += b"\n"
    return out


def build(root: str, out: str) -> None:
    control_dir = os.path.join(root, "DEBIAN")
    if not os.path.isfile(os.path.join(control_dir, "control")):
        sys.exit("no DEBIAN/control in %s" % root)
    now = int(os.environ.get("SOURCE_DATE_EPOCH", time.time()))
    control = gzip.compress(_tar(control_dir, now), compresslevel=9, mtime=now)
    data = lzma.compress(_tar(root, now, skip_top="DEBIAN"), preset=6)
    with open(out, "wb") as fh:
        fh.write(b"!<arch>\n")
        fh.write(_ar_member("debian-binary", b"2.0\n", now))
        fh.write(_ar_member("control.tar.gz", control, now))
        fh.write(_ar_member("data.tar.xz", data, now))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    build(sys.argv[1], sys.argv[2])
