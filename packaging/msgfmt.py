#!/usr/bin/env python3
"""Compile a .po file into a .mo file without gettext's msgfmt (some BSD and minimal systems lack it).

usage: msgfmt.py input.po output.mo

Handles msgctxt, plurals and multi-line strings; fuzzy entries are skipped like msgfmt does."""

from __future__ import annotations

import ast
import struct
import sys


def parse(path: str) -> dict[bytes, bytes]:
    messages: dict[bytes, bytes] = {}
    entry: dict[str, object] = {}
    section = None
    fuzzy = False

    def flush():
        nonlocal entry, fuzzy
        if "msgid" in entry and not fuzzy:
            ctx = entry.get("msgctxt")
            key = entry["msgid"]
            if "msgid_plural" in entry:
                key = key + "\0" + entry["msgid_plural"]
                strs = entry.get("msgstr_plural", {})
                value = "\0".join(strs[i] for i in sorted(strs))
            else:
                value = entry.get("msgstr", "")
            if ctx is not None:
                key = ctx + "\x04" + key
            if value or key == "":
                messages[key.encode("utf-8")] = value.encode("utf-8")
        entry = {}
        fuzzy = False

    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if line.startswith("#,") and "fuzzy" in line:
                if "msgid" in entry:
                    flush()
                fuzzy = True
                continue
            if not line or line.startswith("#"):
                if not line and "msgid" in entry and section and section.startswith("msgstr"):
                    flush()
                continue
            if line.startswith("msgctxt"):
                if "msgid" in entry:
                    flush()
                section = "msgctxt"
                entry["msgctxt"] = ast.literal_eval(line[7:].strip())
            elif line.startswith("msgid_plural"):
                section = "msgid_plural"
                entry["msgid_plural"] = ast.literal_eval(line[12:].strip())
            elif line.startswith("msgid"):
                if "msgid" in entry and section != "msgctxt":
                    flush()
                section = "msgid"
                entry["msgid"] = ast.literal_eval(line[5:].strip())
            elif line.startswith("msgstr["):
                idx = int(line[7:line.index("]")])
                section = "msgstr[%d]" % idx
                entry.setdefault("msgstr_plural", {})[idx] = ast.literal_eval(line[line.index("]") + 1:].strip())
            elif line.startswith("msgstr"):
                section = "msgstr"
                entry["msgstr"] = ast.literal_eval(line[6:].strip())
            elif line.startswith('"'):
                text = ast.literal_eval(line)
                if section and section.startswith("msgstr["):
                    idx = int(section[7:-1])
                    entry["msgstr_plural"][idx] += text
                elif section:
                    entry[section] = entry.get(section, "") + text
    flush()
    return messages


def write(messages: dict[bytes, bytes], path: str) -> None:
    keys = sorted(messages)
    ids = b""
    strs = b""
    offsets = []
    for k in keys:
        offsets.append((len(ids), len(k), len(strs), len(messages[k])))
        ids += k + b"\0"
        strs += messages[k] + b"\0"
    n = len(keys)
    key_start = 7 * 4 + 16 * n
    value_start = key_start + len(ids)
    koffsets, voffsets = [], []
    for o1, l1, o2, l2 in offsets:
        koffsets += [l1, o1 + key_start]
        voffsets += [l2, o2 + value_start]
    header = struct.pack("Iiiiiii", 0x950412DE, 0, n, 7 * 4, 7 * 4 + n * 8, 0, 0)
    with open(path, "wb") as fh:
        fh.write(header)
        fh.write(struct.pack("%di" % len(koffsets), *koffsets))
        fh.write(struct.pack("%di" % len(voffsets), *voffsets))
        fh.write(ids)
        fh.write(strs)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    write(parse(sys.argv[1]), sys.argv[2])
