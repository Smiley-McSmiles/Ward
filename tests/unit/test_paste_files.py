"""Files copied in a file manager and pasted into the composer are offered for sending, not pasted as file:// text."""

import os

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk

from ward.gui import composer as composer_mod


def test_pasted_file_list_is_offered_for_sending(tmp_path, display):
    f = tmp_path / "notes.txt"
    f.write_text("hi")
    files = Gdk.FileList.new_from_list([Gio.File.new_for_path(str(f))])
    value = GObject.Value(Gdk.FileList, files)
    provider = Gdk.ContentProvider.new_union([
        Gdk.ContentProvider.new_for_value(value),
        Gdk.ContentProvider.new_for_bytes("text/plain;charset=utf-8", GLib.Bytes.new(f.as_uri().encode())),
    ])
    view = Gtk.TextView()
    view.get_clipboard().set_content(provider)

    offered = []

    class Fake:
        def confirm_files(self, paths):
            offered.append(paths)

    view.connect("paste-clipboard", lambda v: composer_mod.Composer._on_paste(Fake(), v))
    view.emit("paste-clipboard")
    ctx = GLib.MainContext.default()
    for _ in range(200):
        if offered:
            break
        ctx.iteration(False)
    assert offered == [[str(f)]]
    assert view.get_buffer().get_property("text") == ""       # no file:// text pasted
    assert os.path.exists(f)
