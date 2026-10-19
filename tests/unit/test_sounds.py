"""Notification sounds: the theme's sound by name when a sound theme has it, Ward's own copy otherwise (Void)."""

import os

from ward import paths
from ward.gui import app


def test_bundled_sound_is_found():
    path = app.bundled_sound("message-new-instant")
    assert path.endswith("message-new-instant.oga") and os.path.isfile(path)
    assert path.startswith(paths.share_dir())
    with open(path, "rb") as fh:
        assert fh.read(4) == b"OggS"
    assert app.bundled_sound("no-such-sound") == "" and app.bundled_sound("../etc/passwd") == ""


def test_bundled_ringtone_is_found():
    path = app.bundled_sound("phone-incoming-call")
    assert path.endswith("phone-incoming-call.oga") and os.path.isfile(path)
    with open(path, "rb") as fh:
        assert fh.read(4) == b"OggS"


def test_sound_theme_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "system"))
    assert not app.sound_theme_has("message-new-instant")      # like Void: no sound theme at all
    stereo = tmp_path / "system" / "sounds" / "freedesktop" / "stereo"
    stereo.mkdir(parents=True)
    (stereo / "message-new-instant.oga").write_bytes(b"OggS")
    assert app.sound_theme_has("message-new-instant")
    assert not app.sound_theme_has("dialog-information")


def test_play_sound_uses_the_bundled_file_without_a_theme(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "system"))
    played = []

    class Fake:
        def _play_file(self, path):
            played.append(path)

    app.WardApplication.play_sound(Fake(), "message-new-instant")
    assert played and played[0].endswith("message-new-instant.oga")


def test_play_sound_plays_a_chosen_file(tmp_path):
    played = []

    class Fake:
        def _play_file(self, path):
            played.append(path)

    ding = tmp_path / "ding.ogg"
    ding.write_bytes(b"OggS")
    app.WardApplication.play_sound(Fake(), str(ding))
    assert played == [str(ding)]


def test_sound_row_shows_default_or_file_name(display):
    import gi

    gi.require_version("Adw", "1")
    from gi.repository import Adw

    from ward import schema
    from ward.gui import prefs

    Adw.init()
    spec = schema.SPECS["notify.sound_message"]
    assert spec.type == "sound"
    assert prefs.spec_row(spec, spec.default, lambda v: None).get_subtitle() == "Default"
    assert prefs.spec_row(spec, "/home/me/ding.ogg", lambda v: None).get_subtitle() == "ding.ogg"
