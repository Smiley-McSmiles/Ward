import os
import shutil
import sqlite3

import pytest

from ward import paths
from ward.core.storage import db as dbmod
from ward.core.storage.db import Database, DatabaseError


@pytest.fixture
def database(ward_home):
    d = Database()
    d.open()
    yield d
    d.close()


def test_fresh_database_has_latest_schema(database):
    v = database.run_sync(lambda c: c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0])
    assert v == dbmod.latest_version() >= 1
    tables = database.run_sync(lambda c: {r[0] for r in c.execute("SELECT name FROM sqlite_master")})
    for t in ("accounts", "contacts", "chats", "messages", "messages_fts", "reactions", "attachments", "receipts",
              "mam_state", "omemo_store", "pgp_keys", "pending_ops"):
        assert t in tables
    assert oct(os.stat(paths.db_file()).st_mode & 0o777) == "0o600"


def test_async_helpers_and_cascade(database, run):
    async def main():
        acc = await database.insert("accounts", {"jid": "me@example.org", "created_at": 1})
        chat = await database.insert("chats", {"account_id": acc, "jid": "you@example.org"})
        mid = await database.insert("messages", {"chat_id": chat, "direction": 0, "timestamp": 5, "body": "hi"})
        await database.execute("INSERT INTO messages_fts (rowid, text) VALUES (?, ?)", (mid, "hello wörld"))
        hits = await database.fetchall("SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?", ("world",))
        assert [h[0] for h in hits] == [mid]       # diacritics folded
        await database.kv_set("x", "1")
        assert await database.kv_get("x") == "1"
        await database.execute("DELETE FROM accounts WHERE id = ?", (acc,))
        return await database.scalar("SELECT COUNT(*) FROM messages")

    assert run(main()) == 0


def test_failed_job_rolls_back(database, run):
    def job(conn):
        conn.execute("INSERT INTO kv VALUES ('a', 'b')")
        raise RuntimeError("boom")

    async def main():
        with pytest.raises(RuntimeError):
            await database.run(job)
        return await database.kv_get("a")

    assert run(main()) is None


def test_migration_backup_and_cleanup(ward_home, tmp_path, monkeypatch):
    d = Database()
    d.open()
    d.run_sync(lambda c: c.execute("INSERT INTO kv VALUES ('keep', 'me')"))
    d.close()
    mig = tmp_path / "migrations"
    shutil.copytree(dbmod.MIGRATIONS, mig)
    (mig / "0099_test.sql").write_text("CREATE TABLE extra (x INTEGER);")
    monkeypatch.setattr(dbmod, "MIGRATIONS", str(mig))
    d = Database()
    d.open()
    assert d.migrated_from == 1
    backups = os.listdir(paths.backups_dir())
    assert backups == ["ward.db.before-v99"]
    old = sqlite3.connect(os.path.join(paths.backups_dir(), backups[0]))
    assert old.execute("SELECT value FROM kv").fetchone()[0] == "me"
    d.close()
    d = Database()
    d.open()                                       # nothing to migrate: backups are removed
    assert os.listdir(paths.backups_dir()) == []
    d.close()


def test_newer_database_is_refused(ward_home):
    d = Database()
    d.open()
    d.run_sync(lambda c: c.execute("INSERT INTO schema_version VALUES (999, 0)"))
    d.close()
    with pytest.raises(DatabaseError):
        Database().open()
