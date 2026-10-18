"""python-omemo's key/value storage on Ward's database (table omemo_store), one namespace per account."""

from __future__ import annotations

import json

from omemo.storage import Just, Maybe, Nothing, Storage


class WardStorage(Storage):
    def __init__(self, db, account_id: int):
        super().__init__(disable_cache=False)
        self.db = db
        self.account_id = account_id

    async def _load(self, key: str) -> Maybe:
        v = await self.db.scalar("SELECT value FROM omemo_store WHERE account_id = ? AND key = ?",
                                 (self.account_id, key))
        if v is None:
            return Nothing()
        return Just(json.loads(v))

    async def _store(self, key: str, value) -> None:
        await self.db.execute("INSERT INTO omemo_store (account_id, key, value) VALUES (?, ?, ?) "
                              "ON CONFLICT(account_id, key) DO UPDATE SET value = excluded.value",
                              (self.account_id, key, json.dumps(value)))

    async def _delete(self, key: str) -> None:
        await self.db.execute("DELETE FROM omemo_store WHERE account_id = ? AND key = ?", (self.account_id, key))

    async def dump(self) -> dict[str, str]:
        rows = await self.db.fetchall("SELECT key, value FROM omemo_store WHERE account_id = ?", (self.account_id,))
        return {r["key"]: r["value"] for r in rows}

    async def replace_all(self, data: dict[str, str]) -> None:
        def job(conn):
            conn.execute("DELETE FROM omemo_store WHERE account_id = ?", (self.account_id,))
            conn.executemany("INSERT INTO omemo_store (account_id, key, value) VALUES (?, ?, ?)",
                             [(self.account_id, k, v) for k, v in data.items()])
        await self.db.run(job)

    async def wipe(self) -> None:
        await self.db.execute("DELETE FROM omemo_store WHERE account_id = ?", (self.account_id,))
