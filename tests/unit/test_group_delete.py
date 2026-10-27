"""Deleting a group removes the room from Ward, not just from the server."""

import asyncio
import sqlite3
from types import SimpleNamespace

from ward.core.chats import Chats, chat_attachment_paths


def _db(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")  # the app turns this on (storage/db.py)
    conn.execute("CREATE TABLE chats (id INTEGER PRIMARY KEY, account_id INTEGER, jid TEXT,"
                 " type TEXT, name TEXT)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY,"
                 " chat_id INTEGER REFERENCES chats(id) ON DELETE CASCADE)")
    conn.execute("CREATE VIRTUAL TABLE messages_fts USING fts5 (text)")
    conn.execute("CREATE TABLE attachments (id INTEGER PRIMARY KEY, message_id INTEGER,"
                 " local_path TEXT, thumbnail TEXT)")
    conn.execute("INSERT INTO chats (id, account_id, jid, type, name) VALUES (1, 1, 'room@x', 'groupchat', 'Room')")
    conn.execute("INSERT INTO chats (id, account_id, jid, type, name) VALUES (2, 1, 'other@x', 'chat', 'Other')")
    for mid, chat_id in ((10, 1), (11, 1), (20, 2)):
        conn.execute("INSERT INTO messages (id, chat_id) VALUES (?, ?)", (mid, chat_id))
        conn.execute("INSERT INTO messages_fts (rowid, text) VALUES (?, ?)", (mid, "hello"))
    conn.execute("INSERT INTO attachments (message_id, local_path, thumbnail) VALUES (10, ?, ?)",
                 (str(tmp_path / "photo.jpg"), str(tmp_path / "thumb.jpg")))
    conn.execute("INSERT INTO attachments (message_id, local_path, thumbnail) VALUES (20, ?, '')",
                 (str(tmp_path / "other.jpg"),))
    conn.commit()
    return conn


class _Store:
    def __init__(self, items):
        self.items = list(items)

    def get_n_items(self):
        return len(self.items)

    def get_item(self, i):
        return self.items[i]

    def remove(self, i):
        self.items.pop(i)


def test_chat_attachment_paths_covers_every_message(tmp_path):
    conn = _db(tmp_path)
    assert sorted(chat_attachment_paths(conn, 1)) == [str(tmp_path / "photo.jpg"),
                                                      str(tmp_path / "thumb.jpg")]
    assert chat_attachment_paths(conn, 99) == []


def test_delete_removes_the_chat_its_history_and_its_media(tmp_path):
    conn = _db(tmp_path)
    chat = SimpleNamespace(id=1, account_id=1, jid="room@x", type="groupchat")
    forgotten, emitted = [], []

    class FakeDB:
        async def run(self, job):
            return job(conn)

    fake = SimpleNamespace(
        db=FakeDB(), core=SimpleNamespace(files=SimpleNamespace(forget=forgotten.append)),
        by_id={1: chat}, by_key={(1, "room@x", "groupchat"): chat}, lists={1: object()},
        store=_Store([chat]), emit=lambda name, c: emitted.append(name))
    asyncio.run(Chats.delete(fake, chat))

    assert conn.execute("SELECT COUNT(*) FROM chats WHERE id = 1").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = 1").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM messages_fts WHERE rowid = 10").fetchone()[0] == 0
    # the other chat is untouched
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = 2").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM messages_fts WHERE rowid = 20").fetchone()[0] == 1
    assert sorted(forgotten[0]) == sorted([str(tmp_path / "photo.jpg"), str(tmp_path / "thumb.jpg")])
    assert emitted == ["removed"]
    assert fake.store.get_n_items() == 0


def test_destroy_drops_the_local_chat_after_the_server_iq():
    from ward.core.muc import MucService

    calls = []
    chat = SimpleNamespace(jid="room@x", id=1)

    class FakeSession:
        async def iq(self, typ, jid, payload):
            calls.append(("iq", typ, jid))

    async def leave(c, reason=""):
        calls.append(("leave", c.jid))

    async def delete(c):
        calls.append(("delete", c.jid))

    fake = SimpleNamespace(
        core=SimpleNamespace(messaging=SimpleNamespace(session_for=lambda c: FakeSession()),
                             chats=SimpleNamespace(delete=delete)),
        leave=leave)
    asyncio.run(MucService.destroy(fake, chat, "bye"))

    assert calls == [("iq", "set", "room@x"), ("leave", "room@x"), ("delete", "room@x")]
