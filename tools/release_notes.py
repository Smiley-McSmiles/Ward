#!/usr/bin/env python3
"""Print the CHANGELOG.md section of one version (for the GitHub release text).

    tools/release_notes.py 1.0.0"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    version = sys.argv[1].lstrip("v") if len(sys.argv) > 1 else ""
    with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    out, inside = [], False
    for line in lines:
        if re.match(r"^## v?\d+\.\d+\.\d+\b", line):
            if inside:
                break
            inside = line[3:].split()[0].lstrip("v") == version
            continue
        if inside:
            out.append(line)
    if not out:
        print("no CHANGELOG.md section for %r" % version, file=sys.stderr)
        return 1
    print("\n".join(out).strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
