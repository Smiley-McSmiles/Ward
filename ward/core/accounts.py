"""The account list: stored in the database, shown through a Gio.ListStore of AccountItem."""

from __future__ import annotations

import json
import logging

from gi.repository import Gio, GObject

from .. import schema, util
from . import secrets
from .models import AccountItem
from .storage.db import now_ms

log = logging.getLogger("ward.accounts")

# A colour stripe per account, in this order (accessible on light and dark backgrounds)
PALETTE = ["#3584e4", "#2ec27e", "#e66100", "#9141ac", "#e01b24", "#1a5fb4", "#c64600", "#26a269", "#865e3c",
           "#c061cb"]


class AccountError(Exception):
    pass


class Accounts(GObject.Object):
    __gsignals__ = {
        "added": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "removed": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "option-changed": (GObject.SignalFlags.RUN_LAST, None, (object, str)),
    }

    def __init__(self, db):
        super().__init__()
        self.db = db
        self.store = Gio.ListStore.new(AccountItem)
        self.by_id: dict[int, AccountItem] = {}

    def __iter__(self):
        return iter(self.list())

    def list(self) -> list[AccountItem]:
        return [self.store.get_item(i) for i in range(self.store.get_n_items())]

    def get(self, account_id: int) -> AccountItem | None:
        return self.by_id.get(account_id)

    def find(self, jid: str) -> AccountItem | None:
        jid = util.bare(jid)
        for a in self.list():
            if a.jid == jid:
                return a
        return None

    def _item(self, row) -> AccountItem:
        try:
            opts = json.loads(row["options"] or "{}")
        except ValueError:
            opts = {}
        acc = AccountItem(id=row["id"], jid=row["jid"], label=row["label"], color=row["color"],
                          enabled=bool(row["enabled"]))
        acc.options = opts if isinstance(opts, dict) else {}
        # Images/voice and other files once had separate limits.  Keep the file limit when the media limit was
        # never changed, then drop the old key the next time the account's options are saved.
        legacy_file_limit = acc.options.pop("autodownload_files_mb", None)
        if legacy_file_limit is not None and "autodownload_mb" not in acc.options:
            acc.options["autodownload_mb"] = legacy_file_limit
        if row["resource"]:
            acc.options.setdefault("resource", row["resource"])
        acc.nickname = acc.options.get("nickname", "")
        acc.show = acc.options.get("_show", "available")
        acc.status = acc.options.get("_status", "")
        acc.state = "offline" if acc.enabled else "disabled"
        return acc

    async def load(self) -> None:
        rows = await self.db.fetchall("SELECT * FROM accounts ORDER BY position, id")
        self.store.remove_all()
        self.by_id.clear()
        for r in rows:
            acc = self._item(r)
            self.by_id[acc.id] = acc
            self.store.append(acc)

    async def add(self, jid: str, password: str | None, store_mode: str = "keyring",
                  options: dict | None = None) -> AccountItem:
        jid = util.bare(jid)
        if not util.valid_bare_jid(jid) or "@" not in jid:
            raise AccountError("not a valid address: %s" % jid)
        if self.find(jid):
            raise AccountError("%s is already added" % jid)
        opts = dict(options or {})
        opts.setdefault("resource", util.random_resource())
        if store_mode != "keyring":
            opts["store_password"] = store_mode
        color = PALETTE[len(self.by_id) % len(PALETTE)]
        position = len(self.by_id)
        acc_id = await self.db.insert("accounts", {"jid": jid, "resource": opts["resource"], "enabled": 1,
                                                   "color": color, "position": position,
                                                   "options": json.dumps(_clean(opts)), "created_at": now_ms()})
        row = await self.db.fetchone("SELECT * FROM accounts WHERE id = ?", (acc_id,))
        acc = self._item(row)
        if password:
            try:
                await secrets.store(jid, password, acc.option("store_password"))
            except secrets.KeyringUnavailable as e:
                # still usable now; the password is asked again after a restart (the account says so)
                log.warning("%s: password kept for this session only: %s", jid, e)
                secrets.remember_for_session(jid, password)
                acc.options["_keyring_failed"] = True
        self.by_id[acc.id] = acc
        self.store.append(acc)
        self.emit("added", acc)
        return acc

    async def remove(self, acc: AccountItem) -> None:
        await self.db.execute("DELETE FROM accounts WHERE id = ?", (acc.id,))
        await secrets.clear(acc.jid)
        self.by_id.pop(acc.id, None)
        for i in range(self.store.get_n_items()):
            if self.store.get_item(i) is acc:
                self.store.remove(i)
                break
        self.emit("removed", acc)

    async def save_options(self, acc: AccountItem) -> None:
        await self.db.execute("UPDATE accounts SET options = ?, label = ?, color = ?, enabled = ? WHERE id = ?",
                              (json.dumps(_clean(acc.options)), acc.label, acc.color, int(acc.enabled), acc.id))

    async def set_option(self, acc: AccountItem, key: str, value) -> None:
        if not key.startswith("_"):
            value = schema.coerce(schema.ACCOUNT_SPECS[key], value)
        if acc.options.get(key) == value:
            return
        acc.options[key] = value
        if key == "nickname":
            acc.nickname = value
        await self.save_options(acc)
        self.emit("option-changed", acc, key)

    async def set_enabled(self, acc: AccountItem, enabled: bool) -> None:
        acc.enabled = enabled
        await self.save_options(acc)

    async def set_password(self, acc: AccountItem, password: str) -> None:
        await secrets.store(acc.jid, password, acc.option("store_password"))

    async def reorder(self, acc: AccountItem, position: int) -> None:
        items = self.list()
        items.remove(acc)
        items.insert(max(0, min(position, len(items))), acc)
        self.store.splice(0, self.store.get_n_items(), items)
        await self.db.executemany("UPDATE accounts SET position = ? WHERE id = ?",
                                  [(i, a.id) for i, a in enumerate(items)])


def _clean(opts: dict) -> dict:
    """Store only options that differ from their defaults (and our internal _keys)."""
    out = {}
    for k, v in opts.items():
        if k.startswith("_") or k not in schema.ACCOUNT_SPECS or v != schema.account_default(k):
            out[k] = v
    return out
