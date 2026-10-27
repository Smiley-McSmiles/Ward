"""Deleting a message takes its media with it: the cached file, its thumbnail, and the image bubble."""

import os
import sqlite3

from ward.core.chats import cached_attachment_paths
from ward.core.files import FileService


def test_cached_attachment_paths_returns_media_and_thumbnail(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE attachments (id INTEGER PRIMARY KEY, message_id INTEGER,"
                 " local_path TEXT, thumbnail TEXT)")
    conn.execute("INSERT INTO attachments (message_id, local_path, thumbnail) VALUES (7, ?, ?)",
                 (str(tmp_path / "photo.jpg"), str(tmp_path / "thumb.jpg")))
    conn.execute("INSERT INTO attachments (message_id, local_path, thumbnail) VALUES (8, ?, ?)",
                 (str(tmp_path / "other.jpg"), ""))
    assert cached_attachment_paths(conn, 7) == [str(tmp_path / "photo.jpg"), str(tmp_path / "thumb.jpg")]
    assert cached_attachment_paths(conn, 9) == []


def test_forget_unlinks_only_files_in_the_cache(tmp_path, monkeypatch):
    from ward import paths

    cache = tmp_path / "cache"
    (cache / "files").mkdir(parents=True)
    (cache / "thumbnails").mkdir(parents=True)
    inside = cache / "files" / "photo.jpg"
    thumb = cache / "thumbnails" / "thumb.jpg"
    outside = tmp_path / "documents" / "mine.jpg"
    outside.parent.mkdir()
    for p in (inside, thumb, outside):
        p.write_bytes(b"x")
    monkeypatch.setattr(paths, "cache_dir", lambda: str(cache))

    FileService.forget(object(), [str(inside), str(thumb), str(outside), ""])

    assert not inside.exists() and not thumb.exists()
    assert outside.exists(), "a file that is not in the cache must never be deleted"


def test_retracted_message_draws_no_attachment():
    import gi

    gi.require_version("Gtk", "4.0")

    from ward.core.models import AttachmentItem, ChatItem, MessageItem
    from ward.gui.message_row import MessageRow
    from ward.settings import Settings

    class FakeView:
        def __init__(self):
            self.settings = Settings()
            self.settings["appearance.layout"] = "bubbles"
            self.settings["appearance.avatars"] = "never"
            self.chat = ChatItem(id=1, type="chat")
            self.first_unread_id = 0
            self.highlight_id = 0

        def _layout_changing(self):
            pass

        def previous(self, _m):
            return None

        def display_name(self, m):
            return "Sam"

        def avatar_path(self, _m):
            return ""

        def mention_names(self):
            return set()

        def on_link(self, *_a):
            pass

    calls = []

    class CountingRow(MessageRow):
        def _attachment(self, m, att):
            calls.append(att.id)
            return super()._attachment(m, att)

    att = AttachmentItem(id=1, url="https://upload.example/x.jpg", name="x.jpg", mime="image/jpeg",
                         state="done")
    row = CountingRow(FakeView())
    row.bind(MessageItem(id=1, chat_id=1, timestamp=1000, direction=0, sender="sam@example.org",
                         body="", kind="file", attachments=[att]), None)
    assert calls == [1], "a live message shows its image"

    calls.clear()
    row.bind(MessageItem(id=1, chat_id=1, timestamp=1000, direction=0, sender="sam@example.org",
                         body="", kind="file", retracted=1, attachments=[att]), None)
    assert calls == [], "a deleted message must not draw the image bubble"
