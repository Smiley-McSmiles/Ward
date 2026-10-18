#!/usr/bin/env python3
"""Generate Ward's icons: the symbolic action icons (ward/gui/icons) and the app icon (data/icons).

The SVGs are small hand-written shapes (16x16 for symbolic icons, as GTK recolours them); run this after editing
a shape.  PNG sizes of the app icon are rendered with GdkPixbuf (librsvg) when available."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACTIONS = os.path.join(ROOT, "ward", "gui", "icons", "hicolor", "scalable", "actions")
APPID = "io.github.smiley_mcsmiles.Ward"

SHIELD = "M8 1 2 3.2V7.5c0 3.4 2.6 6.4 6 7.5 3.4-1.1 6-4.1 6-7.5V3.2z"
SYMBOLIC = {
    "lock": '<path d="M8 1a4 4 0 0 0-4 4v2H3v8h10V7h-1V5a4 4 0 0 0-4-4zm0 2a2 2 0 0 1 2 2v2H6V5a2 2 0 0 1 2-2z"/>',
    "lock-open": '<path d="M8 1a4 4 0 0 0-4 4h2a2 2 0 0 1 4 0v2H3v8h10V7h-1V5a4 4 0 0 0-4-4z"/>',
    "shield": '<path d="%s" fill-opacity=".35"/><path d="M8 1 2 3.2V7.5c0 3.4 2.6 6.4 6 7.5 3.4-1.1 6-4.1 6-7.5V3.2zm0 '
              '2.1 4 1.5v2.9c0 2.4-1.7 4.6-4 5.4-2.3-.8-4-3-4-5.4V4.6z"/>' % SHIELD,
    "shield-verified": '<path d="%s"/><path d="m11 5.5-3.8 4-2.2-2.1-1 1.1 3.2 3 4.8-5z" fill="#fff"/>' % SHIELD,
    "shield-warning": '<path d="%s"/><path d="M7 4h2v5H7zm0 6h2v2H7z" fill="#fff"/>' % SHIELD,
    "shield-off": '<path d="%s" fill-opacity=".5"/><path d="M1.3 1.3 14.7 14.7l-1 1L.3 2.3z"/>' % SHIELD,
    "reply": '<path d="M6 3 1 7.5 6 12V9c3 0 5.5.8 8 4-.7-4.2-3.3-7-8-7.6z"/>',
    "react": '<path d="M7 1a6 6 0 1 0 6 6h-1.5A4.5 4.5 0 1 1 7 2.5zm-2 4a1 1 0 1 0 0 2 1 1 0 0 0 0-2zm4 0a1 1 0 1 0 0 2 '
             '1 1 0 0 0 0-2zM4.2 8.5a3 3 0 0 0 5.6 0zM12 1v2h-2v1.5h2v2h1.5v-2h2V3h-2V1z"/>',
    "attach": '<path d="M10.5 1A3.5 3.5 0 0 0 7 4.5v7a1 1 0 0 0 2 0V5H7.5v6.5a.5.5 0 0 1-1 0v-7a2 2 0 1 1 4 0V12a3.5 '
              '3.5 0 0 1-7 0V5H2v7a5 5 0 0 0 10 0V4.5A3.5 3.5 0 0 0 10.5 1z" transform="rotate(30 8 8)"/>',
    "send": '<path d="M1 1.5 15 8 1 14.5 2.5 9 10 8 2.5 7z"/>',
    "members": '<path d="M6 2a3 3 0 1 0 0 6 3 3 0 0 0 0-6zm5.5 1a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zM6 9c-3 0-5 1.5-5 '
               '3.5V14h10v-1.5C11 10.5 9 9 6 9zm5.6.1c1 .8 1.4 1.9 1.4 3.4V14h3v-1.5c0-1.8-1.8-3.2-4.4-3.4z"/>',
    "group": '<path d="M8 1.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zm-5 2a2 2 0 1 0 0 4 2 2 0 0 0 0-4zm10 0a2 2 0 1 0 0 '
             '4 2 2 0 0 0 0-4zM8 7.5c-2.5 0-4 1.3-4 3V13h8v-2.5c0-1.7-1.5-3-4-3zM3 8.5c-1.6 0-3 .9-3 2.3V13h3zm10 '
             '0V13h3v-2.2c0-1.4-1.4-2.3-3-2.3z"/>',
    "contacts": '<path d="M3 1v14h10a1 1 0 0 0 1-1V2a1 1 0 0 0-1-1zm5 3a2 2 0 1 1 0 4 2 2 0 0 1 0-4zM5 11.5C5 10 6.3 '
                '9 8 9s3 1 3 2.5V12H5zM1 3h2v1.5H1zm0 4h2v1.5H1zm0 4h2v1.5H1z"/>',
    "chat": '<path d="M2 2h12a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H6l-4 3v-3H2a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z"/>',
    "pin": '<path d="m9.5 1-1 1 1 1.5-3 3-3-.5-1.5 1.5 3 3L1 15l4.5-4 3 3L10 12.5 9.5 9.5l3-3L14 7.5l1-1z"/>',
    "muted": '<path d="M8 1a1 1 0 0 0-1 1v.6C4.7 3.1 3.5 5 3.5 7.5V11L2 12.5V13h8.3L3.1 5.8C3 6.3 3.5 2 8 2.5zm4.5 '
             '6.5c0-1-.2-1.9-.6-2.7l.6.6zM6.5 14a1.5 1.5 0 0 0 3 0z"/><path d="M1.3 1.3 14.7 14.7l-1 1L.3 2.3z"/>',
    "check": '<path d="m13.3 3.3-7.3 7.4-3.3-3.3-1.4 1.4L6 13.5l8.7-8.8z"/>',
    "check-double": '<path d="m11.3 3.3-6.3 6.4-2.3-2.3-1.4 1.4L5 12.5l7.7-7.8zm3 0L8.5 9.2l1.4 1.4 5.8-5.9z"/>',
    "clock": '<path d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1zm0 1.5a5.5 5.5 0 1 1 0 11 5.5 5.5 0 0 1 0-11zM7.2 4v4.4l3.2 '
             '2.1.8-1.2-2.5-1.7V4z"/>',
    "error": '<path d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1zM7 4h2v5H7zm0 6h2v2H7z"/>',
    "server": '<path d="M2 2h12v5H2zm0 7h12v5H2zm2-5v1h1V4zm0 7v1h1v-1z"/>',
    "qr": '<path d="M1 1h6v6H1zm1.5 1.5v3h3v-3zM9 1h6v6H9zm1.5 1.5v3h3v-3zM1 9h6v6H1zm1.5 1.5v3h3v-3zM3.5 3.5h1v1h-1zm8 '
          '0h1v1h-1zm-8 8h1v1h-1zM9 9h2v2H9zm2 2h2v2h-2zm2-2h2v2h-2zm-4 4h2v2H9zm4 0h2v2h-2z"/>',
    "emoji": '<path d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1zm0 1.5a5.5 5.5 0 1 1 0 11 5.5 5.5 0 0 1 0-11zM5.5 5a1 1 0 1 0 0 '
             '2 1 1 0 0 0 0-2zm5 0a1 1 0 1 0 0 2 1 1 0 0 0 0-2zM4.5 9a3.5 3.5 0 0 0 7 0z"/>',
    "mic": '<path d="M8 1a2.5 2.5 0 0 0-2.5 2.5v4a2.5 2.5 0 0 0 5 0v-4A2.5 2.5 0 0 0 8 1zM3 7a5 5 0 0 0 4.2 4.9V14H5v1.5h6V14'
           'H8.8v-2.1A5 5 0 0 0 13 7h-1.5a3.5 3.5 0 0 1-7 0z"/>',
    "markdown": '<path d="M1 3h14v10H1zm1.5 1.5v7h2V7.3L6 9.3l1.5-2v4.2h2v-7h-2L6 6.5 4.5 4.5zm9 0v4H10L12.5 12 15 8.5'
                'h-1.5v-4z" fill-rule="evenodd"/>',
    "key": '<path d="M5 6a4 4 0 1 0 3.9 4.9H10V12h2v-1.1h1V9.1H8.9A4 4 0 0 0 5 6zm0 2.5a1.5 1.5 0 1 1 0 3 1.5 1.5 0 0 1 '
           '0-3z" transform="rotate(-45 8 8)"/>',
    "edited": '<path d="M11.5 1.5 14.5 4.5 5 14H2v-3zM10 4.6 11.4 6 12.4 5 11 3.6z"/>',
}

APP = """<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="sh" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#e8e7e4"/>
    </linearGradient>
    <linearGradient id="bu" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#4a90e2"/><stop offset="1" stop-color="#1a5fb4"/>
    </linearGradient>
  </defs>
  <!-- the shield fills the icon; a soft edge and shadow keep it visible on light backgrounds -->
  <path d="M64 10 14 28v34c0 31 21.5 56 50 64 28.5-8 50-33 50-64V28z" fill="#000" fill-opacity=".12"
        transform="translate(0 3)"/>
  <path d="M64 8 14 26v34c0 31 21.5 56 50 64 28.5-8 50-33 50-64V26z" fill="url(#sh)" stroke="#9a9996"
        stroke-width="2" stroke-opacity=".6"/>
  <path d="M34 40h60a9 9 0 0 1 9 9v30a9 9 0 0 1-9 9H66l-19 14V88h-13a9 9 0 0 1-9-9V49a9 9 0 0 1 9-9z"
        fill="url(#bu)"/>
  <circle cx="46" cy="64" r="6.5" fill="#fff"/><circle cx="64" cy="64" r="6.5" fill="#fff"/>
  <circle cx="82" cy="64" r="6.5" fill="#fff"/>
