"""Contacts, presence and subscriptions (RFC 6121), blocking (XEP-0191) and spam reports (XEP-0377), nicknames
(XEP-0172) and avatars (XEP-0084 with the vCard fallback of XEP-0153).

The roster is kept in the database with roster versioning, so large contact lists load instantly on reconnect.
Subscription jargon never reaches the interface: ContactItem has sees_us / we_see, and requests show up in the
Requests section."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import time
from xml.etree import ElementTree as ET

from gi.repository import Gio, GObject

from .. import aio, paths, util
from .models import ContactItem
from .pep import Pep
from .stanzas import NS_CLIENT, parse_stamp

log = logging.getLogger("ward.roster")

NS_ROSTER = "jabber:iq:roster"
NS_BLOCKING = "urn:xmpp:blocking"
NS_REPORTING = "urn:xmpp:reporting:1"
NS_NICK = "http://jabber.org/protocol/nick"
NS_AVATAR_META = "urn:xmpp:avatar:metadata"
NS_AVATAR_DATA = "urn:xmpp:avatar:data"
NS_VCARD = "vcard-temp"
NS_VCARD_UPDATE = "vcard-temp:x:update"
NS_IDLE = "urn:xmpp:idle:1"
NS_CAPS = "http://jabber.org/protocol/caps"
NS_MUC_USER = "http://jabber.org/protocol/muc#user"
NS_MUC = "http://jabber.org/protocol/muc"


def now_ms() -> int:
    return int(time.time() * 1000)


class Contacts(GObject.Object):
    """Every contact of every account (the Contacts page filters by account)."""
    __gsignals__ = {
        "added": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "removed": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "changed": (GObject.SignalFlags.RUN_LAST, None, (object,)),
    }

    def __init__(self, db):
        super().__init__()
        self.db = db
        self.store = Gio.ListStore.new(ContactItem)
        self.by_key: dict[tuple[int, str], ContactItem] = {}

    def get(self, account_id: int, jid: str) -> ContactItem | None:
        return self.by_key.get((account_id, util.bare(jid)))

    def for_account(self, account_id: int) -> list[ContactItem]:
        return [c for (a, _j), c in self.by_key.items() if a == account_id]

    async def load(self) -> None:
        rows = await self.db.fetchall("SELECT * FROM contacts")
        self.store.remove_all()
        self.by_key.clear()
        items = [self._from_row(r) for r in rows]
        for c in items:
            self.by_key[(c.account_id, c.jid)] = c
        self.store.splice(0, 0, items)

    @staticmethod
    def _from_row(r) -> ContactItem:
        c = ContactItem(id=r["id"], account_id=r["account_id"], jid=r["jid"], roster_name=r["name"],
                        nickname=r["nickname"], pep_nick=r["pep_nick"], subscription=r["subscription"], ask=r["ask"],
                        in_roster=bool(r["in_roster"]), avatar_hash=r["avatar_hash"], blocked=bool(r["blocked"]),
                        is_gateway=bool(r["is_gateway"]), last_seen=r["last_seen"] or 0)
        try:
            c.set_groups(json.loads(r["groups"] or "[]"))
        except ValueError:
            pass
        if c.avatar_hash:
            p = avatar_path(c.avatar_hash)
            if os.path.exists(p):
                c.avatar = p
        c.refresh_name()
        return c

    async def ensure(self, account_id: int, jid: str, in_roster: bool = False) -> ContactItem:
        jid = util.bare(jid)
        c = self.get(account_id, jid)
        if c is not None:
            return c

        def job(conn):
            conn.execute("INSERT OR IGNORE INTO contacts (account_id, jid, in_roster) VALUES (?, ?, ?)",
                         (account_id, jid, int(in_roster)))
            return conn.execute("SELECT * FROM contacts WHERE account_id = ? AND jid = ?", (account_id, jid)).fetchone()

        row = await self.db.run(job)
        c = self.get(account_id, jid)
        if c is not None:
            return c
        c = self._from_row(row)
        self.by_key[(account_id, jid)] = c
        self.store.append(c)
        self.emit("added", c)
        return c

    async def save(self, c: ContactItem) -> None:
        await self.db.execute(
            "UPDATE contacts SET name = ?, nickname = ?, pep_nick = ?, groups = ?, subscription = ?, ask = ?, "
            "in_roster = ?, avatar_hash = ?, blocked = ?, is_gateway = ?, last_seen = ? WHERE account_id = ? AND jid = ?",
            (c.roster_name, c.nickname, c.pep_nick, json.dumps(c.groups), c.subscription, c.ask, int(c.in_roster),
             c.avatar_hash, int(c.blocked), int(c.is_gateway), c.last_seen or None, c.account_id, c.jid))
        c.refresh_name()
        self.emit("changed", c)

    def drop(self, account_id: int) -> None:
        for key in [k for k in self.by_key if k[0] == account_id]:
            c = self.by_key.pop(key)
            for i in range(self.store.get_n_items()):
                if self.store.get_item(i) is c:
                    self.store.remove(i)
                    break


def avatar_path(sha1: str) -> str:
    return os.path.join(paths.avatars_cache(), sha1 + ".img")


def save_avatar(data: bytes) -> tuple[str, str]:
    sha1 = hashlib.sha1(data).hexdigest()
    p = avatar_path(sha1)
    if not os.path.exists(p):
        paths.ensure(paths.avatars_cache())
        tmp = p + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, p)
    return sha1, p


class RosterService(GObject.Object):
    __gsignals__ = {
        # (account, jid, kind) kind: subscribe (a request) | subscribed | unsubscribed
        "subscription": (GObject.SignalFlags.RUN_LAST, None, (object, str, str)),
    }

    def __init__(self, core):
        super().__init__()
        self.core = core
        self.contacts = Contacts(core.db)
        self.blocklist_supported: dict[int, bool] = {}
        self._vcard_fetching: set[tuple[int, str]] = set()
        core.pep.register(NS_AVATAR_META, self._on_avatar_meta)
        core.pep.register(NS_NICK, self._on_nick)

    iq_handlers = {(NS_ROSTER, "query"): "_on_roster_push", (NS_BLOCKING, "block"): "_on_block_push",
                   (NS_BLOCKING, "unblock"): "_on_block_push"}

    def attach(self, session) -> None:
        session.connect("presence", lambda s, el: self._on_presence(s, el))

    # ================================================================== roster
    async def on_online(self, session, resumed: bool) -> None:
        if resumed:
            return
        await self.fetch_roster(session)

    async def fetch_roster(self, session) -> None:
        acc = session.account
        ver_key = "roster_ver:%d" % acc.id
        versioning = "rosterver" in getattr(session.xmpp, "features", set())
        ver = (await self.core.db.kv_get(ver_key, "")) if versioning else None
        q = ET.Element("{%s}query" % NS_ROSTER)
        if ver is not None:
            q.set("ver", ver)
        res = await session.iq("get", None, q)
        query = res.find("{%s}query" % NS_ROSTER)
        if query is None:
            return  # unchanged since our version: pushes will follow
        items = {util.bare(i.get("jid", "")): i for i in query.findall("{%s}item" % NS_ROSTER) if i.get("jid")}
        for item in items.values():
            await self._apply_item(acc.id, item)
        for c in self.contacts.for_account(acc.id):
            if c.in_roster and c.jid not in items:
                c.update(in_roster=False, subscription="none", ask="")
                await self.contacts.save(c)
        if query.get("ver") is not None:
            await self.core.db.kv_set(ver_key, query.get("ver", ""))

    async def _apply_item(self, account_id: int, item: ET.Element) -> ContactItem | None:
        jid = util.bare(item.get("jid", ""))
        if not jid:
            return None
        sub = item.get("subscription", "none")
        c = await self.contacts.ensure(account_id, jid, in_roster=sub != "remove")
        if sub == "remove":
            c.update(in_roster=False, subscription="none", ask="")
        else:
            c.update(in_roster=True, subscription=sub if sub in ("none", "to", "from", "both") else "none",
                     ask=item.get("ask", ""), roster_name=item.get("name", ""))
            c.set_groups([g.text or "" for g in item.findall("{%s}group" % NS_ROSTER) if g.text])
            c.is_gateway = "@" not in jid
        await self.contacts.save(c)
        return c

    def _on_roster_push(self, session, iq) -> None:
        frm = iq["from"].bare if iq["from"] else ""
        if frm and frm != session.jid:
            session.reply_iq(iq, error="service-unavailable")   # spoofed roster push
            return
        query = iq.xml.find("{%s}query" % NS_ROSTER)
        session.reply_iq(iq)
        if query is None:
            return

        async def apply():
            for item in query.findall("{%s}item" % NS_ROSTER):
                await self._apply_item(session.account.id, item)
            if query.get("ver") is not None:
                await self.core.db.kv_set("roster_ver:%d" % session.account.id, query.get("ver", ""))
        aio.spawn(apply())

    async def _roster_set(self, session, jid: str, name: str | None = None, groups: list[str] | None = None,
                          remove: bool = False) -> None:
        q = ET.Element("{%s}query" % NS_ROSTER)
        item = ET.SubElement(q, "{%s}item" % NS_ROSTER, {"jid": jid})
        if remove:
            item.set("subscription", "remove")
        else:
            if name:
                item.set("name", name)
            for g in groups or []:
                ET.SubElement(item, "{%s}group" % NS_ROSTER).text = g
        await session.iq("set", None, q)

    async def add_contact(self, session, jid: str, name: str = "", groups: list[str] | None = None,
                          message: str = "") -> ContactItem:
        jid = util.bare(jid)
        await self._roster_set(session, jid, name, groups)
        nick = session.account.nickname
        extra = []
        if nick:
            n = ET.Element("{%s}nick" % NS_NICK)
            n.text = nick
            extra.append(n)
        session.send_presence(to=jid, ptype="subscribe", status=message, extra=extra)
        # pre-approve their request for our presence, so it is mutual in one step (RFC 6121 3.4)
        if "presence_preapproval" in getattr(session.xmpp, "features", set()) or True:
            session.send_presence(to=jid, ptype="subscribed")
        c = await self.contacts.ensure(session.account.id, jid, in_roster=True)
        if name and not c.roster_name:
            c.roster_name = name
            await self.contacts.save(c)
        return c

    async def remove_contact(self, session, jid: str) -> None:
        await self._roster_set(session, util.bare(jid), remove=True)

    async def rename(self, session, c: ContactItem, name: str) -> None:
        await self._roster_set(session, c.jid, name, c.groups)

    async def set_groups(self, session, c: ContactItem, groups: list[str]) -> None:
        await self._roster_set(session, c.jid, c.roster_name, groups)

    async def set_local_nickname(self, c: ContactItem, nickname: str) -> None:
        c.nickname = nickname
        await self.contacts.save(c)

    def accept(self, session, jid: str) -> None:
        """Let them see our status, and ask to see theirs if we don't yet."""
        jid = util.bare(jid)
        session.send_presence(to=jid, ptype="subscribed")
        c = self.contacts.get(session.account.id, jid)
        if c is None or not c.we_see:
            session.send_presence(to=jid, ptype="subscribe")

    def decline(self, session, jid: str) -> None:
        session.send_presence(to=util.bare(jid), ptype="unsubscribed")

    def set_sharing(self, session, jid: str, share_mine: bool | None = None, see_theirs: bool | None = None) -> None:
        jid = util.bare(jid)
        if share_mine is not None:
            session.send_presence(to=jid, ptype="subscribed" if share_mine else "unsubscribed")
        if see_theirs is not None:
            session.send_presence(to=jid, ptype="subscribe" if see_theirs else "unsubscribe")

    # ================================================================== presence
    def _on_presence(self, session, el: ET.Element) -> None:
        frm = el.get("from", "")
        if not frm:
            return
        # Group chat presence belongs to the group service
        if el.find("{%s}x" % NS_MUC_USER) is not None or self.core.muc.is_room(session.account.id, util.bare(frm)):
            self.core.muc.on_presence(session, el)
            return
        ptype = el.get("type", "")
        bare, res = util.bare(frm), util.resource(frm)
        acc = session.account
        if bare == acc.jid:
            if ptype in ("", "unavailable"):
                self._own_device_presence(session, res, el, ptype)
            return
        if ptype in ("subscribe", "subscribed", "unsubscribe", "unsubscribed"):
            aio.spawn(self._on_subscription(session, bare, ptype, el))
            return
        if ptype == "error":
            return
        c = self.contacts.get(acc.id, bare)
        if c is None:
            if ptype == "":
                aio.spawn(self._presence_unknown(session, bare, res, el))
            return
        self._apply_presence(session, c, res, ptype, el)

    async def _presence_unknown(self, session, bare, res, el) -> None:
        c = await self.contacts.ensure(session.account.id, bare)
        self._apply_presence(session, c, res, "", el)

    def _apply_presence(self, session, c: ContactItem, res: str, ptype: str, el: ET.Element) -> None:
        if ptype == "unavailable":
            c.resources.pop(res, None)
            if not c.resources:
                c.last_seen = now_ms()
        else:
            show = (el.findtext("{%s}show" % NS_CLIENT) or "available").strip() or "available"
            if show not in ("away", "chat", "dnd", "xa"):
                show = "available"
            try:
                prio = int(el.findtext("{%s}priority" % NS_CLIENT) or 0)
            except ValueError:
                prio = 0
            info = {"show": show, "status": el.findtext("{%s}status" % NS_CLIENT) or "", "priority": prio}
            caps = el.find("{%s}c" % NS_CAPS)
            if caps is not None:
                info["caps"] = "%s#%s" % (caps.get("node", ""), caps.get("ver", ""))
            idle = el.find("{%s}idle" % NS_IDLE)
            info["idle"] = parse_stamp(idle.get("since")) if idle is not None else None
            c.resources[res] = info
            upd = el.find("{%s}x/{%s}photo" % (NS_VCARD_UPDATE, NS_VCARD_UPDATE))
            if upd is not None and upd.text and upd.text.strip() != c.avatar_hash:
                aio.spawn(self.fetch_vcard_avatar(session, c, upd.text.strip()))
        show, status = c.best_presence()
        idles = [r.get("idle") for r in c.resources.values() if r.get("idle")]
        c.update(show=show, status=status, idle_since=int(min(idles) * 1000) if idles else 0)
        self.core.on_contact_presence(session.account, c)

    def _own_device_presence(self, session, res: str, el: ET.Element, ptype: str) -> None:
        devices = self.core.own_devices.setdefault(session.account.id, {})
        if ptype == "unavailable":
            devices.pop(res, None)
        elif res != util.resource(session.bound_jid):
            devices[res] = {"show": el.findtext("{%s}show" % NS_CLIENT) or "available"}

    async def _on_subscription(self, session, bare: str, ptype: str, el: ET.Element) -> None:
        acc = session.account
        c = await self.contacts.ensure(acc.id, bare)
        nick = el.findtext("{%s}nick" % NS_NICK)
        if nick and not c.pep_nick:
            c.pep_nick = nick
            await self.contacts.save(c)
        if ptype == "subscribe":
            if c.blocked:
                return
            if c.sees_us:
                session.send_presence(to=bare, ptype="subscribed")   # already approved: re-confirm
                return
            if c.ask == "subscribe" or c.we_see:
                # we asked them first (or already see them): mutual sharing is what the user wanted
                session.send_presence(to=bare, ptype="subscribed")
                return
            policy = self.core.settings["privacy.strangers"]
            if policy == "block" and not c.in_roster:
                session.send_presence(to=bare, ptype="unsubscribed")
                return
            await self.core.requests.add_request(acc, bare, el.findtext("{%s}status" % NS_CLIENT) or "")
            self.emit("subscription", acc, bare, ptype)
        else:
            self.emit("subscription", acc, bare, ptype)

    def send_initial_presence(self, session) -> None:
        acc = session.account
        show = acc.show if acc.show != "offline" else "available"
        session.send_presence(show=show, status=acc.status)

    # ================================================================== blocking
    async def fetch_blocklist(self, session) -> None:
        try:
            res = await session.iq("get", None, ET.Element("{%s}blocklist" % NS_BLOCKING))
        except Exception:
            self.blocklist_supported[session.account.id] = False
            return
        self.blocklist_supported[session.account.id] = True
        blocked = {util.bare(i.get("jid", "")) for i in res.iter("{%s}item" % NS_BLOCKING)}
        for c in self.contacts.for_account(session.account.id):
            if c.blocked != (c.jid in blocked):
                c.blocked = c.jid in blocked
                await self.contacts.save(c)
        for jid in blocked:
            c = await self.contacts.ensure(session.account.id, jid)
            if not c.blocked:
                c.blocked = True
                await self.contacts.save(c)

    async def block(self, session, jid: str, report_spam: bool = False, reason_text: str = "") -> None:
        b = ET.Element("{%s}block" % NS_BLOCKING)
        item = ET.SubElement(b, "{%s}item" % NS_BLOCKING, {"jid": util.bare(jid)})
        if report_spam:
            r = ET.SubElement(item, "{%s}report" % NS_REPORTING, {"reason": "urn:xmpp:reporting:spam"})
            if reason_text:
                ET.SubElement(r, "{%s}text" % NS_REPORTING).text = reason_text
        await session.iq("set", None, b)
        c = await self.contacts.ensure(session.account.id, jid)
        c.blocked = True
        await self.contacts.save(c)

    async def unblock(self, session, jid: str) -> None:
        u = ET.Element("{%s}unblock" % NS_BLOCKING)
        ET.SubElement(u, "{%s}item" % NS_BLOCKING, {"jid": util.bare(jid)})
        await session.iq("set", None, u)
        c = await self.contacts.ensure(session.account.id, jid)
        c.blocked = False
        await self.contacts.save(c)

    def _on_block_push(self, session, iq) -> None:
        frm = iq["from"].bare if iq["from"] else ""
        if frm and frm != session.jid:
            session.reply_iq(iq, error="service-unavailable")
            return
        session.reply_iq(iq)
        el = iq.xml
        block = el.find("{%s}block" % NS_BLOCKING)
        unblock = el.find("{%s}unblock" % NS_BLOCKING)

        async def apply():
            if block is not None:
                for i in block.findall("{%s}item" % NS_BLOCKING):
                    c = await self.contacts.ensure(session.account.id, i.get("jid", ""))
                    c.blocked = True
                    await self.contacts.save(c)
            if unblock is not None:
                items = unblock.findall("{%s}item" % NS_BLOCKING)
                targets = [self.contacts.get(session.account.id, i.get("jid", "")) for i in items] if items else \
                    [c for c in self.contacts.for_account(session.account.id) if c.blocked]
                for c in targets:
                    if c is not None:
                        c.blocked = False
                        await self.contacts.save(c)
        aio.spawn(apply())

    # ================================================================== nick and avatars
    async def _on_nick(self, session, frm: str, items, retracted) -> None:
        nick = ""
        for it in items:
            nick = it.findtext("{%s}nick" % NS_NICK) or ""
        if frm == session.jid:
            if nick and not session.account.nickname:
                session.account.nickname = nick
            return
        c = self.contacts.get(session.account.id, frm)
        if c is not None and c.pep_nick != nick:
            c.pep_nick = nick
            await self.contacts.save(c)

    async def _on_avatar_meta(self, session, frm: str, items, retracted) -> None:
        for it in items:
            info = it.find("{%s}metadata/{%s}info" % (NS_AVATAR_META, NS_AVATAR_META))
            sha1 = info.get("id", "") if info is not None else ""
            await self._set_avatar_hash(session, frm, sha1, from_pep=True)

    async def _set_avatar_hash(self, session, jid: str, sha1: str, from_pep: bool) -> None:
        acc = session.account
        own = jid == acc.jid
        target = None if own else self.contacts.get(acc.id, jid)
        if not own and target is None:
            return
        if not sha1:
            if own:
                acc.avatar = ""
            else:
                target.update(avatar="", avatar_hash="")
                await self.contacts.save(target)
            return
        p = avatar_path(sha1)
        if not os.path.exists(p) and from_pep:
            try:
                got = await Pep.get_items(session, None if own else jid, NS_AVATAR_DATA, [sha1])
                data_el = got[0].find("{%s}data" % NS_AVATAR_DATA) if got else None
                if data_el is not None and data_el.text:
                    data = base64.b64decode(data_el.text.strip())
                    if hashlib.sha1(data).hexdigest() == sha1:
                        save_avatar(data)
            except Exception as e:
                log.debug("avatar of %s: %s", jid, e)
        if os.path.exists(p):
            if own:
                acc.avatar = p
            else:
                target.update(avatar=p, avatar_hash=sha1)
                await self.contacts.save(target)

    async def fetch_vcard_avatar(self, session, c: ContactItem, sha1: str) -> None:
        key = (c.account_id, c.jid)
        if key in self._vcard_fetching:
            return
        if os.path.exists(avatar_path(sha1)):
            c.update(avatar=avatar_path(sha1), avatar_hash=sha1)
            await self.contacts.save(c)
            return
        self._vcard_fetching.add(key)
        try:
            res = await session.iq("get", c.jid, ET.Element("{%s}vCard" % NS_VCARD))
            binval = res.findtext(".//{%s}PHOTO/{%s}BINVAL" % (NS_VCARD, NS_VCARD))
            if binval:
                data = base64.b64decode("".join(binval.split()))
                got, p = save_avatar(data)
                c.update(avatar=p, avatar_hash=got)
                await self.contacts.save(c)
        except Exception as e:
            log.debug("vCard avatar of %s: %s", c.jid, e)
        finally:
            self._vcard_fetching.discard(key)

    async def fetch_own_profile(self, session) -> None:
        with contextlib.suppress(Exception):
            items = await Pep.get_items(session, None, NS_AVATAR_META, max_items=1)
            for it in items:
                info = it.find("{%s}metadata/{%s}info" % (NS_AVATAR_META, NS_AVATAR_META))
                if info is not None:
                    await self._set_avatar_hash(session, session.jid, info.get("id", ""), True)
        with contextlib.suppress(Exception):
            items = await Pep.get_items(session, None, NS_NICK, max_items=1)
            for it in items:
                nick = it.findtext("{%s}nick" % NS_NICK)
                if nick and not session.account.nickname:
                    session.account.nickname = nick

    async def publish_avatar(self, session, data: bytes, mime: str, width: int, height: int) -> None:
        sha1, p = save_avatar(data)
        d = ET.Element("{%s}data" % NS_AVATAR_DATA)
        d.text = base64.b64encode(data).decode()
        opts = {"pubsub#access_model": "open"}
        await Pep.publish(session, NS_AVATAR_DATA, d, sha1, opts)
        meta = ET.Element("{%s}metadata" % NS_AVATAR_META)
        ET.SubElement(meta, "{%s}info" % NS_AVATAR_META, {"id": sha1, "type": mime, "bytes": str(len(data)),
                                                           "width": str(width), "height": str(height)})
        await Pep.publish(session, NS_AVATAR_META, meta, sha1, opts)
        session.account.avatar = p

    async def publish_nick(self, session, nick: str) -> None:
        n = ET.Element("{%s}nick" % NS_NICK)
        n.text = nick
        await Pep.publish(session, NS_NICK, n, "current", {"pubsub#access_model": "presence"})
