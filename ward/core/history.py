"""History sync with the server archive (XEP-0313 MAM, with RSM paging XEP-0059).

On reconnect one query to our own archive fetches every 1:1 message since the last known archive id, 100 per
page, until complete; new accounts load the last N days (an account option, 7 by default).  Older messages load
as the user scrolls up.  Groups are caught up from their own archives when joined.  Results arrive as normal
messages tagged with our query id; the chat service checks the tag and the sender before trusting them."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from .. import util
from . import forms
from .stanzas import format_stamp

log = logging.getLogger("ward.history")

NS_MAM = "urn:xmpp:mam:2"
NS_RSM = "http://jabber.org/protocol/rsm"
PAGE = 100


@dataclass
class Query:
    queryid: str
    account_id: int
    archive: str        # bare JID of the archive: our own, or the room's
    muc: bool
    mode: str           # catchup | scrollback


@dataclass
class Page:
    complete: bool
    first: str
    last: str
    count: int


class HistoryService:
    def __init__(self, core):
        self.core = core
        self.queries: dict[str, Query] = {}
        self._catching_up: set[tuple[int, str]] = set()

    def query(self, queryid: str) -> Query | None:
        return self.queries.get(queryid)

    @staticmethod
    def supported(session, archive: str | None = None) -> bool:
        feats = session.account_features if archive in (None, session.jid) else \
            session.services.get(archive, {}).get("features", set())
        return NS_MAM in feats

    async def _page(self, session, archive: str | None, mode: str, muc: bool = False, with_: str = "",
                    start: float | None = None, after: str | None = None, before: str | None = None,
                    max_: int = PAGE) -> Page:
        qid = util.random_id(10)
        archive_bare = util.bare(archive) if archive else session.jid
        self.queries[qid] = Query(qid, session.account.id, archive_bare, muc, mode)
        q = ET.Element("{%s}query" % NS_MAM, {"queryid": qid})
        fields: dict = {}
        if with_:
            fields["with"] = with_
        if start is not None:
            fields["start"] = format_stamp(start)
        q.append(forms.simple(NS_MAM, fields))
        rsm = ET.SubElement(q, "{%s}set" % NS_RSM)
        ET.SubElement(rsm, "{%s}max" % NS_RSM).text = str(max_)
        if after:
            ET.SubElement(rsm, "{%s}after" % NS_RSM).text = after
        if before is not None:
            ET.SubElement(rsm, "{%s}before" % NS_RSM).text = before or None
        try:
            res = await session.iq("set", archive, q, timeout=60)
            # every result message was queued before this answer: let them be stored first
            await self.core.messaging.drain(session)
        finally:
            self.queries.pop(qid, None)
        fin = res.find("{%s}fin" % NS_MAM)
        if fin is None:
            return Page(True, "", "", 0)
        s = fin.find("{%s}set" % NS_RSM)
        first = s.findtext("{%s}first" % NS_RSM) if s is not None else ""
        last = s.findtext("{%s}last" % NS_RSM) if s is not None else ""
        count_text = s.findtext("{%s}count" % NS_RSM) if s is not None else ""
        return Page(fin.get("complete") in ("true", "1"), first or "", last or "",
                    int(count_text) if count_text and count_text.isdigit() else 0)

    async def _state(self, account_id: int, archive: str):
        return await self.core.db.fetchone("SELECT * FROM mam_state WHERE account_id = ? AND archive = ?",
                                           (account_id, archive))

    async def _save_state(self, account_id: int, archive: str, last_id: str) -> None:
        await self.core.db.execute(
            "INSERT INTO mam_state (account_id, archive, last_id, last_ts) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(account_id, archive) DO UPDATE SET last_id = excluded.last_id, last_ts = excluded.last_ts",
            (account_id, archive, last_id, int(time.time() * 1000)))

    async def on_online(self, session, resumed: bool) -> None:
        if resumed or not self.supported(session):
            return
        await self.catch_up(session)

    async def catch_up(self, session, archive: str | None = None, muc: bool = False) -> int:
        """Fetch everything newer than what we have.  Returns the number of pages."""
        acc = session.account
        key = archive or session.jid
        if (acc.id, key) in self._catching_up:
            return 0
        self._catching_up.add((acc.id, key))
        self.core.e2e.history_sync(session, True)
        pages = 0
        try:
            state = await self._state(acc.id, key)
            after = state["last_id"] if state is not None and state["last_id"] else None
            start = None
            if after is None:
                days = int(acc.option("first_sync_days") or 7) if not muc else 2
                start = time.time() - days * 86400
            while True:
                try:
                    page = await self._page(session, archive, "catchup", muc=muc, start=start, after=after)
                except Exception as e:
                    if getattr(e, "condition", "") == "item-not-found" and after:
                        # our last id expired from the archive: continue from the time we last synced instead
                        start = (state["last_ts"] / 1000) if state is not None else time.time() - 86400
                        after = None
                        continue
                    raise
                pages += 1
                if page.last:
                    after = page.last
                    await self._save_state(acc.id, key, page.last)
                if page.complete or not page.last or page.count == 0 or pages > 200:
                    break
        except Exception as e:
            log.info("history catch-up of %s failed: %s", key, e)
        finally:
            self._catching_up.discard((acc.id, key))
            self.core.e2e.history_sync(session, False)
        return pages

    async def resync_chat(self, session, chat) -> int:
        """Fetch this chat's whole server archive from newest to oldest, independent of mam_state.

        A normal catch-up only asks for messages newer than the last archive id.  If a device's state is ahead of
        its local rows, or a previous catch-up stopped early, there is no way to fill the gap through the normal
        path.  This explicit resync walks the archive backwards for one chat; message ids still deduplicate.
        """
        group = chat.type == "groupchat"
        archive = chat.jid if group else None
        if not self.supported(session, archive):
            raise RuntimeError("the server does not archive messages")
        key = archive or session.jid
        marker = (session.account.id, key)
        if marker in self._catching_up:
            return 0
        self._catching_up.add(marker)
        pages = 0
        before = ""
        self.core.e2e.history_sync(session, True)
        try:
            while True:
                page = await self._page(session, archive, "scrollback", muc=group,
                                        with_="" if group else chat.jid, before=before, max_=PAGE)
                pages += 1
                if page.complete or not page.first or page.count == 0 or pages > 200:
                    break
                before = page.first
        finally:
            self._catching_up.discard(marker)
            self.core.e2e.history_sync(session, False)
        return pages

    async def fetch_older(self, chat) -> bool:
        """One page of older messages for a chat, from the server archive.  False when there is nothing more."""
        if chat.options.get("history_complete"):
            return False
        session = self.core.sessions.get(chat.account_id)
        if session is None or not session.online:
            return False
        group = chat.type == "groupchat"
        archive = chat.jid if group else None
        if not self.supported(session, archive):
            return False
        oldest = await self.core.db.fetchone(
            "SELECT stanza_id FROM messages WHERE chat_id = ? AND stanza_id IS NOT NULL AND stanza_id != '' "
            "ORDER BY timestamp ASC, id ASC LIMIT 1", (chat.id,))
        before = oldest["stanza_id"] if oldest is not None else ""
        before_count = await self.core.db.scalar("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat.id,), 0)
        try:
            page = await self._page(session, archive, "scrollback", muc=group, with_="" if group else chat.jid,
                                    before=before, max_=50)
        except Exception as e:
            log.info("loading older messages failed: %s", e)
            return False
        after_count = await self.core.db.scalar("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat.id,), 0)
        if page.complete or page.count == 0 or not page.first:
            chat.options["history_complete"] = True
            await self.core.chats.save(chat, "options")
        return after_count > before_count

    # ------------------------------------------------------------------ server archive preferences (XEP-0441)
    async def get_prefs(self, session) -> str:
        res = await session.iq("get", None, ET.Element("{%s}prefs" % NS_MAM))
        p = res.find("{%s}prefs" % NS_MAM)
        return p.get("default", "always") if p is not None else "always"

    async def set_prefs(self, session, default: str) -> None:
        p = ET.Element("{%s}prefs" % NS_MAM, {"default": default})
        ET.SubElement(p, "{%s}always" % NS_MAM)
        ET.SubElement(p, "{%s}never" % NS_MAM)
        await session.iq("set", None, p)
