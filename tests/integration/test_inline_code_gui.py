"""Inline code click hit-testing: Pango's layout coordinates are in Pango units, not pixels."""

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import GLib, Gtk, Pango

from ward.core.markup import parse_markdown
from ward.gui import render

pytestmark = [pytest.mark.integration, pytest.mark.gui]


def _settle() -> None:
    loop = GLib.MainLoop()
    GLib.timeout_add(250, loop.quit)
    loop.run()


def test_code_at_uses_pixel_coordinates(display):
    doc = parse_markdown("click `hello` now")
    renderer = render.Renderer(on_code_copy=lambda _text: None)
    box = renderer.build(doc)
    window = Gtk.Window()
    window.set_child(box)
    window.set_default_size(500, 200)
    window.present()
    try:
        _settle()
        label = box.get_first_child()
        ranges = render.inline_code_ranges(doc.blocks[0].inlines)
        layout = label.get_layout()
        rect = layout.index_to_pos(6)                 # the "h" in "hello"
        ox, oy = label.get_layout_offsets()
        x = ox + rect.x // Pango.SCALE + rect.width // (2 * Pango.SCALE)
        y = oy + rect.y // Pango.SCALE + rect.height // (2 * Pango.SCALE)
        assert renderer._code_at(label, ranges, x, y) == "hello"
        assert renderer._code_at(label, ranges, 2, 5) is None
    finally:
        window.destroy()