</svg>
"""

APP_SYMBOLIC = ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
                '<path fill="#2e3436" d="M8 1 2 3.2V7.5c0 3.4 2.6 6.4 6 7.5 3.4-1.1 6-4.1 6-7.5V3.2zM5 5h6a1 1 0 0 1 1 '
                '1v3a1 1 0 0 1-1 1H8l-2 1.5V10H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z" fill-rule="evenodd"/></svg>\n')


def main():
    os.makedirs(ACTIONS, exist_ok=True)
    for name, body in SYMBOLIC.items():
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
               '<g fill="#2e3436">%s</g></svg>\n' % body)
        with open(os.path.join(ACTIONS, "ward-%s-symbolic.svg" % name), "w") as fh:
            fh.write(svg)
    data = os.path.join(ROOT, "data", "icons", "hicolor")
    for sub in ("scalable/apps", "symbolic/apps"):
        os.makedirs(os.path.join(data, sub), exist_ok=True)
    with open(os.path.join(data, "scalable", "apps", APPID + ".svg"), "w") as fh:
        fh.write(APP)
    with open(os.path.join(data, "symbolic", "apps", APPID + "-symbolic.svg"), "w") as fh:
        fh.write(APP_SYMBOLIC)
    # the app icon in the window's icon theme too, for running from the source tree
    os.makedirs(os.path.join(ROOT, "ward", "gui", "icons", "hicolor", "scalable", "apps"), exist_ok=True)
    with open(os.path.join(ROOT, "ward", "gui", "icons", "hicolor", "scalable", "apps", APPID + ".svg"), "w") as fh:
        fh.write(APP)
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf

        src = os.path.join(data, "scalable", "apps", APPID + ".svg")
        for size in (16, 32, 48, 64, 128, 256, 512):
            d = os.path.join(data, "%dx%d" % (size, size), "apps")
            os.makedirs(d, exist_ok=True)
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(src, size, size)
            pb.savev(os.path.join(d, APPID + ".png"), "png", [], [])
    except Exception as e:  # PNGs are a nicety; the SVG is what desktops use
        print("PNG sizes not rendered:", e, file=sys.stderr)


if __name__ == "__main__":
    main()
