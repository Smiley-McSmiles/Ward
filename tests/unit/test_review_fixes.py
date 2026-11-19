"""Regression tests for bugs found during the codebase review."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from ward.core import proxy as proxymod
from ward.core import roster
from ward.core.history import NS_MAM, HistoryService
from ward.core.markup import styling
from ward.core.storage.db import Database


class ReviewFixesTests(unittest.TestCase):
    def test_avatar_path_rejects_traversal(self):
        self.assertEqual(roster.avatar_path("../../etc/passwd"), "")
        self.assertEqual(roster.avatar_path("not-a-sha1"), "")
        self.assertEqual(roster.avatar_path("a" * 39), "")
        self.assertEqual(roster.avatar_path("a" * 41), "")
        valid = "A" * 40
        p = roster.avatar_path(valid)
        self.assertTrue(p.endswith(("a" * 40) + ".img"))

    def test_proxy_http_request_rejects_crlf(self):
        p = proxymod.Proxy("http", "127.0.0.1", 8080)
        with self.assertRaises(proxymod.ProxyError):
            proxymod._http_request("example.com\r\nEvil: 1", 443, p)
        with self.assertRaises(proxymod.ProxyError):
            proxymod._http_request("example.com\nEvil: 1", 443, p)

    def test_database_backup_to_keeps_transaction_loop_healthy(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as d:
                src = os.path.join(d, "src.db")
                dst = os.path.join(d, "dst.db")
                db = Database(src)
                db.open()
                try:
                    await db.execute("INSERT INTO accounts (jid, created_at) VALUES (?, ?)", ("a@example.com", 1))
                    await db.backup_to(dst)
                    # Subsequent transactions must not fail with OperationalError
                    await db.execute("INSERT INTO accounts (jid, created_at) VALUES (?, ?)", ("b@example.com", 2))
                    count = await db.scalar("SELECT COUNT(*) FROM accounts", (), 0)
                    self.assertEqual(count, 2)
                finally:
                    db.close()
                conn = sqlite3.connect(dst)
                try:
                    rows = conn.execute("SELECT jid FROM accounts").fetchall()
                    self.assertEqual([r[0] for r in rows], ["a@example.com"])
                finally:
                    conn.close()

        asyncio.run(run_test())

    def test_styling_deep_quotes_and_unmatched_markers(self):
        deep = "\n".join(["> " * 1200 + "hello"])
        blocks = styling._blocks(deep.splitlines(), set(), {})
        self.assertTrue(blocks)

        many_stars = "* " * 5000 + "*" * 5000
        runs = styling.spans(many_stars)
        self.assertTrue(runs)

    def test_history_supported_for_muc_room(self):
        room_jid = "room@conference.example.org"
        core = SimpleNamespace(
            muc=SimpleNamespace(rooms={(1, room_jid): SimpleNamespace(features={NS_MAM})}),
        )
        svc = HistoryService(core)
        session = SimpleNamespace(
            account=SimpleNamespace(id=1),
            jid="me@example.org",
            account_features=set(),
            services={},
        )
        self.assertTrue(svc.supported(session, room_jid))
        self.assertFalse(svc.supported(session, "other@conference.example.org"))

    def test_portal_import_from_calls_media(self):
        from ward.core import portal as core_portal
        from ward.core.calls import media as calls_media

        self.assertTrue(hasattr(core_portal, "screencast"))
        self.assertTrue(hasattr(core_portal, "close_session"))
        self.assertTrue(hasattr(calls_media, "MediaSession"))


if __name__ == "__main__":
    unittest.main()
