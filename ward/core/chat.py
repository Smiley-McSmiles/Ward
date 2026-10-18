"""One-to-one messaging: receiving, sending, and everything that points at a message (receipts XEP-0184, read
markers XEP-0333 with cross-device sync XEP-0490, typing XEP-0085, corrections XEP-0308, retractions XEP-0424,
reactions XEP-0444, replies XEP-0461, stable ids XEP-0359), carbons (XEP-0280) and an outbox for messages written
while offline.  Group messages share the storage helpers here and are routed to muc.py.

Every incoming stanza of an account goes through one queue and is processed in order, so a correction is never
handled before the message it corrects - and anything that still arrives early waits in pending_ops."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from gi.repository import GObject

from .. import aio, util
from ..i18n import _
from . import stanzas
from .chats import find_message_job, insert_message_job, message_from_row
from .markup.emoji import emoji_only
from .models import (STATUS_DELIVERED, STATUS_FAILED, STATUS_PENDING, STATUS_READ, STATUS_SENT, ChatItem,
                     MessageItem)
from .session import NotConnected
from .stanzas import ParsedMessage
from .uri import parse_aesgcm

log = logging.getLogger("ward.chat")

NS_CARBONS = "urn:xmpp:carbons:2"
NS_MDS = "urn:xmpp:mds:displayed:0"
TYPING_TIMEOUT = 30


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Incoming:
    """A message after unwrapping, validation and decryption, ready to be stored."""
    pm: ParsedMessage
    outgoing: bool
    peer: str                       # full JID of the other side (or the room for groups)
    when: int                       # ms
    mode: str = "live"              # live | catchup | scrollback
    archive_id: str = ""
    encryption: str = ""
    sender_device: int = 0
    trust: str = ""
    decrypt_error: str = ""
    extra: dict = field(default_factory=dict)


class EncryptionBlocked(Exception):
    """The message must not go out unencrypted, and cannot be encrypted (the reason is user-facing)."""


class ChatService(GObject.Object):
    __gsignals__ = {
        "received": (GObject.SignalFlags.RUN_LAST, None, (object, object, str)),   # chat, message, mode
        "send-failed": (GObject.SignalFlags.RUN_LAST, None, (object, object, str)),
    }

    def __init__(self, core):
        super().__init__()
        self.core = core
        self.queues: dict[int, asyncio.Queue] = {}
        self.workers: dict[int, asyncio.Task] = {}
        self._typing_timers: dict[tuple[int, str], asyncio.TimerHandle] = {}
        self._sent_state: dict[int, str] = {}      # chat id -> last chat state we sent
        core.pep.register(NS_MDS, self._on_mds)

    # ================================================================== plumbing
    def attach(self, session) -> None:
        aid = session.account.id
        q: asyncio.Queue = asyncio.Queue()
        self.queues[aid] = q
        session.connect("message", lambda s, el: q.put_nowait(el))
        session.connect("acked", lambda s, mid: aio.spawn(self._on_acked(s, mid)))
        self.workers[aid] = aio.spawn(self._worker(session, q), name="messages %s" % session.jid)

    def detach(self, account_id: int) -> None:
        t = self.workers.pop(account_id, None)
        if t is not None:
            t.cancel()
        self.queues.pop(account_id, None)

    async def _worker(self, session, q: asyncio.Queue) -> None:
        while True:
            el = await q.get()
            try:
                await self.process(session, el)
            except Exception:
                log.exception("failed to process a message for %s", session.jid)
            finally:
                q.task_done()

    async def drain(self, session) -> None:
        q = self.queues.get(session.account.id)
        if q is not None:
            await q.join()

    async def on_online(self, session, resumed: bool) -> None:
        if not resumed and "urn:xmpp:carbons:2" in session.server_features:
            with contextlib.suppress(Exception):
                await session.iq("set", None, ET.Element("{%s}enable" % NS_CARBONS))
        await self.flush_outbox(session)

    # ================================================================== receiving
    async def process(self, session, el: ET.Element) -> None:
        if await self.core.pep.handle(session, el):
            return
        pm = stanzas.parse(el)
        own = session.jid
        w = pm.wrapper
        mode = "live"
        archive_id = ""
        if w is not None and w.kind.startswith("carbon"):
            if util.bare(w.from_jid) != own:
                log.warning("dropping a carbon copy not sent by our server (from %s)", w.from_jid)
                return
        if w is not None and w.kind == "mam":
            q = self.core.history.query(w.queryid)
            if q is None or util.bare(w.from_jid or own) != q.archive or q.account_id != session.account.id:
                log.warning("dropping an archive result that we did not ask for")
                return
            mode = q.mode
            archive_id = w.archive_id
            if q.muc:
                await self.core.muc.on_archived(session, pm, el, q, archive_id, w.delay)
                return
        if pm.type == "error":
            await self._on_error(session, pm)
            return
        if pm.type == "groupchat":
            if w is None:
                await self.core.muc.on_groupchat(session, pm, el)
            return
        if pm.muc_user is not None and (pm.muc_user.invite_from or pm.muc_user.declined_from):
            await self.core.muc.on_mediated_invite(session, pm)
            return
        if pm.direct_invite is not None and not pm.body:
            await self.core.muc.on_direct_invite(session, pm)
            return
        if pm.type == "headline":
            return

        outgoing = (w is not None and w.kind == "carbon-sent") or (
            w is not None and w.kind == "mam" and util.bare(pm.from_jid) == own)
        if not outgoing and util.bare(pm.from_jid) == own and util.bare(pm.to_jid or own) != own:
            outgoing = True     # a message our other device sent, reflected without a carbon wrapper
        peer = pm.to_jid if outgoing else pm.from_jid
        if not peer:
            return
        if w is not None and w.kind == "mam":
            when = int((w.delay or time.time()) * 1000)
        else:
            when = int((pm.delay or time.time()) * 1000)
        inc = Incoming(pm, outgoing, peer, when, mode, archive_id or pm.stanza_ids.get(own, ""))
        if pm.direct_invite is not None:
            await self.core.muc.on_direct_invite(session, pm)
        # Messages from a room occupant (not groupchat type) are private messages within that room
        is_pm = self.core.muc.is_room(session.account.id, util.bare(peer)) and bool(util.resource(peer))
        if pm.encrypted:
            await self.core.e2e.decrypt(session, el, inc, is_pm=is_pm)
        await self.handle(session, inc, is_pm=is_pm)

    async def handle(self, session, inc: Incoming, is_pm: bool = False) -> None:
        pm = inc.pm
        acc = session.account
        peer_bare = util.bare(inc.peer)
        chat_jid = inc.peer if is_pm else peer_bare
        chat_type = "pm" if is_pm else "chat"
        chat = self.core.chats.find(acc.id, chat_jid, chat_type)
        sender = acc.jid if inc.outgoing else (util.resource(inc.peer) if is_pm else peer_bare)

        # --- things that point at other messages (or at nothing in particular)
        if chat is not None:
            if pm.receipt_for and not inc.outgoing:
                await self._mark_status(chat, pm.receipt_for, STATUS_DELIVERED)
            if pm.marker is not None:
                kind, ref = pm.marker
                if kind in ("displayed", "acknowledged"):
                    if inc.outgoing:
                        await self._read_up_to(chat, ref)            # we read it on another device
                    else:
                        await self._mark_read_by_peer(chat, ref)
                elif kind == "received" and not inc.outgoing:
                    await self._mark_status(chat, ref, STATUS_DELIVERED)
            if pm.chat_state and inc.mode == "live" and not inc.outgoing:
                self._typing(chat, sender, pm.chat_state)
        if pm.correction_of:
            if chat is not None:
                await self._apply_correction(session, chat, inc, sender)
            return
        if pm.retraction_of:
            if chat is not None:
                await self._apply_retraction(chat, pm.retraction_of, sender, inc.outgoing)
            return
        if pm.reactions is not None:
            if chat is not None:
                await self._apply_reactions(chat, pm.reactions[0], sender, pm.reactions[1], inc.when)
            return
        body = pm.display_body()
        files = self._attachments(pm, body)
        if files and len(files) == 1 and body.strip() == files[0]["url"]:
            body = ""
        if not body.strip() and not files and not inc.decrypt_error:
            return
        if inc.decrypt_error:
            body = ""

        # --- strangers
        is_request = False
        if not inc.outgoing and not is_pm and chat is None:
            contact = self.core.roster.contacts.get(acc.id, peer_bare)
            if contact is not None and contact.blocked:
                return
            if contact is None or not contact.in_roster:
                policy = self.core.settings["privacy.strangers"]
                if policy == "block":
                    return
                is_request = policy == "requests"
        if chat is None:
            chat = await self.core.chats.get_or_create(acc.id, chat_jid, chat_type,
                                                       name=self.core.names.contact_name(acc.id, chat_jid),
                                                       is_request=is_request)
            if is_request:
                await self.core.requests.add_request(acc, peer_bare, util.first_line(body), "message", chat.id)
        if not chat.open and inc.mode != "scrollback":
            await self.core.chats.set_flags(chat, open=True)

        msg = await self.store(chat, inc, sender=sender, sender_jid=peer_bare if not inc.outgoing else acc.jid,
                               body=body, files=files)
        if msg is None:
            return
        if not inc.outgoing and inc.mode == "live" and pm.receipt_request and pm.id and not is_request \
                and self.core.settings["chats.send_receipts"]:
            with contextlib.suppress(NotConnected):
                el = session.new_message(inc.peer, "chat", util.random_id())
                stanzas.add_receipt(el, pm.id)
                stanzas.add_hint(el, "store")
                session.send(el)

    def _attachments(self, pm: ParsedMessage, body: str) -> list[dict]:
        out = []
        seen = set()
        for f in pm.files:
            if not f.url or f.url in seen:
                continue
            seen.add(f.url)
            d = {"url": f.url, "name": f.name, "mime": f.mime, "size": f.size, "width": f.width, "height": f.height,
                 "duration": f.duration, "sha256": f.sha256}
            link = parse_aesgcm(f.url)
            if link is not None:
                d.update(url=f.url, aes_key=link.key.hex(), aes_iv=link.iv.hex())
            if f.thumbnail:
                d["thumbnail_data"] = f.thumbnail
            out.append(d)
        for url, desc in pm.oob:
            if url in seen:
                continue
            seen.add(url)
            d = {"url": url, "name": desc or url.rsplit("/", 1)[-1].split("?")[0]}
            link = parse_aesgcm(url)
            if link is not None:
                d.update(aes_key=link.key.hex(), aes_iv=link.iv.hex())
            out.append(d)
        stripped = body.strip()
        if not out and re.fullmatch(r"aesgcm://\S+", stripped or ""):
            link = parse_aesgcm(stripped)
            if link is not None:
                out.append({"url": stripped, "name": link.https_url.rsplit("/", 1)[-1].split("?")[0],
                            "aes_key": link.key.hex(), "aes_iv": link.iv.hex()})
        for d in out:
            if not d.get("mime"):
                d["mime"] = self.core.files.guess_mime(d.get("name") or d["url"])
        return out

    async def store(self, chat: ChatItem, inc: Incoming, sender: str, sender_jid: str, body: str,
                    files: list[dict], occupant_id: str = "", groupchat: bool = False) -> MessageItem | None:
        """Deduplicate, insert, apply waiting operations, show, notify.  Returns None for duplicates."""
        pm = inc.pm
        direction = 1 if inc.outgoing else 0
        kind = "me" if body.startswith("/me ") else ("file" if files and not body.strip() else "text")
        if inc.decrypt_error:
            kind = "system"
        me = self.core.my_nick(chat) if groupchat else ""
        mentions_me = False
        if not inc.outgoing and body:
            mentions_me = self.core.notifier.mentions_me(chat, body, me, pm)
        extra: dict = dict(inc.extra)
        if pm.reply_to is not None:
            extra["reply_to_jid"] = pm.reply_to[0]
        if mentions_me:
            extra["mentions_me"] = True
        if inc.decrypt_error:
            extra["decrypt_error"] = inc.decrypt_error
        if not pm.encrypted and not inc.outgoing and chat.encryption_state in ("omemo", "omemo-legacy", "pgp"):
            extra["unencrypted"] = True
        if pm.spoiler is not None:
            extra["spoiler"] = pm.spoiler
        if emoji_only(body):
            extra["emoji_only"] = emoji_only(body)
        values = {"chat_id": chat.id, "stanza_id": inc.archive_id or None, "origin_id": pm.origin_id or None,
                  "msg_id": pm.id or None, "sender": sender, "sender_jid": sender_jid, "occupant_id": occupant_id,
                  "direction": direction, "timestamp": inc.when, "body": body, "is_markdown": int(pm.markdown),
                  "kind": kind, "encryption": inc.encryption, "sender_device": inc.sender_device or None,
                  "trust_at_receipt": inc.trust, "reply_to": pm.reply_to[1] if pm.reply_to else "",
                  "status": STATUS_SENT if inc.outgoing else 0, "extra": extra}
        thumbs = {}
        for i, f in enumerate(files):
            if f.get("thumbnail_data"):
                thumbs[i] = f.pop("thumbnail_data")
        att_rows = [{k: v for k, v in f.items() if v is not None} for f in files]

        def job(conn):
            # duplicates: the archive id is unique; otherwise the sender's ids with the same direction
            for ref in (inc.archive_id, pm.origin_id, pm.id):
                if not ref:
                    continue
                if ref == inc.archive_id:
                    row = conn.execute("SELECT * FROM messages WHERE chat_id = ? AND stanza_id = ?",
                                       (chat.id, ref)).fetchone()
                else:
                    row = find_message_job(conn, chat.id, ref, sender=sender, direction=direction)
                if row is not None:
                    if inc.archive_id and not row["stanza_id"]:
                        conn.execute("UPDATE messages SET stanza_id = ? WHERE id = ?", (inc.archive_id, row["id"]))
                    if direction == 1 and row["status"] in (STATUS_PENDING, STATUS_FAILED) and \
                            inc.mode != "scrollback":
                        conn.execute("UPDATE messages SET status = ?, error = '' WHERE id = ?",
                                     (STATUS_SENT, row["id"]))
                    return None, row["id"]
            ts = values["timestamp"]
            if ts % 1000 == 0:
                # Offline delivery and archives often stamp whole seconds: a message sent later in the same
                # second must not sort before what we already have from that second.
                latest = conn.execute("SELECT MAX(timestamp) FROM messages WHERE chat_id = ? AND timestamp >= ? "
                                      "AND timestamp < ?", (chat.id, ts, ts + 1000)).fetchone()[0]
                if latest is not None:
                    values["timestamp"] = latest + 1
            mid = insert_message_job(conn, values, att_rows)
            return mid, None

        mid, dup = await self.core.db.run(job)
        if mid is None:
            if dup is not None:
                await self.core.chats.reload_message(chat, dup)
            return None
        for i, data in thumbs.items():
            await self.core.files.store_thumbnail(mid, i, data)
        msg = await self.core.chats.reload_message(chat, mid)
        if msg is None:
            return None
        await self._resolve_reply(chat, msg)
        await self._apply_pending(chat, msg)
        await self._after_store(chat, msg, inc.mode)
        return msg

    async def _after_store(self, chat: ChatItem, msg: MessageItem, mode: str) -> None:
        chats = self.core.chats
        if mode != "scrollback":
            chats.show_message(chat, msg)
            if not msg.outgoing:
                if self.core.is_viewing(chat):
                    chat.last_read_id = msg.id
                    aio.spawn(self.mark_read(chat))
                elif msg.kind != "system":
                    chat.unread += 1
                    if msg.mentions_me:
                        chat.mentions += 1
                    chats.emit("unread-changed")
                if chat.typing:
                    self._clear_typing(chat, msg.sender)
            elif mode == "live" and msg.id > chat.last_read_id:
                # writing from another device means we have read everything before
                chat.update(last_read_id=msg.id, unread=0, mentions=0)
                chats.emit("unread-changed")
            await chats.save(chat, "unread", "mentions", "last_read_id", "last_activity", "open")
            if not msg.outgoing and msg.kind != "system":
                await self.core.files.auto_download(chat, msg)
        else:
            ml = chats.lists.get(chat.id)
            if ml is not None:
                ml.add(msg)
        self.emit("received", chat, msg, mode)

    async def _resolve_reply(self, chat: ChatItem, msg: MessageItem) -> None:
        if not msg.reply_to:
            return
        row = await self.core.db.run(lambda conn: find_message_job(conn, chat.id, msg.reply_to))
        if row is None:
            return
        target = message_from_row(row)
        name = self.core.names.sender_name(chat, target)
        preview = util.first_line(target.body if not target.retracted else _("Deleted message"), 100)
        msg.extra.update(reply_preview=preview, reply_author=name, reply_target=target.id)
        msg.update(reply_preview=preview, reply_author=name, reply_target=target.id)
        await self.core.chats.update_message_fields(msg.id, extra=msg.extra)

    # ------------------------------------------------------------------ pointing at messages
    async def _find(self, chat: ChatItem, ref: str, sender: str | None = None, direction: int | None = None):
        return await self.core.db.run(lambda conn: find_message_job(conn, chat.id, ref, sender, direction))

    async def _mark_status(self, chat: ChatItem, ref: str, status: int) -> None:
        row = await self._find(chat, ref, direction=1)
        if row is None or row["status"] >= status:
            return
        await self.core.chats.update_message_fields(row["id"], status=status)
        await self.core.chats.reload_message(chat, row["id"])

    async def _mark_read_by_peer(self, chat: ChatItem, ref: str) -> None:
        row = await self._find(chat, ref, direction=1)
        if row is None:
            return

        def job(conn):
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM messages WHERE chat_id = ? AND direction = 1 AND status >= 0 AND status < ? AND "
                "timestamp <= ?", (chat.id, STATUS_READ, row["timestamp"]))]
            conn.execute("UPDATE messages SET status = ? WHERE id IN (%s)" % ",".join("?" * len(ids)),
                         [STATUS_READ, *ids]) if ids else None
            return ids

        ids = await self.core.db.run(job)
        ml = self.core.chats.lists.get(chat.id)
        if ml is not None:
            for i in ids:
                m = ml.get(i)
                if m is not None:
                    m.status = STATUS_READ

    async def _read_up_to(self, chat: ChatItem, ref: str) -> None:
        row = await self._find(chat, ref)
        if row is None or row["id"] <= chat.last_read_id:
            return
        unread = await self.core.db.scalar("SELECT COUNT(*) FROM messages WHERE chat_id = ? AND id > ? AND "
                                           "direction = 0 AND kind != 'system'", (chat.id, row["id"]), 0)
        chat.update(last_read_id=row["id"], unread=unread, mentions=0 if unread == 0 else chat.mentions)
        await self.core.chats.save(chat, "last_read_id", "unread", "mentions")
        self.core.chats.emit("unread-changed")

    async def _apply_correction(self, session, chat: ChatItem, inc: Incoming, sender: str) -> None:
        pm = inc.pm
        direction = 1 if inc.outgoing else 0
        row = await self._find(chat, pm.correction_of, sender=sender, direction=direction)
        new_body = pm.display_body()
        if row is None:
            await self._pending(chat, pm.correction_of, "correction", {"body": new_body, "sender": sender,
                                                                       "direction": direction,
                                                                       "markdown": pm.markdown})
            return
        await self._do_correction(chat, row, new_body, pm.markdown, inc.when)

    async def _do_correction(self, chat, row, new_body: str, markdown: bool, when: int) -> None:
        from . import search

        def job(conn):
            conn.execute("INSERT INTO message_edits (message_id, body, timestamp) VALUES (?, ?, ?)",
                         (row["id"], row["body"], row["timestamp"]))
            conn.execute("UPDATE messages SET body = ?, edited = 1, is_markdown = ? WHERE id = ?",
                         (new_body, int(markdown), row["id"]))
            search.index_message(conn, row["id"], new_body, markdown)

        await self.core.db.run(job)
        m = await self.core.chats.reload_message(chat, row["id"])
        if m is not None and m.id == await self._latest_id(chat):
            self.core.chats._set_preview(chat, m)

    async def _latest_id(self, chat) -> int:
        return await self.core.db.scalar("SELECT MAX(id) FROM messages WHERE chat_id = ?", (chat.id,), 0)

    async def _apply_retraction(self, chat: ChatItem, ref: str, sender: str, outgoing: bool) -> None:
        row = await self._find(chat, ref, sender=sender, direction=1 if outgoing else 0)
        if row is None:
            await self._pending(chat, ref, "retraction", {"sender": sender, "direction": 1 if outgoing else 0})
            return
        await self.retract_row(chat, row["id"], 1)

    async def retract_row(self, chat: ChatItem, message_id: int, how: int, extra: dict | None = None) -> None:
        def job(conn):
            conn.execute("UPDATE messages SET retracted = ?, body = '' WHERE id = ?", (how, message_id))
            conn.execute("DELETE FROM messages_fts WHERE rowid = ?", (message_id,))
            conn.execute("DELETE FROM message_edits WHERE message_id = ?", (message_id,))
            conn.execute("DELETE FROM reactions WHERE message_id = ?", (message_id,))
            if extra:
                r = conn.execute("SELECT extra FROM messages WHERE id = ?", (message_id,)).fetchone()
                try:
                    cur = json.loads(r[0] or "{}")
                except ValueError:
                    cur = {}
                cur.update(extra)
                conn.execute("UPDATE messages SET extra = ? WHERE id = ?", (json.dumps(cur), message_id))
            return [r[0] for r in conn.execute("SELECT local_path FROM attachments WHERE message_id = ?",
                                               (message_id,))]

        paths_ = await self.core.db.run(job)
        self.core.files.forget(paths_)
        await self.core.chats.reload_message(chat, message_id)

    async def _apply_reactions(self, chat: ChatItem, ref: str, sender: str, emojis: list[str], when: int) -> None:
        row = await self._find(chat, ref)
        if row is None:
            await self._pending(chat, ref, "reaction", {"sender": sender, "emojis": emojis, "when": when})
            return
        await self._set_reactions(chat, row["id"], sender, emojis, when)

    async def _set_reactions(self, chat, message_id: int, sender: str, emojis: list[str], when: int) -> None:
        def job(conn):
            if emojis:
                conn.execute("INSERT INTO reactions (message_id, sender, emojis, timestamp) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT(message_id, sender) DO UPDATE SET emojis = excluded.emojis, "
                             "timestamp = excluded.timestamp", (message_id, sender, json.dumps(emojis), when))
            else:
                conn.execute("DELETE FROM reactions WHERE message_id = ? AND sender = ?", (message_id, sender))
        await self.core.db.run(job)
        await self.core.chats.reload_message(chat, message_id)

    async def _pending(self, chat: ChatItem, target: str, kind: str, payload: dict) -> None:
        await self.core.db.insert("pending_ops", {"chat_id": chat.id, "target": target, "kind": kind,
                                                  "payload": json.dumps(payload), "timestamp": now_ms()})

    async def _apply_pending(self, chat: ChatItem, msg: MessageItem) -> None:
        refs = list(msg.ref_ids())
        if not refs:
            return

        def job(conn):
            marks = ",".join("?" * len(refs))
            rows = conn.execute("SELECT * FROM pending_ops WHERE chat_id = ? AND target IN (%s) ORDER BY id" % marks,
                                [chat.id, *refs]).fetchall()
            if rows:
                conn.execute("DELETE FROM pending_ops WHERE id IN (%s)" % ",".join("?" * len(rows)),
                             [r["id"] for r in rows])
            return rows

        rows = await self.core.db.run(job)
        for r in rows:
            p = json.loads(r["payload"])
            if r["kind"] == "correction" and p.get("sender") == msg.sender and p.get("direction") == msg.direction:
                row = await self.core.db.fetchone("SELECT * FROM messages WHERE id = ?", (msg.id,))
                await self._do_correction(chat, row, p["body"], p.get("markdown", False), r["timestamp"])
            elif r["kind"] == "retraction" and p.get("sender") == msg.sender:
                await self.retract_row(chat, msg.id, 1)
            elif r["kind"] == "moderation":
                await self.retract_row(chat, msg.id, 3, {"moderated_by": p.get("by", ""),
                                                        "moderation_reason": p.get("reason", "")})
            elif r["kind"] == "reaction":
                await self._set_reactions(chat, msg.id, p["sender"], p.get("emojis", []), p.get("when", now_ms()))

    async def _on_error(self, session, pm: ParsedMessage) -> None:
        peer = util.bare(pm.from_jid)
        cond, text = pm.error or ("", "")
        for chat in (self.core.chats.find(session.account.id, peer, "chat"),
                     self.core.chats.find(session.account.id, pm.from_jid, "pm")):
            if chat is None or not pm.id:
                continue
            row = await self._find(chat, pm.id, direction=1)
            if row is not None:
                reason = text or self.core.describe_error(cond)
                await self.core.chats.update_message_fields(row["id"], status=STATUS_FAILED, error=reason)
                await self.core.chats.reload_message(chat, row["id"])

    async def _on_acked(self, session, msg_id: str) -> None:
        for chat in self.core.chats.for_account(session.account.id):
            ml = self.core.chats.lists.get(chat.id)
            if ml is not None:
                for m in reversed(ml.items()):
                    if m.outgoing and m.msg_id == msg_id:
                        if m.status == STATUS_PENDING:
                            m.status = STATUS_SENT
                        break
        await self.core.db.execute("UPDATE messages SET status = ? WHERE msg_id = ? AND direction = 1 AND status = ? "
                                   "AND chat_id IN (SELECT id FROM chats WHERE account_id = ?)",
                                   (STATUS_SENT, msg_id, STATUS_PENDING, session.account.id))

    # ------------------------------------------------------------------ typing
    def _typing(self, chat: ChatItem, who: str, state: str) -> None:
        if not self.core.settings["chats.show_typing"]:
            return
        key = (chat.id, who)
        h = self._typing_timers.pop(key, None)
        if h is not None:
            h.cancel()
        names = [n for n in chat.typing.split(", ") if n] if chat.typing else []
        name = self.core.names.contact_name(chat.account_id, who) if chat.type == "chat" else who
        if state == "composing":
            if name not in names:
                names.append(name)
            self._typing_timers[key] = aio.get_loop().call_later(TYPING_TIMEOUT, self._typing, chat, who, "paused")
        elif name in names:
            names.remove(name)
        chat.typing = ", ".join(names)

    def _clear_typing(self, chat: ChatItem, who: str) -> None:
        self._typing(chat, who, "active")

    # ================================================================== sending
    def session_for(self, chat: ChatItem):
        s = self.core.sessions.get(chat.account_id)
        if s is None:
            raise NotConnected(str(chat.account_id))
        return s

    async def send_text(self, chat: ChatItem, text: str, reply_to: MessageItem | None = None) -> MessageItem | None:
        text = text.rstrip()
        if not text.strip():
            return None
        session = self.session_for(chat)
        acc = session.account
        msg_id = util.random_id(18)
        body = text
        extra: dict = {}
        if reply_to is not None:
            author = self.core.names.sender_name(chat, reply_to)
            fallback = stanzas.reply_fallback(reply_to.body, author)
            body = fallback + text
            extra.update(reply_preview=util.first_line(reply_to.body, 100), reply_author=author,
                         reply_target=reply_to.id, reply_fallback=len(fallback))
        group = chat.type == "groupchat"
        ref = ""
        if reply_to is not None:
            ref = (reply_to.stanza_id if group else (reply_to.origin_id or reply_to.msg_id)) or reply_to.msg_id
        values = {"chat_id": chat.id, "origin_id": msg_id, "msg_id": msg_id, "direction": 1, "timestamp": now_ms(),
                  "sender": self.core.my_nick(chat) if group else acc.jid, "sender_jid": acc.jid, "body": text,
                  "is_markdown": 1, "kind": "me" if text.startswith("/me ") else "text", "status": STATUS_PENDING,
                  "reply_to": ref, "extra": {**extra, "outbox": True, "wire_body": body, "reply_jid":
                                             (reply_to.sender_jid if not group else reply_to.sender)
                                             if reply_to is not None else ""}}
        mid = await self.core.db.run(lambda conn: insert_message_job(conn, values))
        msg = await self.core.chats.reload_message(chat, mid)
        assert msg is not None
        if reply_to is not None:
            msg.update(reply_preview=extra["reply_preview"], reply_author=extra["reply_author"],
                       reply_target=reply_to.id)
        self.core.chats.show_message(chat, msg)
        chat.update(draft="", last_read_id=mid, unread=0, mentions=0)
        await self.core.chats.save(chat, "draft", "last_read_id", "unread", "mentions", "last_activity")
        self._sent_state[chat.id] = "active"
        if session.online:
            await self._transmit(session, chat, msg)
        return msg

    def build_message(self, session, chat: ChatItem, msg: MessageItem) -> ET.Element:
        group = chat.type == "groupchat"
        el = session.new_message(chat.jid, "groupchat" if group else "chat", msg.msg_id)
        wire = msg.extra.get("wire_body") or msg.body
        stanzas.set_body(el, wire)
        stanzas.add_origin_id(el, msg.origin_id or msg.msg_id)
        stanzas.add_markdown_flag(el)
        if not group:
            stanzas.add_receipt_request(el)
        stanzas.add_markable(el)
        if self.core.settings["chats.send_typing"]:
            stanzas.add_chat_state(el, "active")
        if msg.reply_to:
            fb_len = int(msg.extra.get("reply_fallback") or 0)
            stanzas.add_reply(el, msg.extra.get("reply_jid", ""), msg.reply_to, wire[:fb_len] if fb_len else "")
        stanzas.add_hint(el, "store")
        return el

    async def _transmit(self, session, chat: ChatItem, msg: MessageItem) -> None:
        el = self.build_message(session, chat, msg)
        await self._send_element(session, chat, el, msg)

    async def _send_element(self, session, chat: ChatItem, el: ET.Element, msg: MessageItem | None) -> bool:
        try:
            out, enc = await self.core.e2e.encrypt(session, chat, el)
        except EncryptionBlocked as e:
            if msg is not None:
                await self._fail(chat, msg, str(e))
            return False
        except Exception as e:
            log.exception("encryption failed")
            if msg is not None:
                await self._fail(chat, msg, _("Encryption failed: %s") % e)
            return False
        try:
            session.send(out)
        except NotConnected:
            return False
        if msg is not None:
            extra = dict(msg.extra)
            extra.pop("outbox", None)
            status = STATUS_PENDING if session.sm_enabled else STATUS_SENT
            msg.extra = extra
            msg.update(encryption=enc, status=max(msg.status, status), error="")
            await self.core.chats.update_message_fields(msg.id, encryption=enc, status=msg.status, error="",
                                                        extra=extra)
        return True

    async def _fail(self, chat: ChatItem, msg: MessageItem, reason: str) -> None:
        msg.update(status=STATUS_FAILED, error=reason)
        extra = dict(msg.extra)
        extra.pop("outbox", None)
        msg.extra = extra
        await self.core.chats.update_message_fields(msg.id, status=STATUS_FAILED, error=reason, extra=extra)
        self.emit("send-failed", chat, msg, reason)

    async def retry(self, chat: ChatItem, msg: MessageItem) -> None:
        session = self.session_for(chat)
        msg.update(status=STATUS_PENDING, error="")
        if session.online:
            await self._transmit(session, chat, msg)
        else:
            msg.extra["outbox"] = True
            await self.core.chats.update_message_fields(msg.id, status=STATUS_PENDING, error="", extra=msg.extra)

    async def flush_outbox(self, session) -> None:
        rows = await self.core.db.fetchall(
            "SELECT m.* FROM messages m JOIN chats c ON c.id = m.chat_id WHERE c.account_id = ? AND m.direction = 1 "
            "AND m.status = ? AND m.extra LIKE '%\"outbox\": true%' ORDER BY m.id", (session.account.id, STATUS_PENDING))
        for r in rows:
            chat = self.core.chats.by_id.get(r["chat_id"])
            if chat is None or (chat.type == "groupchat" and not chat.joined):
                continue
            msg = self.core.chats.lists[chat.id].get(r["id"]) if chat.id in self.core.chats.lists else None
            msg = msg or message_from_row(r)
            await self._transmit(session, chat, msg)

    async def correct(self, chat: ChatItem, msg: MessageItem, new_text: str) -> None:
        """Edit one of our messages (XEP-0308): everyone sees the new text, with an "edited" label."""
        new_text = new_text.rstrip()
        if not msg.outgoing or not new_text or new_text == msg.body:
            return
        session = self.session_for(chat)
        group = chat.type == "groupchat"
        el = session.new_message(chat.jid, "groupchat" if group else "chat", util.random_id(18))
        stanzas.set_body(el, new_text)
        stanzas.add_correction(el, msg.origin_id or msg.msg_id)
        stanzas.add_origin_id(el, el.get("id"))
        stanzas.add_markdown_flag(el)
        stanzas.add_hint(el, "store")
        if await self._send_element(session, chat, el, None):
            row = await self.core.db.fetchone("SELECT * FROM messages WHERE id = ?", (msg.id,))
            await self._do_correction(chat, row, new_text, True, now_ms())

    async def retract(self, chat: ChatItem, msg: MessageItem) -> None:
        """Delete for everyone (XEP-0424)."""
        session = self.session_for(chat)
        group = chat.type == "groupchat"
        el = session.new_message(chat.jid, "groupchat" if group else "chat", util.random_id(18))
        target = (msg.stanza_id or msg.origin_id or msg.msg_id) if group else (msg.origin_id or msg.msg_id)
        stanzas.add_retraction(el, target)
        if await self._send_element(session, chat, el, None):
            await self.retract_row(chat, msg.id, 1)

    async def react(self, chat: ChatItem, msg: MessageItem, emoji: str) -> None:
        """Toggle one reaction (XEP-0444): we always send our whole set for the message."""
        session = self.session_for(chat)
        me = self.core.my_reaction_id(chat)
        mine = [e for e, _who, includes_me in msg.reactions if includes_me]
        if emoji in mine:
            mine.remove(emoji)
        else:
            mine.append(emoji)
        group = chat.type == "groupchat"
        target = (msg.stanza_id or msg.msg_id) if group else (msg.origin_id or msg.msg_id)
        if not target:
            return
        el = session.new_message(chat.jid, "groupchat" if group else "chat", util.random_id(18))
        stanzas.add_reactions(el, target, mine)
        if await self._send_element(session, chat, el, None):
            await self._set_reactions(chat, msg.id, me, mine, now_ms())

    def set_chat_state(self, chat: ChatItem, state: str) -> None:
        """composing / paused / active from the composer (sent only on changes)."""
        if not self.core.settings["chats.send_typing"] or chat.type == "groupchat" and not chat.joined:
            return
        if self._sent_state.get(chat.id) == state:
            return
        self._sent_state[chat.id] = state
        session = self.core.sessions.get(chat.account_id)
        if session is None or not session.online:
            return
        if chat.type == "chat":
            contact = self.core.roster.contacts.get(chat.account_id, chat.jid)
            if contact is None or not contact.sees_us or contact.blocked:
                return
        el = session.new_message(chat.jid, "groupchat" if chat.type == "groupchat" else "chat", util.random_id())
        stanzas.add_chat_state(el, state)
        stanzas.add_hint(el, "no-store")
        aio.spawn(self._send_element(session, chat, el, None))

    # ------------------------------------------------------------------ reading
    async def mark_read(self, chat: ChatItem) -> None:
        """The user saw the chat: clear the unread count, send a read marker, sync our other devices."""
        row = await self.core.db.fetchone(
            "SELECT * FROM messages WHERE chat_id = ? AND direction = 0 AND retracted = 0 AND kind != 'system' "
            "ORDER BY timestamp DESC, id DESC LIMIT 1", (chat.id,))
        changed = chat.unread or chat.mentions
        latest = await self._latest_id(chat)
        chat.update(unread=0, mentions=0, last_read_id=max(chat.last_read_id, latest))
        if changed:
            self.core.chats.emit("unread-changed")
        await self.core.chats.save(chat, "unread", "mentions", "last_read_id")
        if row is None or chat.options.get("marker_sent") == row["id"]:
            return
        session = self.core.sessions.get(chat.account_id)
        if session is None or not session.online:
            return
        chat.options["marker_sent"] = row["id"]
        await self.core.chats.save(chat, "options")
        group = chat.type == "groupchat"
        ref = row["stanza_id"] if group else row["msg_id"]
        if self.core.settings["chats.send_markers"] and ref and not chat.is_request:
            el = session.new_message(chat.jid, "groupchat" if group else "chat", util.random_id())
            stanzas.add_marker(el, "displayed", ref)
            stanzas.add_hint(el, "store")
            with contextlib.suppress(Exception):
                await self._send_element(session, chat, el, None)
        if row["stanza_id"]:
            await self._publish_mds(session, chat, row["stanza_id"])

    async def _publish_mds(self, session, chat: ChatItem, stanza_id: str) -> None:
        """XEP-0490: tell our other devices how far we read (stored privately on our server)."""
        if "http://jabber.org/protocol/pubsub#publish-options" not in session.account_features:
            return
        disp = ET.Element("{%s}displayed" % NS_MDS)
        by = chat.jid if chat.type == "groupchat" else session.jid
        ET.SubElement(disp, "{urn:xmpp:sid:0}stanza-id", {"id": stanza_id, "by": by})
        with contextlib.suppress(Exception):
            await self.core.pep.publish(session, NS_MDS, disp, chat.jid,
                                        {"pubsub#persist_items": "true", "pubsub#max_items": "max",
                                         "pubsub#send_last_published_item": "never",
                                         "pubsub#access_model": "whitelist"})

    async def _on_mds(self, session, frm: str, items, retracted) -> None:
        if frm != session.jid:
            return
        for it in items:
            chat_jid = it.get("id", "")
            sid = it.find("{%s}displayed/{urn:xmpp:sid:0}stanza-id" % NS_MDS)
            if sid is None:
                continue
            for t in ("chat", "groupchat"):
                chat = self.core.chats.find(session.account.id, chat_jid, t)
                if chat is not None:
                    await self._read_up_to(chat, sid.get("id", ""))
