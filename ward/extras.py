"""Python libraries that distributions rarely package, installed by Ward itself.

The OMEMO libraries (python-omemo, twomemo, oldmemo, x3dh, doubleratchet, xeddsa) are in few distributions and in
no BSD ports tree yet, while Ward encrypts by default.  Packages therefore depend on what distributions do ship,
and Ward can fetch the rest with pip into a private folder - "Install encryption support" in the app, or
`install.sh --vendor` for a system-wide copy.  Nothing is installed into the system's site-packages.

Search order: the system's own packages first, then the folder install.sh filled (WARD_VENDOR, set by the
launcher), then the user's folder (~/.local/share/ward/python)."""

from __future__ import annotations

import importlib.metadata
import logging
import os
import re
import shutil
import subprocess
import sys

from . import paths

log = logging.getLogger("ward.extras")

OMEMO = ["omemo>=1.0", "twomemo[xml]>=1.0", "x3dh>=1.0", "doubleratchet>=1.0", "xeddsa>=1.0"]
# AGPL-3.0: only installed when the user asks for compatibility with today's apps (Conversations, Monal, Dino...)
LEGACY_OMEMO = ["oldmemo[xml]>=1.0"]
_OMEMO_NAMES = {"omemo", "twomemo", "oldmemo", "x3dh", "doubleratchet", "xeddsa"}
_XEDDSA_ABI3_PYTHON = "3.13"  # xeddsa 1.2.0 ships cp313 wheels with an abi3 extension and bundled libxeddsa


def user_dir() -> str:
    return os.path.join(paths.data_dir(), "python")


def dirs() -> list[str]:
    out = []
    for d in (os.environ.get("WARD_VENDOR", ""), user_dir()):
        if d and os.path.isdir(d):
            out.append(d)
    return out


def activate() -> None:
    """Make the private folders importable, ahead of the system's site-packages: pip resolved these libraries
    together, and an older system copy of one of their dependencies (protobuf, pydantic, cryptography...) would
    break them.  Ward's own code and the GObject bindings are never in these folders.

    In the AppImage the libraries are already inside the image, built for its Python.  A private folder left by a
    distribution install belongs to a different Python (Void's 3.14, say) and its compiled modules must not shadow
    the image's: it goes after the image's site-packages, not before them."""
    private = dirs()
    for d in reversed(private):
        if d in sys.path:
            sys.path.remove(d)
    if paths.appimage():
        sys.path.extend(d for d in private if d not in sys.path)
        return
    for d in reversed(private):
        sys.path.insert(1 if sys.path and sys.path[0] in ("", os.getcwd()) else 0, d)


def pip_available() -> bool:
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


class InstallError(Exception):
    pass


def _pkg_name(spec: str) -> str:
    return re.split(r"[\[><=!~;\s]", spec, maxsplit=1)[0].lower().replace("_", "-")


def _pkg_extras(spec: str) -> set[str]:
    m = re.match(r"^[^\[><=!~;\s]+\[([^\]]+)\]", spec)
    return {e.strip().lower() for e in m.group(1).split(",") if e.strip()} if m else set()


def _run_pip(args: list[str], on_line=None) -> tuple[int, list[str]]:
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check",
           "--no-warn-script-location", "--no-input", *args]
    env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_ROOT_USER_ACTION="ignore")
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
    return proc.wait(), tail


def _omemo_deps_from_metadata(target: str, requested_extras: dict[str, set[str]]) -> list[str]:
    """Collect transitive requirements of the OMEMO packages installed in target, excluding the OMEMO set itself."""
    deps: list[str] = []
    for dist in importlib.metadata.distributions(path=[target]):
        name = (dist.metadata.get("Name") or "").lower().replace("_", "-")
        if name not in _OMEMO_NAMES:
            continue
        wanted_extras = requested_extras.get(name, set())
        for raw in dist.requires or ():
            req_part, _, marker = raw.partition(";")
            if marker:
                m_extra = re.search(r"""extra\s*==\s*['"]([^'"]+)['"]""", marker)
                if m_extra:
                    if m_extra.group(1).lower() not in wanted_extras:
                        continue
                else:
                    continue
            req = re.sub(r"\s*\(([^)]+)\)", r"\1", req_part).strip()
            if req and _pkg_name(req) not in _OMEMO_NAMES:
                deps.append(req)
    return list(dict.fromkeys(deps))


