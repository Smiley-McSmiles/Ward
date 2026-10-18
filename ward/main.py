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
    p.add_argument("--debug", action="store_true", help="verbose logging to the terminal")
    p.add_argument("--version", action="version", version="%s %s" % (APP_NAME, __version__))
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    args = _parser().parse_args(argv[1:])
    from . import i18n, log

    i18n.setup()
    log.setup("verbose" if args.debug else "off", to_file=False if args.debug else None)
    if args.check:
        from . import deps

        print(deps.report())
        return 1 if deps.missing_required() else 0
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
    if os.environ.get("WARD_RENDERER"):
        os.environ.setdefault("GSK_RENDERER", os.environ["WARD_RENDERER"])
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
