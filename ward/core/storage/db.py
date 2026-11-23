"""The database: SQLite in WAL mode, used from one dedicated thread so the interface never waits on the disk.

    rows = await db.fetchall("SELECT ... WHERE chat_id = ?", (chat_id,))
    msg_id = await db.insert("messages", {"chat_id": 1, "body": "hi", ...})
    result = await db.run(lambda conn: ...)          # anything else, inside one transaction

Migrations are the numbered SQL files in migrations/, applied in order at startup.  Before any migration the
database is copied (with SQLite's online backup, so the WAL is included) to backups/ward.db.before-vN, and those
copies are removed at the next start that needs no migration - i.e. after the new version has started once.
"""

from __future__ import annotations

import asyncio
import glob
import itertools
import logging
import os
import queue
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, TypeVar

from ... import paths

log = logging.getLogger("ward.db")
T = TypeVar("T")
HERE = os.path.dirname(os.path.abspath(__file__))
MIGRATIONS = os.path.join(HERE, "migrations")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def migration_files() -> list[tuple[int, str]]:
    out = []
    for p in glob.glob(os.path.join(MIGRATIONS, "*.sql")):
        m = re.match(r"^(\d+)_", os.path.basename(p))
        if m:
            out.append((int(m.group(1)), p))
    return sorted(out)


def latest_version() -> int:
    files = migration_files()
    return files[-1][0] if files else 0


def now_ms() -> int:
    return int(time.time() * 1000)


class DatabaseError(Exception):
    pass