def _install_with_abi3_xeddsa(packages: list[str], target: str, on_line=None) -> tuple[int, list[str]]:
    """Install xeddsa from its prebuilt abi3 wheel (cp313) on newer Pythons (3.14/3.15+) without compiling."""
    xeddsa_pkgs = [p for p in packages if _pkg_name(p) == "xeddsa"]
    pure_omemo = [p for p in packages if _pkg_name(p) in (_OMEMO_NAMES - {"xeddsa"})]
    other_pkgs = [p for p in packages if _pkg_name(p) not in _OMEMO_NAMES]

    if on_line is not None:
        on_line("No native xeddsa wheel for Python %d.%d; using prebuilt abi3 wheel (cp%s)..." % (
            sys.version_info[0], sys.version_info[1], _XEDDSA_ABI3_PYTHON.replace(".", "")))

    rc, tail = _run_pip(["--target", target, "--only-binary=:all:", "--python-version", _XEDDSA_ABI3_PYTHON,
                         "--no-deps", *xeddsa_pkgs], on_line)
    if rc != 0:
        return rc, tail
    if pure_omemo:
        rc, tail = _run_pip(["--target", target, "--no-deps", *pure_omemo], on_line)
        if rc != 0:
            return rc, tail

    requested_extras = {_pkg_name(p): _pkg_extras(p) for p in packages}
    native_deps = other_pkgs + _omemo_deps_from_metadata(target, requested_extras)
    if native_deps:
        rc, tail = _run_pip(["--target", target, *native_deps], on_line)
        if rc != 0:
            return rc, tail
    return 0, []


def install(packages: list[str], target: str | None = None, on_line=None, clean: bool = False,
            verify: bool = True) -> None:
    """pip install into the private folder (blocking: run it in a worker thread).  on_line(text) gets pip's
    output as it comes.  clean: start from an empty folder, so nothing left from an earlier attempt mixes in."""
    target = target or user_dir()
    if clean and os.path.isdir(target):
        shutil.rmtree(target)
    os.makedirs(target, exist_ok=True)
    has_xeddsa = any(_pkg_name(p) == "xeddsa" for p in packages)
    base_args = ["--target", target]
    if has_xeddsa:
        base_args += ["--only-binary", "xeddsa"]
    rc, tail = _run_pip([*base_args, *packages], on_line)
    if rc != 0 and has_xeddsa:
        rc, tail = _install_with_abi3_xeddsa(packages, target, on_line)
        if rc != 0:
            # Last resort (e.g. BSD/exotic arch with no PyPI wheel but system libxeddsa installed): allow sdist build.
            rc, tail = _run_pip(["--target", target, *packages], on_line)
    if rc != 0:
        raise InstallError("\n".join(tail) or "pip failed")
    if verify:
        activate()
        check(packages)


def check(packages: list[str]) -> None:
    """Import what was installed in a fresh interpreter, the way Ward will after its restart."""
    mods = [p.split("[")[0].split(">")[0].split("=")[0].replace("-", "_") for p in packages]
    mods += ["%s.etree" % m for m in mods if m in ("twomemo", "oldmemo")]
    code = "import ward.extras as e; e.activate(); " + "; ".join("import %s" % m for m in mods)
    if "omemo" in mods:     # what Ward itself loads, so a conflict with another library shows up here too
        code += "; import ward.core.omemo.service"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.environ.get("PYTHONPATH", "")]))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    if r.returncode != 0:
        lines = r.stderr.strip().splitlines() or ["unknown error"]
        log.warning("the installed libraries do not load:\n%s", r.stderr)
        last = lines[-1]
        raise InstallError("Installed, but it does not load: %s" % last)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Install optional Python libraries into Ward's private folder.")
    ap.add_argument("--target", required=True)
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("packages", nargs="+")
    ns = ap.parse_args()
    try:
        install(ns.packages, target=ns.target, on_line=print, verify=not ns.no_verify)
    except InstallError as e:
        print(e, file=sys.stderr)
        raise SystemExit(1)
