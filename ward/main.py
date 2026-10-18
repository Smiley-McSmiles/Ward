"""Command line entry point.

    ward                      open the window (or bring the running one to front)
    ward xmpp:alex@example.org?message
    ward --background         start hidden, stay connected (used by autostart)
    ward --quit               quit the running instance
    ward --check              report libraries and optional features, then exit
    ward --debug              log everything to the terminal
"""

from __future__ import annotations

import argparse
import os
import sys

from . import APP_NAME, __version__


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ward", description="%s, an XMPP client" % APP_NAME)
    p.add_argument("uris", nargs="*", metavar="URI", help="xmpp: links to open")
    p.add_argument("-b", "--background", action="store_true", help="start without a window")
    p.add_argument("-q", "--quit", action="store_true", help="quit the running instance")
    p.add_argument("--check", action="store_true", help="show which libraries and features are available")
    p.add_argument("--memory-report", action="store_true", help="show where the running Ward's memory goes")
    p.add_argument("--debug", action="store_true", help="verbose logging to the terminal")
    p.add_argument("--version", action="version", version="%s v%s" % (APP_NAME, __version__))
    return p


RENDERERS = {"opengl": "ngl", "software": "cairo"}


def _choose_renderer() -> None:
    """GTK's renderer, chosen before GTK starts: GSK_RENDERER from the environment wins, then WARD_RENDERER, then the
    preference.  GTK picks Vulkan by default where it can, which on many drivers costs hundreds of MB more than
    OpenGL for an app like this one, so Ward asks for OpenGL unless told otherwise."""
    if os.environ.get("GSK_RENDERER"):
        _software_only(os.environ["GSK_RENDERER"])
        return
    choice = os.environ.get("WARD_RENDERER", "")
    if not choice:
        try:
            from .settings import Settings

            choice = Settings()["advanced.renderer"]
        except Exception:
            choice = "opengl"
    renderer = RENDERERS.get(choice, choice if choice not in ("auto", "") else "")
    if renderer:
        os.environ["GSK_RENDERER"] = renderer
        _software_only(renderer)


def _software_only(renderer: str) -> None:
    """With the software renderer, keep GDK from setting up OpenGL and Vulkan anyway: the graphics driver it loads
    for them costs 100 MB or more even when nothing is drawn with it."""
    if renderer != "cairo":
        return
    for var, flags in (("GDK_DISABLE", "gl,vulkan"), ("GDK_DEBUG", "gl-disable,vulkan-disable")):
        old = os.environ.get(var, "")
        os.environ[var] = "%s,%s" % (old, flags) if old else flags


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    args = _parser().parse_args(argv[1:])
    from . import extras, i18n, log

    extras.activate()

    i18n.setup()
    log.setup("verbose" if args.debug else "off", to_file=False if args.debug else None)
    if args.check:
        from . import deps

        print(deps.report())
        return 1 if deps.missing_required() else 0
    if args.memory_report:
        from . import memreport

        return memreport.run()
    _choose_renderer()
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk  # noqa: F401
    except (ImportError, ValueError) as e:
        from . import deps

        sys.stderr.write("Ward needs GTK 4 and libadwaita with their Python bindings (%s).\n%s\n"
                         % (e, deps.install_hint("gtk", "adw", "gi")))
        return 1
    from . import deps

    missing = deps.missing_required()
    if missing:
        sys.stderr.write("Missing: %s\n%s\n" % (", ".join(f.title for f in missing),
                                                deps.install_hint(*[c for f in missing for c in f.components])))
        return 1
    from . import aio
    from .gui.app import WardApplication

    loop = aio.install()
    app = WardApplication()
    forwarded = [argv[0]] + (["--background"] if args.background else []) + (["--quit"] if args.quit else []) + \
        list(args.uris)
    with loop.running():
        status = app.run(forwarded)
    return status


if __name__ == "__main__":
    sys.exit(main())
