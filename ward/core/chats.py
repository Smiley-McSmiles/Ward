"""Chats and their messages in the database, and the ChatItem/MessageItem objects the interface shows.

All database work happens in jobs on the database thread (the *_job functions take the sqlite3 connection);
the async methods wrap them and keep the in-memory items in sync."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from gi.repository import Gio, GObject

from .. import util
from . import search
from .markup import parse as parse_markup
from .markup import preview as markup_preview
from .models import AttachmentItem, ChatItem, MessageItem, MessageList
from .storage.db import insert, update

log = logging.getLogger("ward.chats")

CHAT_FIELDS = ("name", "pinned", "muted_until", "archived", "open", "is_request", "draft", "last_read_id",
               "last_activity", "unread", "mentions", "folder", "encryption", "notify")


def now_ms() -> int:
    return int(time.time() * 1000)


def chat_from_row(r: sqlite3.Row) -> ChatItem:
    c = ChatItem(id=r["id"], account_id=r["account_id"], jid=r["jid"], type=r["type"], name=r["name"],
                 pinned=bool(r["pinned"]), muted_until=r["muted_until"], archived=bool(r["archived"]),
                 open=bool(r["open"]), is_request=bool(r["is_request"]), draft=r["draft"],
                 last_read_id=r["last_read_id"], last_activity=r["last_activity"], unread=r["unread"],
                 mentions=r["mentions"], folder=r["folder"], encryption=r["encryption"], notify=r["notify"])
    try:
        c.options = json.loads(r["options"] or "{}")
    except ValueError:
        c.options = {}
    return c


def message_from_row(r: sqlite3.Row) -> MessageItem:
    m = MessageItem(id=r["id"], chat_id=r["chat_id"], stanza_id=r["stanza_id"] or "", origin_id=r["origin_id"] or "",
                    msg_id=r["msg_id"] or "", sender=r["sender"], sender_jid=r["sender_jid"],
                    occupant_id=r["occupant_id"], direction=r["direction"], timestamp=r["timestamp"],
                    body=r["body"], is_markdown=bool(r["is_markdown"]), kind=r["kind"], encryption=r["encryption"],
                    sender_device=r["sender_device"] or 0, trust=r["trust_at_receipt"], edited=bool(r["edited"]),
                    retracted=r["retracted"], reply_to=r["reply_to"], status=r["status"], error=r["error"])
    try:
        m.extra = json.loads(r["extra"] or "{}")
    except ValueError:
        m.extra = {}
    m.reply_preview = m.extra.get("reply_preview", "")
    m.reply_author = m.extra.get("reply_author", "")
    m.mentions_me = bool(m.extra.get("mentions_me"))
    return m


def attachment_from_row(r: sqlite3.Row) -> AttachmentItem:
    a = AttachmentItem(id=r["id"], url=r["url"], name=r["name"], mime=r["mime"], size=r["size"] or 0,
                       width=r["width"] or 0, height=r["height"] or 0, duration=r["duration"] or 0,
                       local_path=r["local_path"], thumbnail=r["thumbnail"], state=r["state"],
                       encrypted=bool(r["aes_key"]))
    a.aes_key, a.aes_iv, a.sha256 = r["aes_key"], r["aes_iv"], r["sha256"]
    return a


# ----------------------------------------------------------------------------------------------- jobs
def find_message_job(conn: sqlite3.Connection, chat_id: int, ref: str, sender: str | None = None,
                     direction: int | None = None) -> sqlite3.Row | None:
    """A message by any of its ids (stanza-id, origin-id, id attribute), newest first."""
    if not ref:
        return None
    sql = "SELECT * FROM messages WHERE chat_id = ? AND (stanza_id = ? OR origin_id = ? OR msg_id = ?)"
    params: list[Any] = [chat_id, ref, ref, ref]
    if sender is not None:
        sql += " AND sender = ?"
        params.append(sender)
    if direction is not None:
        sql += " AND direction = ?"
        params.append(direction)
    return conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()


def insert_message_job(conn: sqlite3.Connection, values: dict, attachments: list[dict] | None = None) -> int:
    values = dict(values)
    extra = values.pop("extra", None)
    if extra is not None:
        values["extra"] = json.dumps(extra, ensure_ascii=False)
    mid = insert(conn, "messages", values)
    for a in attachments or []:
        insert(conn, "attachments", {**a, "message_id": mid})
    if values.get("body") and values.get("kind", "text") in ("text", "me"):
        search.index_message(conn, mid, values["body"], bool(values.get("is_markdown")))
    return mid


def load_reactions_job(conn: sqlite3.Connection, ids: list[int]) -> dict[int, list[tuple[str, list[str]]]]:
    if not ids:
        return {}
    out: dict[int, list[tuple[str, list[str]]]] = {}
    marks = ",".join("?" * len(ids))
    for r in conn.execute("SELECT message_id, sender, emojis FROM reactions WHERE message_id IN (%s) "
                          "ORDER BY timestamp" % marks, ids):
        try:
            emojis = json.loads(r["emojis"])
        except ValueError:
            emojis = []
        out.setdefault(r["message_id"], []).append((r["sender"], emojis))
    return out


def load_attachments_job(conn: sqlite3.Connection, ids: list[int]) -> dict[int, list[sqlite3.Row]]:
    if not ids:
        return {}
    out: dict[int, list[sqlite3.Row]] = {}
    marks = ",".join("?" * len(ids))
    for r in conn.execute("SELECT * FROM attachments WHERE message_id IN (%s) ORDER BY id" % marks, ids):
        out.setdefault(r["message_id"], []).append(r)
    return out


def summarize_reactions(rows: list[tuple[str, list[str]]], me: str, names) -> list[tuple[str, list[str], bool]]:
    """[(sender, [emoji...])] -> [(emoji, [names...], includes_me)] in first-seen order."""
    order: list[str] = []
    who: dict[str, list[str]] = {}
    mine: set[str] = set()
    for sender, emojis in rows:
        for e in emojis:
            if e not in who:
                order.append(e)
                who[e] = []
            who[e].append(names(sender))
            if sender == me:
                mine.add(e)
    return [(e, who[e], e in mine) for e in order]


class Chats(GObject.Object):
    """Every chat of every account.  The sidebar filters and sorts this one list."""
    __gsignals__ = {
        "added": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "removed": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "activity": (GObject.SignalFlags.RUN_LAST, None, (object,)),   # re-sort the chat list
        "unread-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self, core):
        super().__init__()
        self.core = core
        self.db = core.db
        self.store = Gio.ListStore.new(ChatItem)
        self.by_id: dict[int, ChatItem] = {}
        self.by_key: dict[tuple[int, str, str], ChatItem] = {}
        self.lists: dict[int, MessageList] = {}
        self._draft_timers: dict[int, Any] = {}

    def items(self) -> list[ChatItem]:
        return [self.store.get_item(i) for i in range(self.store.get_n_items())]

    async def load(self) -> None:
        rows = await self.db.fetchall("SELECT * FROM chats ORDER BY last_activity DESC")
        self.store.remove_all()
        self.by_id.clear()
        self.by_key.clear()
        items = []
        for r in rows:
            c = chat_from_row(r)
            self._index(c)
            items.append(c)
        self.store.splice(0, 0, items)
        previews = await self.db.run(lambda conn: {r["chat_id"]: r for r in conn.execute(
            "SELECT m.* FROM messages m JOIN (SELECT chat_id, MAX(id) AS mid FROM messages WHERE retracted = 0 "
            "AND kind IN ('text','me','file') GROUP BY chat_id) x ON m.id = x.mid")})
        for c in items:
            r = previews.get(c.id)
            if r is not None:
                m = message_from_row(r)
                m.sender_name = self.core.names.sender_name(c, m)
                self._set_preview(c, m)

    def _index(self, c: ChatItem) -> None:
        self.by_id[c.id] = c
        self.by_key[(c.account_id, c.jid, c.type)] = c

    def find(self, account_id: int, jid: str, type_: str | None = None) -> ChatItem | None:
        if type_ is not None:
            return self.by_key.get((account_id, jid, type_))
        for t in ("chat", "groupchat", "pm"):
            c = self.by_key.get((account_id, jid, t))
            if c is not None:
                return c
        return None

    def for_account(self, account_id: int) -> list[ChatItem]:
        return [c for c in self.items() if c.account_id == account_id]

    async def get_or_create(self, account_id: int, jid: str, type_: str = "chat", name: str = "",
                            open_: bool = True, is_request: bool = False) -> ChatItem:
        c = self.find(account_id, jid, type_)
        if c is not None:
            changes = {}
            if open_ and not c.open:
                changes["open"] = True
            if changes:
                c.update(**changes)
                await self.save(c, *changes)
                self.emit("activity", c)
            return c
        ts = now_ms()

        def job(conn):
            conn.execute("INSERT OR IGNORE INTO chats (account_id, jid, type, name, open, is_request, last_activity) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (account_id, jid, type_, name, int(open_), int(is_request),
                                                          ts))
            return conn.execute("SELECT * FROM chats WHERE account_id = ? AND jid = ? AND type = ?",
                                (account_id, jid, type_)).fetchone()

        row = await self.db.run(job)
        c = self.find(account_id, jid, type_)
        if c is not None:  # created concurrently
            return c
        c = chat_from_row(row)
        self._index(c)
        self.store.insert(0, c)
        self.emit("added", c)
        return c

    async def save(self, chat: ChatItem, *fields: str) -> None:
        fields = tuple(fields) or CHAT_FIELDS
        values: dict[str, Any] = {}
        for f in fields:
            if f == "options":
                values["options"] = chat.options_json()
            elif f in CHAT_FIELDS:
                v = getattr(chat, f)
                values[f] = int(v) if isinstance(v, bool) else v
        if values and self.db.is_open:     # a toggle made as Ward quits is not worth an error
            await self.db.update("chats", values, "id = ?", (chat.id,))

    async def delete(self, chat: ChatItem) -> None:
        await self.db.execute("DELETE FROM chats WHERE id = ?", (chat.id,))
        self.by_id.pop(chat.id, None)
        self.by_key.pop((chat.account_id, chat.jid, chat.type), None)
        self.lists.pop(chat.id, None)
        for i in range(self.store.get_n_items()):
            if self.store.get_item(i) is chat:
                self.store.remove(i)
                break
        self.emit("removed", chat)

    async def remove_account(self, account_id: int) -> None:
        for c in self.for_account(account_id):
            self.by_id.pop(c.id, None)
            self.by_key.pop((c.account_id, c.jid, c.type), None)
            self.lists.pop(c.id, None)
            for i in range(self.store.get_n_items()):
                if self.store.get_item(i) is c:
                    self.store.remove(i)
                    break
            self.emit("removed", c)

    # ------------------------------------------------------------------ chat list actions
    async def set_flags(self, chat: ChatItem, **values) -> None:
        chat.update(**values)
        await self.save(chat, *values)
        self.emit("activity", chat)

    async def close(self, chat: ChatItem) -> None:
        await self.set_flags(chat, open=False)

    def set_draft(self, chat: ChatItem, text: str) -> None:
        """Drafts are saved shortly after typing stops, and survive restarts."""
        if chat.draft == text:
            return
        chat.draft = text
        from .. import aio

        loop = aio.get_loop()
        h = self._draft_timers.pop(chat.id, None)
        if h is not None:
            h.cancel()
        self._draft_timers[chat.id] = loop.call_later(0.8, lambda: aio.spawn(self.save(chat, "draft")))

    def _set_preview(self, chat: ChatItem, m: MessageItem) -> None:
        if m.retracted:
            text = ""
        elif m.kind == "file" or (m.attachments and not m.body):
            a = m.attachments[0] if m.attachments else None
            text = "📎 " + (a.name if a and a.name else "File")
        elif m.encryption and not m.body:
            text = ""
        else:
            body = m.body[4:] if m.kind == "me" else m.body
            try:
                text = markup_preview(parse_markup(body, m.is_markdown,
                                                   markdown_from_others=self.core.settings["chats.markdown_others"]))
            except Exception:
                text = util.first_line(body)
            if m.kind == "me":
                text = "* %s %s" % (m.sender_name or m.sender, text)
        chat.update(preview=text, preview_sender="" if m.kind == "me" else ("me" if m.outgoing else
                                                                           (m.sender_name or "")))

    # ------------------------------------------------------------------ messages
    def message_list(self, chat: ChatItem) -> MessageList:
        ml = self.lists.get(chat.id)
        if ml is None:
            ml = MessageList(chat)
            self.lists[chat.id] = ml
        return ml

    async def load_page(self, chat: ChatItem, before: MessageItem | None = None, limit: int = 60
                        ) -> list[MessageItem]:
        """Messages before the given one (or the newest), oldest first, with reactions and attachments."""
        def job(conn):
            if before is None:
                rows = conn.execute("SELECT * FROM messages WHERE chat_id = ? ORDER BY timestamp DESC, id DESC "
                                    "LIMIT ?", (chat.id, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM messages WHERE chat_id = ? AND (timestamp < ? OR (timestamp = ? "
                                    "AND id < ?)) ORDER BY timestamp DESC, id DESC LIMIT ?",
                                    (chat.id, before.timestamp, before.timestamp, before.id, limit)).fetchall()
            ids = [r["id"] for r in rows]
            return rows, load_reactions_job(conn, ids), load_attachments_job(conn, ids)

        rows, reactions, attachments = await self.db.run(job)
        out = []
        for r in reversed(rows):
            m = message_from_row(r)
            self.decorate(chat, m, reactions.get(m.id, []), attachments.get(m.id, []))
            out.append(m)
        return out

    def decorate(self, chat: ChatItem, m: MessageItem, reactions=None, attachments=None) -> None:
        names = self.core.names
        m.sender_name = names.sender_name(chat, m)
        if reactions is not None:
            m.reactions = summarize_reactions(reactions, self.core.my_reaction_id(chat),
                                              lambda s: names.reactor_name(chat, s))
        if attachments is not None:
            m.attachments = [attachment_from_row(a) for a in attachments]

    async def open_messages(self, chat: ChatItem) -> MessageList:
        ml = self.message_list(chat)
        if len(ml) == 0:
            ml.add_many(await self.load_page(chat))
        return ml

    async def load_older(self, chat: ChatItem, limit: int = 60) -> int:
        ml = self.message_list(chat)
        if ml.loading:
            return 0
        ml.loading = True
        try:
            page = await self.load_page(chat, ml.oldest, limit)
            if not page:
                fetched = await self.core.history.fetch_older(chat)
                if fetched:
                    page = await self.load_page(chat, ml.oldest, limit)
                else:
                    ml.has_older = False
            ml.add_many(page)
            return len(page)
        finally:
            ml.loading = False

    async def reload_message(self, chat: ChatItem, msg_id: int) -> MessageItem | None:
        def job(conn):
            r = conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
            if r is None:
                return None, {}, {}
            return r, load_reactions_job(conn, [msg_id]), load_attachments_job(conn, [msg_id])

        r, reactions, attachments = await self.db.run(job)
        if r is None:
            return None
        fresh = message_from_row(r)
        self.decorate(chat, fresh, reactions.get(msg_id, []), attachments.get(msg_id, []))
        ml = self.lists.get(chat.id)
        cur = ml.get(msg_id) if ml else None
        if cur is None:
            return fresh
        cur.update(body=fresh.body, edited=fresh.edited, retracted=fresh.retracted, status=fresh.status,
                   error=fresh.error, stanza_id=fresh.stanza_id, trust=fresh.trust, kind=fresh.kind,
                   is_markdown=fresh.is_markdown)
        cur.extra = fresh.extra
        if cur.reactions != fresh.reactions:
            cur.reactions = fresh.reactions
            cur.reactions_rev += 1
        if [a.id for a in cur.attachments] != [a.id for a in fresh.attachments]:
            cur.attachments = fresh.attachments
            cur.attachments_rev += 1
        return cur

    def show_message(self, chat: ChatItem, m: MessageItem) -> None:
        """A new message: into the open list, and into the chat list's preview."""
        ml = self.lists.get(chat.id)
        if ml is not None:
            ml.add(m)
        if m.kind in ("text", "me", "file") and not m.retracted:
            self._set_preview(chat, m)
        if m.timestamp > chat.last_activity:
            chat.last_activity = m.timestamp
        self.emit("activity", chat)

    async def update_message_fields(self, msg_id: int, **values) -> None:
        if "extra" in values and isinstance(values["extra"], dict):
            values["extra"] = json.dumps(values["extra"], ensure_ascii=False)
        await self.db.run(lambda conn: update(conn, "messages", values, "id = ?", (msg_id,)))

    async def delete_for_me(self, chat: ChatItem, m: MessageItem) -> None:
        def job(conn):
            conn.execute("UPDATE messages SET retracted = 2, body = '' WHERE id = ?", (m.id,))
            conn.execute("DELETE FROM messages_fts WHERE rowid = ?", (m.id,))
            conn.execute("DELETE FROM message_edits WHERE message_id = ?", (m.id,))
        await self.db.run(job)
        m.update(retracted=2, body="")

    async def clear_history(self, chat: ChatItem) -> None:
        def job(conn):
            conn.execute("DELETE FROM messages_fts WHERE rowid IN (SELECT id FROM messages WHERE chat_id = ?)",
                         (chat.id,))
            conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat.id,))
        await self.db.run(job)
        ml = self.lists.get(chat.id)
        if ml is not None:
            ml.store.remove_all()
            ml.by_id.clear()
            ml._keys.clear()
        chat.update(preview="", unread=0, mentions=0)
        await self.save(chat, "unread", "mentions")

    async def apply_retention(self, chat: ChatItem | None, days: int, account_id: int | None = None) -> int:
        if days <= 0:
            return 0
        cutoff = now_ms() - days * 86400 * 1000

        def job(conn):
            where = "timestamp < ?"
            params: list[Any] = [cutoff]
            if chat is not None:
                where += " AND chat_id = ?"
                params.append(chat.id)
            elif account_id is not None:
                where += " AND chat_id IN (SELECT id FROM chats WHERE account_id = ?)"
                params.append(account_id)
            conn.execute("DELETE FROM messages_fts WHERE rowid IN (SELECT id FROM messages WHERE %s)" % where,
                         params)
            return conn.execute("DELETE FROM messages WHERE %s" % where, params).rowcount
        return await self.db.run(job)

    def total_unread(self) -> int:
        return sum(c.unread for c in self.items() if c.open and not c.muted(now_ms()))
