"""Small helpers shared by the core and the interface (no GTK here)."""

from __future__ import annotations

import json
import os
import secrets
import string
import tempfile
import time
from typing import Any


def read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        # keep the broken file for inspection instead of silently overwriting it
        try:
            os.replace(path, path + ".broken")
        except OSError:
            pass
        return default


def write_json(path: str, data: Any, mode: int = 0o600) -> None:
    """Atomic write: a crash never leaves a half-written settings file behind."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def random_resource(prefix: str = "ward") -> str:
    """A resource like 'ward.k3f9': unique per install, but says nothing about the machine."""
    alphabet = string.ascii_lowercase + string.digits
    return "%s.%s" % (prefix, "".join(secrets.choice(alphabet) for _ in range(4)))


def random_id(n: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def now() -> float:
    return time.time()


def human_size(n: float | None) -> str:
    if n is None:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            if unit == "B":
                return "%d %s" % (n, unit)
            return "%.1f %s" % (n, unit) if n < 10 else "%.0f %s" % (n, unit)
        n /= 1000.0
    return "%.0f TB" % n


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def bare(jid: str) -> str:
    return jid.split("/", 1)[0].lower() if jid else ""


def resource(jid: str) -> str:
    return jid.split("/", 1)[1] if jid and "/" in jid else ""


def localpart(jid: str) -> str:
    b = jid.split("/", 1)[0]
    return b.split("@", 1)[0] if "@" in b else b


def domain(jid: str) -> str:
    b = jid.split("/", 1)[0]
    return b.split("@", 1)[1].lower() if "@" in b else b.lower()


def valid_bare_jid(jid: str) -> bool:
    """A cheap sanity check for user input; the server is the real judge."""
    if not jid or len(jid) > 3071 or "/" in jid or any(c.isspace() for c in jid):
        return False
    if jid.count("@") > 1:
        return False
    local, _, dom = jid.rpartition("@")
    if "@" in jid and not local:
        return False
    if not dom or dom.startswith(".") or dom.endswith(".") or ".." in dom:
        return False
    return all(c not in dom for c in "\"&'/:<>@") and all(c not in local for c in "\"&'/:<>@")


def first_line(text: str, limit: int = 120) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""
