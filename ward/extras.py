"""Python libraries that distributions rarely package, installed by Ward itself.

The OMEMO libraries (python-omemo, twomemo, oldmemo, x3dh, doubleratchet, xeddsa) are in few distributions and in
no BSD ports tree yet, while Ward encrypts by default.  Packages therefore depend on what distributions do ship,
and Ward can fetch the rest with pip into a private folder - "Install encryption support" in the app, or
`install.sh --vendor` for a system-wide copy.  Nothing is installed into the system's site-packages.

Search order: the system's own packages first, then the folder install.sh filled (WARD_VENDOR, set by the
launcher), then the user's folder (~/.local/share/ward/python)."""

from __future__ import annotations

import os
import subprocess
import sys

from . import paths

OMEMO = ["omemo>=1.0", "twomemo>=1.0", "x3dh>=1.0", "doubleratchet>=1.0", "xeddsa>=1.0"]
# AGPL-3.0: only installed when the user asks for compatibility with today's apps (Conversations, Monal, Dino...)
LEGACY_OMEMO = ["oldmemo>=1.0"]
QR = ["segno"]


def user_dir() -> str:
    return os.path.join(paths.data_dir(), "python")


def dirs() -> list[str]:
    out = []
    for d in (os.environ.get("WARD_VENDOR", ""), user_dir()):
        if d and os.path.isdir(d):
            out.append(d)
    return out


def activate() -> None:
    """Make the private folders importable (after the system's packages, which always win)."""
    for d in dirs():
        if d not in sys.path:
            sys.path.append(d)


def pip_available() -> bool:
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


class InstallError(Exception):
    pass


def install(packages: list[str], target: str | None = None, on_line=None) -> None:
    """pip install into the private folder (blocking: run it in a worker thread).  on_line(text) gets pip's
    output as it comes."""
    target = target or user_dir()
    os.makedirs(target, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--target", target, "--disable-pip-version-check",
           "--no-warn-script-location", "--no-input", *packages]
    env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    except OSError as e:
        raise InstallError(str(e)) from e
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        tail = [*tail, line][-12:]
        if on_line is not None:
            on_line(line)
    if proc.wait() != 0:
        raise InstallError("\n".join(tail) or "pip failed")
    activate()
    import importlib

    importlib.invalidate_caches()
