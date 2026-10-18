#!/usr/bin/env python3
"""Write packaging/flatpak/python3-deps.json: Ward's Python dependencies pinned with their sha256, for an offline
Flatpak build (flatpak-builder has no network while building).

    python3 tools/flatpak_deps.py [--python 3.12]

pip resolves the dependency tree for each Flatpak architecture (x86_64, aarch64) from wheels only; compiled wheels
become arch-specific sources, pure-Python ones are shared.  The GNOME runtime already has PyGObject."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIREMENTS = ["slixmpp>=1.8", "markdown-it-py>=2", "mdit-py-plugins", "linkify-it-py", "cryptography",
                "pillow", "omemo>=1.0", "twomemo[xml]>=1.0", "oldmemo[xml]>=1.0", "x3dh>=1.0", "doubleratchet>=1.0",
                "xeddsa>=1.0"]
ARCHES = {"x86_64": "manylinux_2_28_x86_64", "aarch64": "manylinux_2_28_aarch64"}
# also accept older manylinux tags, which pip only matches when listed explicitly
EXTRA_TAGS = {"x86_64": ["manylinux2014_x86_64", "manylinux_2_17_x86_64"],
              "aarch64": ["manylinux2014_aarch64", "manylinux_2_17_aarch64"]}


def download(arch: str, python: str, dest: str) -> list[str]:
    cmd = [sys.executable, "-m", "pip", "download", "--only-binary=:all:", "--python-version", python,
           "--implementation", "cp", "--dest", dest, "--quiet"]
    for tag in [ARCHES[arch], *EXTRA_TAGS[arch]]:
        cmd += ["--platform", tag]
    cmd += REQUIREMENTS
    subprocess.run(cmd, check=True)
    return sorted(os.listdir(dest))


def pypi_url(filename: str) -> tuple[str, str]:
    """(url, sha256) of a file on PyPI, found through the JSON API of its project and version."""
    name, version = filename.split("-")[:2]
    with urllib.request.urlopen("https://pypi.org/pypi/%s/%s/json" % (name, version), timeout=30) as r:
        data = json.load(r)
    for f in data["urls"]:
        if f["filename"] == filename:
            return f["url"], f["digests"]["sha256"]
    raise SystemExit("%s is not on PyPI" % filename)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default="3.12", help="the runtime's Python version")
    a = ap.parse_args()
    per_arch: dict[str, set[str]] = {}
    for arch in ARCHES:
        with tempfile.TemporaryDirectory() as d:
            per_arch[arch] = set(download(arch, a.python, d))
    every = set.union(*per_arch.values())
    sources = []
    for fn in sorted(every):
        url, sha = pypi_url(fn)
        src = {"type": "file", "url": url, "sha256": sha}
        arches = [arch for arch, files in per_arch.items() if fn in files]
        if len(arches) != len(ARCHES):
            src["only-arches"] = arches
        sources.append(src)
    names = sorted({r.split(">")[0].split("=")[0] for r in REQUIREMENTS} | {"xmlschema"})
    module = {
        "name": "python3-ward-deps",
        "buildsystem": "simple",
        "build-commands": ["pip3 install --verbose --exists-action=i --no-index --find-links=\"file://${PWD}\" "
                           "--prefix=${FLATPAK_DEST} --no-build-isolation " + " ".join(names)],
        "sources": sources,
    }
    out = os.path.join(ROOT, "packaging", "flatpak", "python3-deps.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(module, fh, indent=2)
        fh.write("\n")
    print("wrote %s (%d files)" % (out, len(sources)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
