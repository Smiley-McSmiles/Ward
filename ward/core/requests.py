"""The Requests section: people who want to see your status, and strangers who wrote to you.  Their images and
links stay unloaded until you accept.  Accept, Decline, Block, or Block and report as spam."""

from __future__ import annotations

import json
import time

from gi.repository import Gio, GObject

from .. import util
from .models import Item, _prop


class RequestItem(Item):
    __gtype_name__ = "WardRequest"
    account_id = _prop(int)
    jid = _prop(str)
    kind = _prop(str, "subscription")    # subscription | message
    text = _prop(str)
    timestamp = _prop("int64")
    chat_id = _prop(int)

    def __init__(self, **kw):
        super().__init__()
        for k, v in kw.items():
            setattr(self, k, v)


class Requests(GObject.Object):
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_LAST, None, ())}

    def __init__(self, core):
        super().__init__()
        self.core = core
        self.store = Gio.ListStore.new(RequestItem)

    def items(self) -> list[RequestItem]:
        return [self.store.get_item(i) for i in range(self.store.get_n_items())]

    def find(self, account_id: int, jid: str) -> RequestItem | None:
        jid = util.bare(jid)
        for r in self.items():
            if r.account_id == account_id and r.jid == jid:
                return r
        return None

    async def load(self) -> None:
        self.store.remove_all()
        rows = await self.core.db.fetchall("SELECT key, value FROM kv WHERE key LIKE 'requests:%'")
        for row in rows:
            try:
                for d in json.loads(row["value"]):
                    self.store.append(RequestItem(**d))
            except (ValueError, TypeError):
                continue

    async def _save(self, account_id: int) -> None:
        data = [{"account_id": r.account_id, "jid": r.jid, "kind": r.kind, "text": r.text,
                 "timestamp": r.timestamp, "chat_id": r.chat_id} for r in self.items() if r.account_id == account_id]
        await self.core.db.kv_set("requests:%d" % account_id, json.dumps(data))
        self.emit("changed")

    async def add_request(self, account, jid: str, text: str = "", kind: str = "subscription",
                          chat_id: int = 0) -> RequestItem:
        r = self.find(account.id, jid)
        if r is not None:
            if kind == "subscription" and r.kind == "message":
                r.kind = "subscription"
            if text:
                r.text = text
            if chat_id:
                r.chat_id = chat_id
        else:
            r = RequestItem(account_id=account.id, jid=util.bare(jid), kind=kind, text=text,
                            timestamp=int(time.time() * 1000), chat_id=chat_id)
            self.store.insert(0, r)
        await self._save(account.id)
        return r

    async def remove(self, account_id: int, jid: str) -> None:
        r = self.find(account_id, jid)
        if r is None:
            return
        for i in range(self.store.get_n_items()):
            if self.store.get_item(i) is r:
                self.store.remove(i)
                break
        await self._save(account_id)

    async def drop_account(self, account_id: int) -> None:
        for r in [r for r in self.items() if r.account_id == account_id]:
            await self.remove(account_id, r.jid)
        await self.core.db.execute("DELETE FROM kv WHERE key = ?", ("requests:%d" % account_id,))
