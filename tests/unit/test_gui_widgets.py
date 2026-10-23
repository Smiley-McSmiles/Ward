"""Opening attachments: Ward uses the desktop's default handler instead of GTK's app chooser when it can."""

from ward.gui import widgets


class _App:
    def __init__(self, uris: bool = True, files: bool = False):
        self.uris, self.files = uris, files
        self.launched: list[list[str]] = []

    def supports_uris(self) -> bool:
        return self.uris

    def supports_files(self) -> bool:
        return self.files

    def launch_uris(self, uris, _context) -> bool:
        self.launched.append(list(uris))
        return True

    def launch(self, files, _context) -> bool:
        self.launched.append([f.get_uri() for f in files])
        return True


def _no_display(monkeypatch):
    monkeypatch.setattr(widgets.Gdk.Display, "get_default", staticmethod(lambda: None))


def test_launch_with_default_opens_uris(tmp_path, monkeypatch):
    app = _App()
    monkeypatch.setattr(widgets, "_default_app_for", lambda *_a: app)
    _no_display(monkeypatch)
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"\xff\xd8\xff")
    assert widgets._launch_with_default(str(photo)) is True
    assert app.launched and app.launched[0][0].startswith("file://")


def test_launch_with_default_falls_back_without_one(tmp_path, monkeypatch):
    monkeypatch.setattr(widgets, "_default_app_for", lambda *_a: None)
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"\xff\xd8\xff")
    assert widgets._launch_with_default(str(photo)) is False


def test_content_type_prefers_the_attachment_mime_for_a_nameless_cache_file(tmp_path):
    cached = tmp_path / "6f2a1c9d"          # a cache name without an extension
    cached.write_bytes(b"\xff\xd8\xff")
    assert widgets._content_type_for(str(cached), "image/jpeg") == "image/jpeg"
    named = tmp_path / "photo.png"
    named.write_bytes(b"\x89PNG\r\n")
    assert widgets._content_type_for(str(named), "image/jpeg") == "image/png"