class Database:
    def __init__(self, path: str | None = None):
        self.path = path or paths.db_file()
        self._jobs: queue.PriorityQueue[tuple[int, int, Any, Any]] = queue.PriorityQueue()
        self._read_jobs: queue.PriorityQueue[tuple[int, int, Any, Any]] | None = None
        self._job_seq = itertools.count()
        self._thread: threading.Thread | None = None
        self._read_thread: threading.Thread | None = None
        self._conn: sqlite3.Connection | None = None
        self._read_conn: sqlite3.Connection | None = None
        self.migrated_from: int | None = None

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        """Start the database thread and bring the schema up to date (blocking, at startup)."""
        if self._thread is not None:
            return
        if self.path != ":memory:":
            paths.ensure(os.path.dirname(self.path))
        ready = threading.Event()
        err: list[BaseException] = []

        def boot():
            try:
                self._conn = self._connect()
                self._migrate(self._conn)
            except BaseException as e:  # reported to the caller of open()
                err.append(e)
            finally:
                ready.set()
            if not err:
                self._loop()

        self._thread = threading.Thread(target=boot, name="ward-db", daemon=True)
        self._thread.start()
        ready.wait()
        if err:
            self._thread = None
            raise DatabaseError("could not open %s: %s" % (self.path, err[0])) from err[0]

        if self.path != ":memory:":
            read_ready = threading.Event()
            read_err: list[BaseException] = []
            self._read_jobs = queue.PriorityQueue()

            def read_boot():
                try:
                    self._read_conn = self._connect(readonly=True)
                except BaseException as e:
                    read_err.append(e)
                finally:
                    read_ready.set()
                if not read_err:
                    self._read_loop()

            self._read_thread = threading.Thread(target=read_boot, name="ward-db-read", daemon=True)
            self._read_thread.start()
            read_ready.wait()
            if read_err:
                self._read_thread = None
                self._read_jobs = None
                self.close()
                raise DatabaseError(
                    "could not open read connection for %s: %s" % (self.path, read_err[0])
                ) from read_err[0]

    def _connect(self, readonly: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if readonly:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.execute("PRAGMA cache_size = -4096")
            return conn
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -8192")
        return conn

    def close(self) -> None:
        if self._read_thread is not None and self._read_jobs is not None:
            read_done = threading.Event()
            self._read_jobs.put((999, next(self._job_seq), None, read_done))
            read_done.wait(10)
            self._read_thread = None
            self._read_jobs = None
            self._read_conn = None
        if self._thread is None:
            return
        done = threading.Event()
        self._jobs.put((999, next(self._job_seq), None, done))
        done.wait(10)
        self._thread = None
        self._conn = None

    def _loop(self) -> None:
        assert self._conn is not None
        conn = self._conn
        while True:
            _prio, _seq, job, reply = self._jobs.get()
            if job is None:
                try:
                    conn.execute("PRAGMA optimize")
                    conn.close()
                finally:
                    reply.set()
                return
            try:
                conn.execute("BEGIN")
                try:
                    result = job(conn)
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
            except BaseException as e:
                reply(None, e)
            else:
                reply(result, None)

    def _read_loop(self) -> None:
        assert self._read_conn is not None
        assert self._read_jobs is not None
        conn = self._read_conn
        while True:
            _prio, _seq, job, reply = self._read_jobs.get()
            if job is None:
                try:
                    conn.close()
                finally:
                    reply.set()
                return
            try:
                conn.execute("BEGIN DEFERRED")
                try:
                    result = job(conn)
                finally:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
            except BaseException as e:
                reply(None, e)
            else:
                reply(result, None)

    # ------------------------------------------------------------------ migrations
    def _migrate(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL, applied_at INTEGER "
                     "NOT NULL)")
        current = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
        todo = [(v, p) for v, p in migration_files() if v > current]
        if current > latest_version():
            raise DatabaseError("the database was created by a newer version of Ward (schema %d, this version "
                                "knows %d)" % (current, latest_version()))
        if not todo:
            self._cleanup_backups()
            return
        if current > 0:
            self._backup(conn, current)
        self.migrated_from = current
        for version, path in todo:
            with open(path, encoding="utf-8") as fh:
                sql = fh.read()
            log.info("applying migration %s", os.path.basename(path))
            try:
                conn.executescript("BEGIN;\n" + sql + "\nINSERT INTO schema_version VALUES (%d, %d);\nCOMMIT;"
                                   % (version, now_ms()))
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def _backup(self, conn: sqlite3.Connection, version: int) -> None:
        if self.path == ":memory:":
            return
        d = paths.ensure(paths.backups_dir())
        dest = os.path.join(d, "%s.before-v%d" % (os.path.basename(self.path), latest_version()))
        target = sqlite3.connect(dest)
        try:
            conn.backup(target)
        finally:
            target.close()
        log.info("database (schema %d) backed up to %s before migrating", version, dest)

    def _cleanup_backups(self) -> None:
        if self.path == ":memory:":
            return
        for p in glob.glob(os.path.join(paths.backups_dir(), os.path.basename(self.path) + ".before-v*")):
            try:
                os.unlink(p)
            except OSError:
                pass

    @property
    def is_open(self) -> bool:
        return self._thread is not None

    # ------------------------------------------------------------------ running jobs
    def _submit(self, q: queue.PriorityQueue[tuple[int, int, Any, Any]],
                func: Callable[[sqlite3.Connection], T], priority: int = 0) -> asyncio.Future[T]:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[T] = loop.create_future()

        def reply(result, exc):
            def deliver():
                if fut.cancelled():
                    return
                if exc is not None:
                    fut.set_exception(exc)
                else:
                    fut.set_result(result)

            try:
                loop.call_soon_threadsafe(deliver)
            except RuntimeError:
                pass

        q.put((priority, next(self._job_seq), func, reply))
        return fut

    def run(self, func: Callable[[sqlite3.Connection], T], *, priority: int = 0) -> asyncio.Future[T]:
        """Run func(conn) on the database thread inside one transaction; await the result."""
        if self._thread is None:
            raise DatabaseError("database is not open")
        return self._submit(self._jobs, func, priority=priority)

    def run_read(self, func: Callable[[sqlite3.Connection], T], *, priority: int = 0) -> asyncio.Future[T]:
        """Run read-only func(conn) on the dedicated WAL reader when available, else the DB queue."""
        if self._thread is None:
            raise DatabaseError("database is not open")
        if self._read_jobs is not None and self._read_thread is not None:
            return self._submit(self._read_jobs, func, priority=priority)
        return self._submit(self._jobs, func, priority=priority)

    def run_sync(self, func: Callable[[sqlite3.Connection], T], timeout: float = 30, *, priority: int = 0) -> T:
        """Blocking variant for startup code and tests (never from a GTK callback)."""
        if self._thread is None:
            raise DatabaseError("database is not open")
        box: dict[str, Any] = {}
        done = threading.Event()

        def reply(result, exc):
            box["r"], box["e"] = result, exc
            done.set()

        self._jobs.put((priority, next(self._job_seq), func, reply))
        if not done.wait(timeout):
            raise DatabaseError("database job timed out")
        if box["e"] is not None:
            raise box["e"]
        return box["r"]

    # ------------------------------------------------------------------ helpers
    async def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> int:
        """Returns lastrowid for INSERTs, the number of changed rows otherwise."""
        def job(conn):
            cur = conn.execute(sql, params)
            return cur.lastrowid if sql.lstrip()[:6].upper() == "INSERT" else cur.rowcount
        return await self.run(job)

    async def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        rows = list(seq)
        await self.run(lambda conn: conn.executemany(sql, rows))

    async def fetchall(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> list[sqlite3.Row]:
        return await self.run(lambda conn: conn.execute(sql, params).fetchall())

    async def fetchone(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> sqlite3.Row | None:
        return await self.run(lambda conn: conn.execute(sql, params).fetchone())

    async def scalar(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = (), default: Any = None) -> Any:
        row = await self.fetchone(sql, params)
        return row[0] if row is not None and row[0] is not None else default

    async def fetchall_read(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = (), *,
                            priority: int = 0) -> list[sqlite3.Row]:
        return await self.run_read(lambda conn: conn.execute(sql, params).fetchall(), priority=priority)

    async def fetchone_read(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = (), *,
                            priority: int = 0) -> sqlite3.Row | None:
        return await self.run_read(lambda conn: conn.execute(sql, params).fetchone(), priority=priority)

    async def scalar_read(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = (), default: Any = None, *,
                          priority: int = 0) -> Any:
        row = await self.fetchone_read(sql, params, priority=priority)
        return row[0] if row is not None and row[0] is not None else default

    async def insert(self, table: str, values: Mapping[str, Any], or_: str = "") -> int:
        return await self.run(lambda conn: insert(conn, table, values, or_))

    async def update(self, table: str, values: Mapping[str, Any], where: str, params: Sequence[Any] = ()) -> int:
        return await self.run(lambda conn: update(conn, table, values, where, params))

    async def kv_get(self, key: str, default: str | None = None) -> str | None:
        v = await self.scalar("SELECT value FROM kv WHERE key = ?", (key,))
        return default if v is None else v

    async def kv_set(self, key: str, value: str) -> None:
        await self.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                           "value = excluded.value", (key, value))

    async def size(self) -> int:
        def job(conn):
            pages = conn.execute("PRAGMA page_count").fetchone()[0]
            psize = conn.execute("PRAGMA page_size").fetchone()[0]
            return pages * psize
        return await self.run(job)

    async def compact(self) -> None:
        """VACUUM cannot run inside a transaction, so it bypasses run()'s BEGIN."""
        if self._thread is None:
            raise DatabaseError("database is not open")
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        def job(conn):
            conn.execute("COMMIT")      # end the transaction run() opened
            try:
                conn.execute("VACUUM")
                conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('optimize')")
            finally:
                conn.execute("BEGIN")

        def reply(result, exc):
            def deliver():
                if not fut.done():
                    if exc:
                        fut.set_exception(exc)
                    else:
                        fut.set_result(None)

            try:
                loop.call_soon_threadsafe(deliver)
            except RuntimeError:
                pass

        self._jobs.put((1, next(self._job_seq), job, reply))
        await fut

    async def backup_to(self, dest: str) -> None:
        def job(conn):
            target = sqlite3.connect(dest)
            try:
                conn.commit()
                conn.backup(target)
            finally:
                target.close()
                conn.execute("BEGIN")
        await self.run(job)


def _check_ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError("bad SQL identifier %r" % name)
    return name


def insert(conn: sqlite3.Connection, table: str, values: Mapping[str, Any], or_: str = "") -> int:
    cols = [_check_ident(c) for c in values]
    verb = "INSERT OR %s" % or_ if or_ else "INSERT"
    sql = "%s INTO %s (%s) VALUES (%s)" % (verb, _check_ident(table), ", ".join(cols), ", ".join("?" * len(cols)))
    return conn.execute(sql, list(values.values())).lastrowid or 0


def update(conn: sqlite3.Connection, table: str, values: Mapping[str, Any], where: str,
           params: Sequence[Any] = ()) -> int:
    if not values:
        return 0
    sets = ", ".join("%s = ?" % _check_ident(c) for c in values)
    sql = "UPDATE %s SET %s WHERE %s" % (_check_ident(table), sets, where)
    return conn.execute(sql, [*values.values(), *params]).rowcount
