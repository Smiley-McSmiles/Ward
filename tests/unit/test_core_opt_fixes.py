"""Regression and optimization tests for core-opt fixes across core, DB, markup, aio, and services."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import struct
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ward import aio, util
from ward.core import chat as chatmod  # noqa: F401
from ward.core import chats as chatsmod
from ward.core import http as httpmod
from ward.core import images, markup, search
from ward.core import proxy as proxymod
from ward.core.chat import ChatService
from ward.core.chats import Chats, find_message_job, message_from_row
from ward.core.history import NS_MAM, HistoryService
from ward.core.markup import parse as parse_markup
from ward.core.markup import parse_markdown, parse_styling, styling, to_plain
from ward.core.markup import tree as markup_tree
from ward.core.markup.emoji import emoji_only
from ward.core.markup.plain import preview_text
from ward.core.models import AttachmentItem, ChatItem, MessageItem, MessageList
from ward.core.muc import MucService, Room
from ward.core.notify import Notifier
from ward.core.session import AccountSession
from ward.core.storage.db import Database, latest_version


class _DummySettings(dict):
    def __init__(self, data=None):
        super().__init__(data or {})

    def get(self, key, default=None):
        return dict.get(self, key, default)

    def __getitem__(self, key):
        return dict.get(self, key)


class _DummyNames:
    def sender_name(self, chat, m):
        return m.sender or "Sender"

    def reactor_name(self, chat, sender):
        return sender


class _DummyFiles:
    def forget(self, paths_):
        pass


class _DummyCore(SimpleNamespace):
    def is_active(self, chat):
        return self.active_chat is chat and self.window_visible


def _make_core(db):
    core = _DummyCore(
        db=db,
        settings=_DummySettings({"chats.markdown_others": False}),
        names=_DummyNames(),
        files=_DummyFiles(),
        active_chat=None,
        window_visible=True,
        window_focused=True,
    )
    core.my_reaction_id = lambda chat: "me@example.com"
    core.set_active_chat = lambda c: setattr(core, "active_chat", c)
    return core


class ModelsAndChatsTests(unittest.TestCase):
    def test_item_init_props_and_signals(self):
        m = MessageItem(id=7, chat_id=2, body="hello", direction=1, edited=True)
        self.assertEqual(m.id, 7)
        self.assertEqual(m.get_property("body"), "hello")
        self.assertEqual(m.kind, "text")
        self.assertEqual(m.sender, "")
        self.assertEqual(m.retracted, 0)
        self.assertFalse(m.mentions_me)

        # Lazy containers must not live in __dict__ until needed.
        self.assertNotIn("_reactions", m.__dict__)
        self.assertNotIn("_attachments", m.__dict__)
        self.assertNotIn("_extra", m.__dict__)

        seen = []
        m.connect("notify::body", lambda obj, _pspec: seen.append(obj.body))
        m.update(body="updated")
        self.assertEqual(seen, ["updated"])

        # Setting empty containers clears backing keys cleanly.
        m.extra = {"k": 1}
        self.assertIn("_extra", m.__dict__)
        m.extra = {}
        self.assertNotIn("_extra", m.__dict__)

        # Boolean vs int type safety: False/True/int bounds
        m2 = MessageItem(id=8, direction=True)  # bool is subclass of int; fallback setter converts safely
        self.assertEqual(m2.direction, 1)

        att = AttachmentItem(id=3, aes_key="", aes_iv="", sha256="")
        self.assertNotIn("aes_key", att.__dict__)
        self.assertEqual(att.aes_key, "")

    def test_message_list_dedup_remove_trim_and_find_ref(self):
        chat = ChatItem(id=1)
        ml = MessageList(chat)
        m1 = MessageItem(id=1, chat_id=1, timestamp=100, stanza_id="s1")
        m2 = MessageItem(id=2, chat_id=1, timestamp=200, origin_id="o2")
        m3 = MessageItem(id=3, chat_id=1, timestamp=300, msg_id="m3")
        ml.add_many([m1, m2, m2, m1])
        self.assertEqual([m.id for m in ml], [1, 2])
        ml.add_many([m3, m2])  # tail splice append path
        self.assertEqual([m.id for m in ml], [1, 2, 3])
        self.assertEqual([m.id for m in reversed(ml)], [3, 2, 1])

        self.assertIs(ml.find_ref("s1"), m1)
        self.assertIs(ml.find_ref("o2"), m2)
        self.assertIs(ml.find_ref("m3"), m3)
        self.assertIsNone(ml.find_ref(""))

        # Simulate stale timestamp key outside bisect index -> linear fallback still cleans store.
        m2.timestamp = 999
        ml.remove(2)
        self.assertIsNone(ml.get(2))
        self.assertEqual([m.id for m in ml], [1, 3])

        # Negative keep clamps to 0 cleanly without slicing python negatives.
        ml.trim(-5)
        self.assertEqual(len(ml), 0)
        self.assertEqual(ml.by_id, {})
        self.assertEqual(ml._keys, [])
        self.assertTrue(ml.has_older)

    def test_chats_lru_trim_inactive_and_delete_cleanup(self):
        async def run():
            db = Database(":memory:")
            db.open()
            try:
                aid = await db.insert("accounts", {"jid": "me@example.com", "created_at": 1})
                core = _make_core(db)
                chats = Chats(core)
                created = []
                for idx in range(1, 17):
                    c = await chats.get_or_create(aid, f"c{idx}@example.com", "chat")
                    created.append(c)
                core.set_active_chat(created[0])
                for idx, c in enumerate(created):
                    ml = chats.message_list(c)
                    ml.add_many([
                        MessageItem(id=idx * 200 + k, chat_id=c.id, timestamp=k * 1000)
                        for k in range(1, 140)
                    ])

                # Re-touch another chat triggers LRU eviction past MAX_CACHED_LISTS while protecting active_chat.
                chats.message_list(created[-1])
                self.assertIn(created[0].id, chats.lists)
                self.assertIn(created[-1].id, chats.lists)
                self.assertLessEqual(len(chats.lists), chats.MAX_CACHED_LISTS)

                # Inactive lists get trimmed to INACTIVE_TRIM_MESSAGES while active keeps all 139.
                self.assertEqual(len(chats.lists[created[0].id]), 139)
                for cid, ml in chats.lists.items():
                    if cid != created[0].id and cid != created[-1].id:
                        self.assertLessEqual(len(ml), chats.INACTIVE_TRIM_MESSAGES)

                # show_message caps background open lists when exceeding INACTIVE_TRIM_THRESHOLD
                bg = created[5]
                bg_ml = chats.message_list(bg)
                bg_ml.add_many([MessageItem(id=50_000 + k, chat_id=bg.id, timestamp=k * 1000) for k in range(1, 151)])
                chats.show_message(bg, MessageItem(id=60_000, chat_id=bg.id, timestamp=999_000, body="hi"))
                self.assertLessEqual(len(bg_ml), chats.INACTIVE_TRIM_MESSAGES)

                # Draft timers and active_chat are cleaned on delete; message_from_row keeps extra lazy on "{}".
                row_plain = {
                    "id": 99,
                    "chat_id": created[0].id,
                    "stanza_id": "",
                    "origin_id": "",
                    "msg_id": "",
                    "sender": "bob@example.com",
                    "sender_jid": "",
                    "occupant_id": "",
                    "direction": 0,
                    "timestamp": 1000,
                    "body": "hi",
                    "is_markdown": 0,
                    "kind": "text",
                    "encryption": "",
                    "sender_device": None,
                    "trust_at_receipt": "",
                    "edited": 0,
                    "retracted": 0,
                    "reply_to": "",
                    "status": 0,
                    "error": "",
                    "extra": "{}",
                }
                plain_msg = message_from_row(row_plain)
                self.assertNotIn("_extra", plain_msg.__dict__)
                loop = asyncio.get_running_loop()
                chats._draft_timers[created[0].id] = loop.call_later(60, lambda: None)
                await chats.delete(created[0])
                self.assertIsNone(core.active_chat)
                self.assertNotIn(created[0].id, chats._draft_timers)
            finally:
                db.close()

        asyncio.run(run())


class DbAndSearchTests(unittest.TestCase):
    def test_wal_read_lane_pragmas_indexes_and_concurrency(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "ward.db")
                db = Database(path)
                db.open()
                try:
                    self.assertIsNotNone(db._read_thread)
                    jm = await db.scalar("PRAGMA journal_mode")
                    ts = await db.scalar("PRAGMA temp_store")
                    cs = await db.scalar("PRAGMA cache_size")
                    self.assertEqual(str(jm).lower(), "wal")
                    self.assertEqual(ts, 2)
                    self.assertEqual(cs, -8192)

                    r_qo = await db.scalar_read("PRAGMA query_only")
                    r_ts = await db.scalar_read("PRAGMA temp_store")
                    self.assertEqual(r_qo, 1)
                    self.assertEqual(r_ts, 2)
                    with self.assertRaises(sqlite3.OperationalError):
                        await db.run_read(
                            lambda conn: conn.execute("INSERT INTO accounts (jid, created_at) VALUES ('x', 1)")
                        )

                    # Migrations applied including 0003_indexes.sql
                    v = await db.scalar("SELECT MAX(version) FROM schema_version")
                    self.assertGreaterEqual(v, 3)
                    self.assertEqual(v, latest_version())
                    idxs = {
                        r["name"]
                        for r in await db.fetchall_read("SELECT name FROM sqlite_master WHERE type = 'index'")
                    }
                    for name in (
                        "messages_chat_time_id",
                        "messages_chat_preview",
                        "messages_outbox",
                        "messages_msg_id_only",
                        "messages_chat_unread",
                        "messages_timestamp",
                        "message_edits_message",
                    ):
                        self.assertIn(name, idxs)

                    # Seed messages with duplicate timestamps + IDs + FTS
                    aid = await db.insert("accounts", {"jid": "me@example.com", "created_at": 1})
                    cid = await db.insert("chats", {"account_id": aid, "jid": "bob@example.com", "type": "chat"})
                    for i in range(1, 11):
                        ts_val = 5000 if i <= 5 else 6000
                        await db.run(
                            lambda conn, i=i, ts_val=ts_val: chatsmod.insert_message_job(
                                conn,
                                {
                                    "chat_id": cid,
                                    "stanza_id": f"st-{i}",
                                    "origin_id": f"or-{i}",
                                    "msg_id": f"mg-{i}",
                                    "sender": "bob@example.com" if i % 2 else "me@example.com",
                                    "direction": 0 if i % 2 else 1,
                                    "timestamp": ts_val,
                                    "body": f"needle word-{i}",
                                    "kind": "text",
                                },
                            )
                        )

                    # UNION ALL find_message_job matches exact sender/direction filter across duplicate refs.
                    def check_find(conn):
                        r1 = find_message_job(conn, cid, "or-4", sender="me@example.com", direction=1)
                        r2 = find_message_job(conn, cid, "or-4", sender="bob@example.com", direction=0)
                        plan = conn.execute(
                            "EXPLAIN QUERY PLAN SELECT * FROM messages WHERE id = ("
                            "SELECT MAX(id) FROM ("
                            "SELECT id FROM messages WHERE chat_id = ? AND stanza_id = ? "
                            "UNION ALL SELECT id FROM messages WHERE chat_id = ? AND origin_id = ? "
                            "UNION ALL SELECT id FROM messages WHERE chat_id = ? AND msg_id = ?))",
                            (cid, "or-4", cid, "or-4", cid, "or-4"),
                        ).fetchall()
                        return r1, r2, [row["detail"] for row in plan]

                    r1, r2, details = await db.run_read(check_find)
                    self.assertIsNotNone(r1)
                    self.assertEqual(r1["id"], 4)
                    self.assertIsNone(r2)
                    self.assertTrue(any("INDEX" in d for d in details), details)

                    # Row-value pagination on equal timestamps must return all 10 messages without gaps/dups.
                    core = _make_core(db)
                    chats = Chats(core)
                    await chats.load()
                    chat = chats.by_id[cid]
                    self.assertTrue(chat.preview.startswith("needle word-10"))
                    p1 = await chats.load_page(chat, limit=4)
                    p2 = await chats.load_page(chat, before=p1[0], limit=4)
                    p3 = await chats.load_page(chat, before=p2[0], limit=4)
                    got_ids = [m.id for m in (p3 + p2 + p1)]
                    self.assertEqual(got_ids, list(range(1, 11)))

                    # Read lane must complete while the write worker is blocked!
                    gate = threading.Event()
                    started = threading.Event()

                    def slow_write(_conn):
                        started.set()
                        gate.wait(timeout=5)
                        return "wrote"

                    write_fut = db.run(slow_write)
                    await asyncio.to_thread(started.wait, 2)
                    hits = await asyncio.wait_for(search.search(db, "needle"), timeout=1.0)
                    self.assertEqual(len(hits), 10)
                    gate.set()
                    self.assertEqual(await write_fut, "wrote")
                finally:
                    db.close()

        asyncio.run(run())


class MarkupTests(unittest.TestCase):
    def test_plain_fast_paths_and_tree_optimizations(self):
        # Slots on render tree nodes
        doc = parse_markdown("Plain sentence with https://example.org and ||secret||", nicknames={"alex"})
        self.assertFalse(hasattr(doc, "__dict__"))
        self.assertFalse(hasattr(doc.blocks[0], "__dict__"))
        self.assertFalse(hasattr(doc.blocks[0].inlines[0], "__dict__"))
        self.assertIn("https://example.org", markup_tree.links(doc))
        self.assertEqual(preview_text("Plain ||hidden|| and more", True), "Plain ▒▒▒ and more")

        # Markdown-significant start chars must bypass fast path cleanly.
        lst = parse_markdown("1. first item")
        self.assertIsInstance(lst.blocks[0], markup_tree.ListBlock)
        hd = parse_markdown("# Heading")
        self.assertIsInstance(hd.blocks[0], markup_tree.Heading)
        st = parse_styling("Plain line")
        self.assertEqual(to_plain(st), "Plain line")

    def test_pathological_markup_bounded_and_cache(self):
        # 50k unbalanced brackets and deep nested styling spans complete quickly via guards.
        t0 = time.perf_counter()
        d1 = parse_markdown("[" * 50_000)
        d2 = parse_styling("*a _b ~c `" * 5_000)
        deep_spans = styling.spans("* " * 2000 + ("*_" * 100) + "x" + ("_*" * 100))
        elapsed = time.perf_counter() - t0
        self.assertTrue(d1.blocks)
        self.assertTrue(d2.blocks)
        self.assertTrue(deep_spans)
        self.assertLess(elapsed, 1.0)

        # Cache hit when '@' absent even if nickname sets vary; large messages skip cache.
        markup._cache.clear()
        markup._cache_bytes = 0
        a = parse_markup("no mentions here", True, nicknames={"alice"})
        b = parse_markup("no mentions here", True, nicknames={"bob"})
        self.assertIs(a, b)
        huge = "hello " * 1000
        _ = parse_markup(huge, False)
        self.assertLessEqual(markup._cache_bytes, markup._CACHE_MAX_BYTES)
        self.assertLessEqual(len(markup._cache), markup._CACHE_MAX_ENTRIES)

        self.assertEqual(emoji_only("hello world"), 0)
        self.assertEqual(emoji_only("👍👍"), 2)


class AioAndServicesTests(unittest.TestCase):
    def test_aio_spawn_under_asyncio_run_and_gio_call_done_guard(self):
        async def run():
            done = asyncio.Event()

            async def job():
                done.set()

            t = aio.spawn(job())
            await asyncio.wait_for(done.wait(), 1.0)
            await t

            # Second callback after completion must be a no-op (not InvalidStateError).
            cbs = []
            fut = aio.gio_call(lambda *a: cbs.append(a[-1]), lambda res: res, "ok")
            cbs[0](None, "first")
            cbs[0](None, "second")
            self.assertEqual(await fut, "first")

        asyncio.run(run())

    def test_chat_service_flush_outbox_locked_and_detach_typing(self):
        async def run():
            db = Database(":memory:")
            db.open()
            try:
                chats = []
                core = _make_core(db)
                aid = await db.insert("accounts", {"jid": "me@example.com", "created_at": 1})
                cid = await db.insert("chats", {"account_id": aid, "jid": "bob@example.com", "type": "chat"})
                c = ChatItem(id=cid, account_id=aid, jid="bob@example.com", type="chat")
                c.typing = "Bob"
                chats.append(c)
                core.chats = SimpleNamespace(
                    by_id={cid: c},
                    lists={},
                    for_account=lambda a: chats if a == aid else [],
                    update_message_fields=AsyncMock(),
                )
                core.pep = SimpleNamespace(register=lambda *a: None)
                svc = ChatService(core)
                loop = asyncio.get_running_loop()
                svc._typing_timers[(cid, "bob@example.com")] = loop.call_later(60, lambda: None)
                svc.detach(aid)
                self.assertEqual(c.typing, "")
                self.assertEqual(svc._typing_timers, {})

                # Outbox locking: two concurrent flush_outbox calls send the pending row once.
                await db.run(
                    lambda conn: chatsmod.insert_message_job(
                        conn,
                        {
                            "chat_id": cid,
                            "origin_id": "o1",
                            "msg_id": "o1",
                            "sender": "me@example.com",
                            "direction": 1,
                            "timestamp": 1000,
                            "body": "queued",
                            "kind": "text",
                            "status": 0,
                            "extra": {"outbox": True},
                        },
                    )
                )
                sent = 0

                async def slow_transmit(_session, _chat, msg):
                    nonlocal sent
                    sent += 1
                    await asyncio.sleep(0.02)
                    msg.extra = {}
                    await db.execute("UPDATE messages SET status = 1, extra = '{}' WHERE id = ?", (msg.id,))

                svc._transmit = slow_transmit
                session = SimpleNamespace(account=SimpleNamespace(id=aid), online=True)
                await asyncio.gather(svc.flush_outbox(session), svc.flush_outbox(session))
                self.assertEqual(sent, 1)

                # _apply_pending_rows gracefully ignores deleted rows.
                await svc._apply_pending_rows(
                    c,
                    MessageItem(id=999_999, sender="bob", direction=0),
                    [{"kind": "correction", "payload": json.dumps({"sender": "bob", "direction": 0, "body": "new"}),
                      "timestamp": 1000}],
                )
            finally:
                db.close()

        asyncio.run(run())

    def test_muc_occupant_cleanup_rejoin_timer_and_destroy(self):
        from xml.etree import ElementTree as ET

        async def run():
            core = SimpleNamespace(
                pep=SimpleNamespace(register=lambda *a: None),
                accounts=SimpleNamespace(get=lambda _aid: None),
                chats=SimpleNamespace(delete=AsyncMock(), save=AsyncMock()),
                messaging=SimpleNamespace(
                    session_for=lambda _c: SimpleNamespace(iq=AsyncMock(), online=False),
                    flush_outbox=AsyncMock(),
                ),
                sessions={},
                db=SimpleNamespace(execute=AsyncMock()),
                e2e=SimpleNamespace(refresh=lambda _c: None),
            )
            muc = MucService(core)
            chat = ChatItem(id=10, account_id=1, jid="room@conf.example", type="groupchat")
            room = Room(1, "room@conf.example", chat, nick="me")
            room.initial = False
            room.joined = True
            muc.rooms[(1, "room@conf.example")] = room
            # Stub _status_line and _remember_occupant to keep test focused and DB-free
            muc._status_line = lambda *a: None
            muc._remember_occupant = AsyncMock()

            join_el = ET.fromstring(
                "<presence from='room@conf.example/bob'>"
                "<occupant-id xmlns='urn:xmpp:occupant-id:0' id='occ-bob'/>"
                "<x xmlns='http://jabber.org/protocol/muc#user'><item role='participant' affiliation='none'/></x>"
                "</presence>"
            )
            muc.on_presence(SimpleNamespace(account=SimpleNamespace(id=1)), join_el)
            self.assertIn("occ-bob", room.by_occupant_id)

            leave_el = ET.fromstring(
                "<presence from='room@conf.example/bob' type='unavailable'>"
                "<x xmlns='http://jabber.org/protocol/muc#user'/>"
                "</presence>"
            )
            muc.on_presence(SimpleNamespace(account=SimpleNamespace(id=1)), leave_el)
            self.assertNotIn("occ-bob", room.by_occupant_id)

            # 332 restart creates rejoin timer that is cancelled on leave()
            kicked_el = ET.fromstring(
                "<presence from='room@conf.example/me' type='unavailable'>"
                "<x xmlns='http://jabber.org/protocol/muc#user'><status code='110'/><status code='332'/></x>"
                "</presence>"
            )
            muc.on_presence(SimpleNamespace(account=SimpleNamespace(id=1)), kicked_el)
            self.assertIn((1, "room@conf.example"), muc._rejoin_timers)
            await muc.destroy(chat)
            self.assertNotIn((1, "room@conf.example"), muc._rejoin_timers)
            self.assertNotIn((1, "room@conf.example"), muc.rooms)

        asyncio.run(run())

    def test_session_console_and_invalid_chain_and_small_fixes(self):
        async def run():
            settings = _DummySettings({"advanced.devtools": False})
            acc = SimpleNamespace(id=1, jid="me@example.com", enabled=True, state="connecting", update=lambda **kw: [
                setattr(acc, k, v) for k, v in kw.items()
            ])
            s = AccountSession(acc, settings=settings)

            class ExplodingStanza:
                def __str__(self):
                    raise AssertionError("stanza serialized when devtools disabled")

            s._console_in(ExplodingStanza())
            settings["advanced.devtools"] = True
            s._keep_xml(True, "<message/>")
            self.assertEqual(len(s.xml_history), 1)
            settings["advanced.devtools"] = False
            s._console_out("<presence/>")
            self.assertEqual(len(s.xml_history), 0)
            self.assertEqual(s.xml_history_size, 0)

            # _on_invalid_chain prevents _on_disconnected from scheduling network retry race
            scheduled = []
            s._schedule = lambda delay: scheduled.append(delay)
            s._certificate_failed = AsyncMock()
            s.wanted = True
            s._candidate = SimpleNamespace(host="example.com", port=5222, direct_tls=False)
            s.xmpp = SimpleNamespace(abort=lambda: s._on_disconnected("cert abort"))
            s._on_invalid_chain(Exception("untrusted"))
            self.assertEqual(acc.state, "error")
            self.assertEqual(scheduled, [])
            await asyncio.sleep(0)

            # JPEG malformed padding / standalone marker / bad length guards
            bad_jpg = b"\xff\xd8\xff\xff\xff\x01\xff\xd0\xff\xe1\x00\x01\xff\xd9"
            self.assertEqual(images.jpeg_orientation(bad_jpg), 1)
            self.assertEqual(images.strip_jpeg(bad_jpg), bad_jpg)
            # Valid orientation=6 in Exif APP1
            tiff = b"II*\x00\x08\x00\x00\x00\x01\x00\x12\x01\x03\x00\x01\x00\x00\x00\x06\x00\x00\x00"
            app1 = b"Exif\x00\x00" + tiff
            good_jpg = (
                b"\xff\xd8\xff\xe1"
                + struct.pack(">H", len(app1) + 2)
                + app1
                + b"\xff\xda\x00\x02data"
            )
            self.assertEqual(images.jpeg_orientation(good_jpg), 6)
            self.assertNotIn(b"Exif", images.strip_jpeg(good_jpg))

            # Misc fixes: util.first_line huge text, proxy bad atyp rejection, http buffered + error cap
            big = "\n" * 5000 + "first non empty" + "\nmore" * 5000
            self.assertEqual(util.first_line(big), "first non empty")

            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.bind(("127.0.0.1", 0))
            srv.listen(2)
            port = srv.getsockname()[1]

            def socks_bad_atyp():
                conn, _ = srv.accept()
                with conn:
                    conn.recv(16)
                    conn.sendall(b"\x05\x00")
                    conn.recv(64)
                    conn.sendall(b"\x05\x00\x00\x09")  # invalid atyp 9

            t_socks = threading.Thread(target=socks_bad_atyp, daemon=True)
            t_socks.start()
            try:
                p = proxymod.Proxy("socks5", "127.0.0.1", port)
                with self.assertRaises(proxymod.ProxyError):
                    proxymod.open_socket_sync(p, "example.com", 5222, timeout=2.0)
                with self.assertRaises(proxymod.ProxyError):
                    # Also verify async open_socket rejects invalid atyp 9
                    def socks_bad_atyp2():
                        conn, _ = srv.accept()
                        with conn:
                            conn.recv(16)
                            conn.sendall(b"\x05\x00")
                            conn.recv(64)
                            conn.sendall(b"\x05\x00\x00\x09")

                    t2 = threading.Thread(target=socks_bad_atyp2, daemon=True)
                    t2.start()
                    try:
                        await proxymod.open_socket(p, "example.com", 5222, timeout=2.0)
                    finally:
                        t2.join(1.0)
            finally:
                t_socks.join(1.0)
                srv.close()

            # http error body cap (>1 MiB 500 response body capped without OOM)
            hsrv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            hsrv.bind(("127.0.0.1", 0))
            hsrv.listen(1)
            hport = hsrv.getsockname()[1]

            def http_big_err():
                conn, _ = hsrv.accept()
                with conn:
                    conn.recv(4096)
                    body = b"X" * (1200 * 1024)
                    hdr = f"HTTP/1.1 500 Err\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    conn.sendall(hdr + body)

            t_http = threading.Thread(target=http_big_err, daemon=True)
            t_http.start()
            try:
                resp = httpmod.request("GET", f"http://127.0.0.1:{hport}/fail", allow_http=True, timeout=3.0)
                self.assertEqual(resp.status, 500)
                self.assertEqual(len(resp.body), 1024 * 1024)
            finally:
                t_http.join(1.0)
                hsrv.close()

            # History fetch_older checks MAX(id) instead of COUNT(*)
            calls = []
            db_mock = SimpleNamespace(
                fetchone=AsyncMock(return_value={"stanza_id": "s1"}),
                scalar=AsyncMock(side_effect=[10, 12]),
            )
            hist_core = SimpleNamespace(
                db=db_mock,
                sessions={
                    1: SimpleNamespace(
                        online=True,
                        jid="me@example.com",
                        account=SimpleNamespace(id=1),
                        account_features={NS_MAM},
                    )
                },
                muc=SimpleNamespace(rooms={}),
                chats=SimpleNamespace(save=AsyncMock()),
            )
            hist = HistoryService(hist_core)
            hist._page = AsyncMock(return_value=SimpleNamespace(complete=False, count=2, first="s0"))
            c_obj = SimpleNamespace(id=1, account_id=1, jid="b@example.com", type="chat", options={})
            self.assertTrue(await hist.fetch_older(c_obj))
            for call in db_mock.scalar.await_args_list:
                calls.append(call.args[0])
            self.assertTrue(all("MAX(id)" in sql and "COUNT(*)" not in sql for sql in calls))

            # Notifier drops _pending when chat is removed
            rem_cb = []
            notif = Notifier(
                SimpleNamespace(
                    settings=_DummySettings({"notify.highlight_words": ["ping"]}),
                    messaging=SimpleNamespace(connect=lambda *a: None),
                    chats=SimpleNamespace(connect=lambda sig, cb: rem_cb.append(cb)),
                )
            )
            notif._pending[42] = 3
            rem_cb[0](None, SimpleNamespace(id=42))
            self.assertNotIn(42, notif._pending)
            gc = SimpleNamespace(type="groupchat", account_id=1)
            self.assertTrue(notif.mentions_me(gc, "hey ping!", ""))
            self.assertFalse(notif.mentions_me(gc, "nothing", ""))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
