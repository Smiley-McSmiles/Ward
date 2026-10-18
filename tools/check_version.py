#!/usr/bin/env python3
"""Check that the version is the same everywhere it is written (CI runs this; a release tag must match too).

    tools/check_version.py [v1.2.3]"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def main() -> int:
    version = re.search(r'^__version__ = "([^"]+)"', read("ward/__init__.py"), re.M).group(1)
    problems = []
    release = re.search(r'<release version="([^"]+)"', read("data/io.github.smiley_mcsmiles.Ward.metainfo.xml"))
    if not release or release.group(1) != version:
        problems.append("metainfo's newest release is %s" % (release.group(1) if release else "missing"))
    man = re.search(r'^\.TH WARD 1 "[^"]*" "Ward v?([^"]+)"', read("data/ward.1"), re.M)
    if not man or man.group(1) != version:
        problems.append("the man page says %s" % (man.group(1) if man else "nothing"))
    if not re.search(r"^## v?%s\b" % re.escape(version), read("CHANGELOG.md"), re.M):
        problems.append("CHANGELOG.md has no section for %s" % version)
    if len(sys.argv) > 1 and sys.argv[1].lstrip("v") != version:
        problems.append("the tag %s does not match" % sys.argv[1])
    for p in problems:
        print("version %s: %s" % (version, p), file=sys.stderr)
    if not problems:
        print("version %s everywhere" % version)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
