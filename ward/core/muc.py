"""Group chats (XEP-0045) with two presets, so nobody has to understand room configuration:

    private group   members-only, non-anonymous, persistent, archived: can be encrypted with OMEMO
    public channel  open, members can't see each other's addresses, optionally listed

Plus occupant ids (XEP-0421), hats (XEP-0317), moderation (XEP-0425), invites (XEP-0249 and mediated), bookmarks
synced across devices (XEP-0402, reading XEP-0048 on old servers), self-ping (XEP-0410) to notice silent drops,
voice requests, room archives (MAM) and public channel search (XEP-0433)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from gi.repository import Gio, GObject

from .. import aio, util
from ..i18n import N_, _
from . import forms, stanzas
from .chat import Incoming
from .models import ROLE_ORDER, ChatItem, MessageItem, OccupantItem
from .pep import Pep
from .stanzas import NS_CLIENT, ParsedMessage, parse_error, parse_muc_user

log = logging.getLogger("ward.muc")

NS_MUC = "http://jabber.org/protocol/muc"
NS_MUC_USER = "http://jabber.org/protocol/muc#user"
NS_MUC_ADMIN = "http://jabber.org/protocol/muc#admin"
NS_MUC_OWNER = "http://jabber.org/protocol/muc#owner"
NS_MUC_REQUEST = "http://jabber.org/protocol/muc#request"
NS_BOOKMARKS2 = "urn:xmpp:bookmarks:1"
NS_BOOKMARKS_OLD = "storage:bookmarks"
NS_PRIVATE = "jabber:iq:private"
NS_OCCUPANT_ID = "urn:xmpp:occupant-id:0"
NS_HATS = "urn:xmpp:hats:0"
NS_PING = "urn:xmpp:ping"
NS_CHANNEL_SEARCH = "urn:xmpp:channel-search:0:search"
NS_VCARD_UPDATE = "vcard-temp:x:update"
DEFAULT_SEARCH = "api@search.jabber.network"
SELF_PING_EVERY = 15 * 60

JOIN_ERRORS = {
    "conflict": N_("That nickname is already in use in this group"),
    "not-authorized": N_("This group needs a password"),
    "registration-required": N_("This group is for members only: ask an admin to invite you"),
    "forbidden": N_("You are banned from this group"),
    "item-not-found": N_("This group does not exist"),
    "not-allowed": N_("You are not allowed to create groups on this server"),
    "service-unavailable": N_("This group is full"),
    "jid-malformed": N_("Your nickname is not valid here"),
    "remote-server-not-found": N_("The group's server could not be reached"),
}

PRESETS = {
    "private": {"muc#roomconfig_membersonly": True, "muc#roomconfig_whois": "anyone",
                "muc#roomconfig_persistentroom": True, "muc#roomconfig_publicroom": False,
                "muc#roomconfig_enablearchiving": True, "muc#roomconfig_enablelogging": True,
                "muc#roomconfig_moderatedroom": False, "muc#roomconfig_allowinvites": False,
                "muc#roomconfig_changesubject": True},
    "public": {"muc#roomconfig_membersonly": False, "muc#roomconfig_whois": "moderators",
               "muc#roomconfig_persistentroom": True, "muc#roomconfig_publicroom": True,
               "muc#roomconfig_enablearchiving": True, "muc#roomconfig_enablelogging": True,
               "muc#roomconfig_moderatedroom": False, "muc#roomconfig_allowinvites": True,
               "muc#roomconfig_changesubject": False},
}


@dataclass
class Room:
    account_id: int
    jid: str
    chat: ChatItem
    nick: str = ""
    password: str = ""
    occupants: Gio.ListStore = field(default_factory=lambda: Gio.ListStore.new(OccupantItem))
    by_nick: dict[str, OccupantItem] = field(default_factory=dict)
    by_occupant_id: dict[str, OccupantItem] = field(default_factory=dict)
    features: set[str] = field(default_factory=set)
    info: forms.Form | None = None
    joined: bool = False
    joining: asyncio.Future | None = None
    self_occupant_id: str = ""
    members: set[str] = field(default_factory=set)      # real JIDs with an affiliation (for encryption)
    created: bool = False
    initial: bool = True                                # still receiving the presence flood of joining
    last_activity: float = 0.0

    @property
    def mam(self) -> bool:
        return "urn:xmpp:mam:2" in self.features

    @property
    def members_only(self) -> bool:
        return "muc_membersonly" in self.features

    @property
    def non_anonymous(self) -> bool:
        return "muc_nonanonymous" in self.features


class MucError(Exception):
    def __init__(self, condition: str, text: str = ""):
        super().__init__(text or _(JOIN_ERRORS.get(condition, condition)))
        self.condition = condition


class MucService(GObject.Object):
    __gsignals__ = {
        "joined": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "left": (GObject.SignalFlags.RUN_LAST, None, (object, str)),            # chat, reason
        "invite": (GObject.SignalFlags.RUN_LAST, None, (object, object)),       # account, dict
        "occupants-changed": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "voice-request": (GObject.SignalFlags.RUN_LAST, None, (object, str, str)),
    }

    def __init__(self, core):
        super().__init__()
        self.core = core
        self.rooms: dict[tuple[int, str], Room] = {}
        self._ping_task: dict[int, asyncio.Task] = {}
        core.pep.register(NS_BOOKMARKS2, self._on_bookmarks_event)

    # ================================================================== lookups
    def room(self, account_id: int, jid: str) -> Room | None:
        return self.rooms.get((account_id, util.bare(jid)))

    def room_of(self, chat: ChatItem) -> Room | None:
        return self.rooms.get((chat.account_id, chat.jid))

    def is_room(self, account_id: int, jid: str) -> bool:
        jid = util.bare(jid)
        return (account_id, jid) in self.rooms or self.core.chats.find(account_id, jid, "groupchat") is not None

    def occupants(self, chat: ChatItem) -> Gio.ListStore | None:
        r = self.room_of(chat)
        return r.occupants if r else None

    def occupant_by_id(self, chat: ChatItem, ident: str) -> OccupantItem | None:
        r = self.room_of(chat)
        if r is None:
            return None
        return r.by_occupant_id.get(ident) or r.by_nick.get(ident)

    def my_nick(self, chat: ChatItem) -> str:
        r = self.room_of(chat)
        if r and r.nick:
            return r.nick
        acc = self.core.accounts.get(chat.account_id)
        return chat.options.get("nick") or (acc.nickname if acc else "") or (util.localpart(acc.jid) if acc else "me")

    # ================================================================== lifecycle
    def attach(self, session) -> None:
        pass

    async def on_online(self, session, resumed: bool) -> None:
        acc = session.account
        if resumed:
            return
        for r in [r for (aid, _j), r in self.rooms.items() if aid == acc.id]:
            r.joined = False
            r.chat.joined = False
        bookmarks = await self.fetch_bookmarks(session)
        targets: dict[str, dict] = {}
        for b in bookmarks:
            if b.get("autojoin"):
                targets[b["jid"]] = b
            chat = self.core.chats.find(acc.id, b["jid"], "groupchat")
            if chat is None and b.get("autojoin"):
                chat = await self.core.chats.get_or_create(acc.id, b["jid"], "groupchat", name=b.get("name") or "")
            if chat is not None:
                changed = False
                for k in ("nick", "password"):
                    if b.get(k) and chat.options.get(k) != b[k]:
                        chat.options[k] = b[k]
                        changed = True
                if b.get("name") and not chat.name:
                    chat.name = b["name"]
                    changed = True
                if changed:
                    await self.core.chats.save(chat, "options", "name")
        for chat in self.core.chats.for_account(acc.id):
            if chat.type == "groupchat" and chat.open and chat.options.get("autojoin", True) and \
                    chat.jid not in targets:
                targets[chat.jid] = {"jid": chat.jid, "nick": chat.options.get("nick"),
                                     "password": chat.options.get("password")}
        for jid, b in targets.items():
            aio.spawn(self._join_quietly(acc, jid, b.get("nick"), b.get("password")))
        self._ping_task[acc.id] = aio.spawn(self._self_ping_loop(session))

    async def _join_quietly(self, acc, jid, nick, password) -> None:
        try:
            await self.join(acc, jid, nick, password, bookmark=False)
        except Exception as e:
            log.info("could not join %s: %s", jid, e)

    def on_offline(self, session) -> None:
        for (aid, _j), r in self.rooms.items():
            if aid == session.account.id:
                r.joined = False
                r.chat.joined = False
                r.occupants.remove_all()
                r.by_nick.clear()
                r.by_occupant_id.clear()
        t = self._ping_task.pop(session.account.id, None)
        if t is not None:
            t.cancel()

    # ================================================================== joining
    async def room_info(self, session, jid: str):
        from .disco import DiscoService

        return await DiscoService(self.core).info(session, jid)

    async def join(self, acc, jid: str, nick: str | None = None, password: str | None = None,
                   bookmark: bool = True, create: str | None = None) -> ChatItem:
        session = self.core.sessions.get(acc.id)
        if session is None or not session.online:
            raise MucError("service-unavailable", _("You are offline"))
        jid = util.bare(jid)
        chat = await self.core.chats.get_or_create(acc.id, jid, "groupchat")
        room = self.rooms.get((acc.id, jid))
        if room is None:
            room = Room(acc.id, jid, chat)
            self.rooms[(acc.id, jid)] = room
        if room.joined:
            return chat
        nick = nick or chat.options.get("nick") or acc.nickname or util.localpart(acc.jid)
        password = password if password is not None else chat.options.get("password", "")
        info = None
        if not create:
            with contextlib.suppress(Exception):
                info = await self.room_info(session, jid)
        if info is not None:
            room.features = info.features
            room.info = info.form("http://jabber.org/protocol/muc#roominfo")
            if not chat.name and info.name:
                chat.name = info.name
            self._update_encryptable(room)
        for attempt in range(3):
            room.nick = nick
            room.password = password or ""
            room.initial = True
            room.joining = aio.get_loop().create_future()
            x = ET.Element("{%s}x" % NS_MUC)
            hist = ET.SubElement(x, "{%s}history" % NS_MUC)
            if room.mam or create:
                hist.set("maxstanzas", "0")       # we catch up from the archive instead
            elif chat.last_activity:
                hist.set("since", stanzas.format_stamp(chat.last_activity / 1000))
            else:
                hist.set("maxstanzas", "50")
            if password:
                ET.SubElement(x, "{%s}password" % NS_MUC).text = password
            session.send_presence(show=acc.show if acc.show != "offline" else "available", status=acc.status,
                                  to="%s/%s" % (jid, nick), extra=[x])
            try:
                await asyncio.wait_for(room.joining, 30)
                break
            except MucError as e:
                if e.condition == "conflict" and attempt < 2:
                    nick = nick + "_"
                    continue
                chat.update(join_error=str(e), joined=False)
                raise
            except asyncio.TimeoutError:
                chat.update(join_error=_("The group did not answer"), joined=False)
                raise MucError("remote-server-timeout", _("The group did not answer")) from None
            finally:
                room.joining = None
        chat.update(joined=True, join_error="", our_nick=room.nick)
        if chat.options.get("nick") != room.nick or (password and chat.options.get("password") != password):
            chat.options["nick"] = room.nick
            if password:
                chat.options["password"] = password
            await self.core.chats.save(chat, "options")
        if room.created and not create:
            # an instant room: accept the default configuration so it unlocks
            with contextlib.suppress(Exception):
                await self.submit_config(session, jid, forms.Form(type="submit"))
        if info is None or create:
            with contextlib.suppress(Exception):
                info = await self.room_info(session, jid)
                room.features = info.features
                room.info = info.form("http://jabber.org/protocol/muc#roominfo")
                self._update_encryptable(room)
        if bookmark:
            with contextlib.suppress(Exception):
                await self.set_bookmark(session, chat, autojoin=True)
        self.emit("joined", chat)
        if room.non_anonymous and room.members_only:
            aio.spawn(self.refresh_members(session, room))
        if room.mam:
            aio.spawn(self.core.history.catch_up(session, jid, muc=True))
        self.core.e2e.refresh(chat)
        return chat

    def _update_encryptable(self, room: Room) -> None:
        room.chat.encryptable = room.members_only and room.non_anonymous

    async def leave(self, chat: ChatItem, reason: str = "", forget: bool = True) -> None:
        room = self.room_of(chat)
        session = self.core.sessions.get(chat.account_id)
        if room is not None and room.joined and session is not None and session.online:
            session.send_presence(to="%s/%s" % (chat.jid, room.nick), ptype="unavailable", status=reason)
        if room is not None:
            room.joined = False
            room.occupants.remove_all()
            room.by_nick.clear()
            room.by_occupant_id.clear()
        chat.joined = False
        if forget:
            chat.options["autojoin"] = False
            await self.core.chats.save(chat, "options")
            if session is not None and session.online:
                with contextlib.suppress(Exception):
                    await self.set_bookmark(session, chat, autojoin=False)

    # ================================================================== presence
    def on_presence(self, session, el: ET.Element) -> None:
        frm = el.get("from", "")
        jid, nick = util.bare(frm), util.resource(frm)
        room = self.rooms.get((session.account.id, jid))
        if room is None:
            return
        ptype = el.get("type", "")
        x = el.find("{%s}x" % NS_MUC_USER)
        mu = parse_muc_user(x) if x is not None else None
        status = mu.status if mu else set()
        if ptype == "error":
            err = el.find("{%s}error" % NS_CLIENT)
            cond, text = parse_error(err) if err is not None else ("undefined-condition", "")
            if room.joining is not None and not room.joining.done():
                room.joining.set_exception(MucError(cond, text))
            return
        is_self = 110 in status or (nick == room.nick and room.joining is not None)
        occ_el = el.find("{%s}occupant-id" % NS_OCCUPANT_ID)
        occ_id = occ_el.get("id", "") if occ_el is not None else ""
        if ptype == "unavailable":
            o = room.by_nick.pop(nick, None)
            if o is not None:
                self._remove_occupant(room, o)
                if 303 in status and mu and mu.nick:
                    # a nick change: same person, new name
                    o.nick = mu.nick
                    room.by_nick[mu.nick] = o
                    room.occupants.append(o)
                    if is_self:
                        room.nick = mu.nick
                        room.chat.our_nick = mu.nick
                    self._status_line(room, _("%s is now known as %s") % (nick, mu.nick), "nick")
                elif not room.initial:
                    reason = ""
                    if 307 in status:
                        reason = _("%s was removed from the group") % nick
                    elif 301 in status:
                        reason = _("%s was banned from the group") % nick
                    self._status_line(room, reason or _("%s left") % nick, "leave")
            if is_self and 303 not in status:
                why = ""
                if 307 in status:
                    why = _("You were removed from this group")
                elif 301 in status:
                    why = _("You were banned from this group")
                elif 321 in status or 322 in status:
                    why = _("You are no longer a member of this group")
                elif 332 in status:
                    why = _("The group's server is restarting")
                elif mu and mu.destroyed:
                    why = _("This group was closed") + (": " + mu.destroy_reason if mu.destroy_reason else "")
                room.joined = False
                room.chat.update(joined=False, join_error=why)
                room.occupants.remove_all()
                room.by_nick.clear()
                room.by_occupant_id.clear()
                self.emit("left", room.chat, why)
                if 332 in status:
                    acc = self.core.accounts.get(room.account_id)
                    aio.get_loop().call_later(30, lambda: aio.spawn(self._join_quietly(acc, room.jid, room.nick,
                                                                                       room.password)))
            self.emit("occupants-changed", room.chat)
            room.chat.member_count = room.occupants.get_n_items()
            return
        show = (el.findtext("{%s}show" % NS_CLIENT) or "available").strip() or "available"
        o = room.by_nick.get(nick)
        new = o is None
        if new:
            o = OccupantItem(nick=nick)
            room.by_nick[nick] = o
            room.occupants.append(o)
        o.update(role=(mu.role if mu else "") or "participant", affiliation=(mu.affiliation if mu else "") or "none",
                 real_jid=util.bare(mu.jid) if mu and mu.jid else o.real_jid, show=show,
                 status=el.findtext("{%s}status" % NS_CLIENT) or "", is_self=is_self)
        if occ_id:
            o.occupant_id = occ_id
            room.by_occupant_id[occ_id] = o
        hats = [h.get("title", "") for h in el.iter("{%s}hat" % NS_HATS) if h.get("title")]
        o.hats = ", ".join(hats)
        if o.real_jid and o.affiliation in ("owner", "admin", "member"):
            room.members.add(o.real_jid)
        if occ_id:
            aio.spawn(self._remember_occupant(room, o))
        if new and not room.initial and not is_self:
            self._status_line(room, _("%s joined") % nick, "join")
        if is_self:
            room.self_occupant_id = occ_id
            room.chat.update(our_role=o.role, our_affiliation=o.affiliation)
            if 201 in status:
                room.created = True
            if 210 in status:
                room.nick = nick
            if 100 in status or 172 in status:
                room.features.add("muc_nonanonymous")
                self._update_encryptable(room)
            room.joined = True
            room.initial = False
            if room.joining is not None and not room.joining.done():
                room.joining.set_result(True)
        room.chat.member_count = room.occupants.get_n_items()
        self.emit("occupants-changed", room.chat)

    def _remove_occupant(self, room: Room, o: OccupantItem) -> None:
        for i in range(room.occupants.get_n_items()):
            if room.occupants.get_item(i) is o:
                room.occupants.remove(i)
                break

    async def _remember_occupant(self, room: Room, o: OccupantItem) -> None:
        await self.core.db.execute("INSERT OR REPLACE INTO occupants (chat_id, occupant_id, nick, real_jid, last_seen) "
                                   "VALUES (?, ?, ?, ?, ?)", (room.chat.id, o.occupant_id, o.nick, o.real_jid,
                                                              int(time.time() * 1000)))

    def _status_line(self, room: Room, text: str, kind: str) -> None:
        """Join/leave/rename lines: shown in the open chat (never stored), collapsed by the interface."""
        mode = self.core.settings["chats.join_leave"]
        if mode == "hidden":
            return
        ml = self.core.chats.lists.get(room.chat.id)
        if ml is None:
            return
        self._status_seq = getattr(self, "_status_seq", 0) - 1
        m = MessageItem(id=self._status_seq, chat_id=room.chat.id, kind="status", body=text,
                        timestamp=int(time.time() * 1000), sender=kind)
        ml.add(m)

    # ================================================================== messages
    def _sender(self, room: Room, nick: str, occ_id: str) -> tuple[str, str, bool]:
        """(nick, real JID, is it us)."""
        o = room.by_occupant_id.get(occ_id) if occ_id else None
        o = o or room.by_nick.get(nick)
        real = o.real_jid if o is not None else ""
        ours = (bool(occ_id) and occ_id == room.self_occupant_id) or (not occ_id and nick == room.nick)
        acc = self.core.accounts.get(room.account_id)
        if real and acc is not None and real == acc.jid:
            ours = True
        return nick, real, ours

    async def on_groupchat(self, session, pm: ParsedMessage, el: ET.Element) -> None:
        room = self.rooms.get((session.account.id, util.bare(pm.from_jid)))
        if room is None:
            return
        nick = util.resource(pm.from_jid)
        mode = "catchup" if pm.delay is not None and util.bare(pm.delay_from or room.jid) == room.jid else "live"
        await self._room_message(session, room, pm, el, nick, mode, pm.stanza_ids.get(room.jid, ""),
                                 int((pm.delay or time.time()) * 1000))

    async def on_archived(self, session, pm: ParsedMessage, el: ET.Element, q, archive_id: str,
                          delay: float | None) -> None:
        room = self.rooms.get((session.account.id, q.archive))
        if room is None:
            return
        await self._room_message(session, room, pm, el, util.resource(pm.from_jid), q.mode, archive_id,
                                 int((delay or time.time()) * 1000))

    async def _room_message(self, session, room: Room, pm: ParsedMessage, el, nick: str, mode: str,
                            stanza_id: str, when: int) -> None:
        chat = room.chat
        if not nick:
            if pm.subject is not None or pm.muc_user is not None:
                await self._room_status(room, pm, mode)
            if pm.moderation is not None:
                await self._moderated(room, pm.moderation)
            return
        if pm.subject is not None and not pm.body:
            if chat.subject != pm.subject:
                chat.subject = pm.subject
                if mode == "live" and not room.initial:
                    self._status_line(room, _("%s changed the topic") % nick, "topic")
            return
        if pm.moderation is not None:
            await self._moderated(room, pm.moderation)
            return
        if el.find("{%s}x" % forms.NS) is not None and pm.body is None:
            f = forms.find(el)
            if f is not None and f.form_type == NS_MUC_REQUEST:
                self.emit("voice-request", chat, f.value("muc#roomnick"), f.value("muc#jid"))
            return
        occ_id = pm.occupant_id
        nick, real, ours = self._sender(room, nick, occ_id)
        if not real and pm.muc_user is not None and pm.muc_user.jid:
            real = util.bare(pm.muc_user.jid)
        inc = Incoming(pm, ours, room.jid + "/" + nick, when, mode, stanza_id)
        sender = occ_id or nick
        if pm.chat_state and mode == "live" and not ours:
            self.core.messaging._typing(chat, nick, pm.chat_state)
        if pm.encrypted:
            await self.core.e2e.decrypt(session, el, inc, is_pm=False)
            pm = inc.pm
        if pm.correction_of:
            row = await self.core.messaging._find(chat, pm.correction_of, direction=1 if ours else 0)
            if row is not None and (row["occupant_id"] == occ_id if occ_id else row["sender"] == nick):
                await self.core.messaging._do_correction(chat, row, pm.display_body(), pm.markdown, when)
            elif row is None:
                await self.core.messaging._pending(chat, pm.correction_of, "correction",
                                                   {"body": pm.display_body(), "sender": nick,
                                                    "direction": 1 if ours else 0, "markdown": pm.markdown})
            return
        if pm.retraction_of:
            row = await self.core.messaging._find(chat, pm.retraction_of)
            if row is not None and (row["occupant_id"] == occ_id if occ_id else row["sender"] == nick):
                await self.core.messaging.retract_row(chat, row["id"], 1)
            return
        if pm.reactions is not None:
            await self.core.messaging._apply_reactions(chat, pm.reactions[0], sender if not ours else
                                                       self.core.my_reaction_id(chat), pm.reactions[1], when)
            return
        body = pm.display_body()
        files = self.core.messaging._attachments(pm, body)
        if files and len(files) == 1 and body.strip() == files[0]["url"]:
            body = ""
        if not body.strip() and not files and not inc.decrypt_error:
            return
        if ours:
            # our own message, reflected by the room: it was delivered
            if pm.origin_id or pm.id:
                for ref in (pm.origin_id, pm.id):
                    row = await self.core.messaging._find(chat, ref, direction=1) if ref else None
                    if row is not None:
                        await self.core.chats.update_message_fields(row["id"], stanza_id=stanza_id or row["stanza_id"],
                                                                    status=max(row["status"], 2))
                        await self.core.chats.reload_message(chat, row["id"])
                        return
        await self.core.messaging.store(chat, inc, sender=nick, sender_jid=real, body=body, files=files,
                                        occupant_id=occ_id, groupchat=True)

    async def _room_status(self, room: Room, pm: ParsedMessage, mode: str) -> None:
        if pm.subject is not None:
            room.chat.subject = pm.subject
        mu = pm.muc_user
        if mu and mode == "live" and (104 in mu.status or 170 in mu.status or 171 in mu.status or
                                      172 in mu.status or 173 in mu.status or 174 in mu.status):
            session = self.core.sessions.get(room.account_id)
            if session is not None:
                with contextlib.suppress(Exception):
                    info = await self.room_info(session, room.jid)
                    room.features = info.features
                    self._update_encryptable(room)
                    self.core.e2e.refresh(room.chat)

    async def _moderated(self, room: Room, mod) -> None:
        row = await self.core.db.fetchone("SELECT id FROM messages WHERE chat_id = ? AND stanza_id = ?",
                                          (room.chat.id, mod.target))
        if row is None:
            await self.core.messaging._pending(room.chat, mod.target, "moderation", {"by": util.resource(mod.by),
                                                                                    "reason": mod.reason})
            return
        await self.core.messaging.retract_row(room.chat, row["id"], 3, {"moderated_by": util.resource(mod.by),
                                                                         "moderation_reason": mod.reason})

    # ================================================================== invites
    async def on_mediated_invite(self, session, pm: ParsedMessage) -> None:
        mu = pm.muc_user
        if mu is None or not mu.invite_from:
            return
        self.emit("invite", session.account, {"room": util.bare(pm.from_jid), "from": util.bare(mu.invite_from),
                                              "reason": mu.invite_reason, "password": mu.password})

    async def on_direct_invite(self, session, pm: ParsedMessage) -> None:
        room, password, reason = pm.direct_invite
        if not room:
            return
        c = self.core.roster.contacts.get(session.account.id, pm.from_jid)
        if c is not None and c.blocked:
            return
        self.emit("invite", session.account, {"room": util.bare(room), "from": util.bare(pm.from_jid),
                                              "reason": reason, "password": password})

    async def invite(self, chat: ChatItem, jid: str, reason: str = "") -> None:
        session = self.core.messaging.session_for(chat)
        room = self.room_of(chat)
        if room is not None and room.members_only and chat.our_affiliation in ("owner", "admin"):
            with contextlib.suppress(Exception):
                await self.set_affiliation(chat, jid, "member")
        el = session.new_message(util.bare(jid), "normal", util.random_id())
        stanzas.add_direct_invite(el, chat.jid, reason, chat.options.get("password", ""))
        session.send(el)

    # ================================================================== creating
    async def create_group(self, acc, name: str, preset: str = "private", invitees: list[str] | None = None,
                           description: str = "", listed: bool = False, address: str = "") -> ChatItem:
        from .disco import DiscoService

        session = self.core.sessions.get(acc.id)
        if session is None or not session.online:
            raise MucError("service-unavailable", _("You are offline"))
        service = DiscoService.muc_service(session)
        if service is None and not address:
            raise MucError("service-unavailable", _("Your server has no group chat service"))
        jid = address or "%s@%s" % (util.random_id(10).lower(), service)
        chat = await self.join(acc, jid, create=preset, bookmark=False)
        room = self.room_of(chat)
        form = await self.get_config(session, jid)
        values = dict(PRESETS.get(preset, PRESETS["private"]))
        values["muc#roomconfig_roomname"] = name
        if description:
            values["muc#roomconfig_roomdesc"] = description
        if preset == "public":
            values["muc#roomconfig_publicroom"] = bool(listed)
        for var, v in values.items():
            if form.get(var) is not None:
                form.set(var, v)
        await self.submit_config(session, jid, form)
        chat.name = name
        await self.core.chats.save(chat, "name")
        info = await self.room_info(session, jid)
        room.features = info.features
        self._update_encryptable(room)
        if preset == "private" and chat.encryptable:
            chat.encryption = "default"
        await self.set_bookmark(session, chat, autojoin=True)
        for j in invitees or []:
            with contextlib.suppress(Exception):
                await self.invite(chat, j)
        self.core.e2e.refresh(chat)
        return chat

    async def get_config(self, session, jid: str) -> forms.Form:
        res = await session.iq("get", jid, ET.Element("{%s}query" % NS_MUC_OWNER))
        q = res.find("{%s}query" % NS_MUC_OWNER)
        f = forms.find(q, res) if q is not None else None
        return f or forms.Form()

    async def submit_config(self, session, jid: str, form: forms.Form) -> None:
        q = ET.Element("{%s}query" % NS_MUC_OWNER)
        q.append(forms.to_element(form, "submit"))
        await session.iq("set", jid, q)

    async def destroy(self, chat: ChatItem, reason: str = "") -> None:
        session = self.core.messaging.session_for(chat)
        q = ET.Element("{%s}query" % NS_MUC_OWNER)
        d = ET.SubElement(q, "{%s}destroy" % NS_MUC_OWNER)
        if reason:
            ET.SubElement(d, "{%s}reason" % NS_MUC_OWNER).text = reason
        await session.iq("set", chat.jid, q)
        await self.leave(chat)

    # ================================================================== moderation and settings
    async def _admin(self, chat: ChatItem, item: dict, reason: str = "") -> None:
        session = self.core.messaging.session_for(chat)
        q = ET.Element("{%s}query" % NS_MUC_ADMIN)
        it = ET.SubElement(q, "{%s}item" % NS_MUC_ADMIN, item)
        if reason:
            ET.SubElement(it, "{%s}reason" % NS_MUC_ADMIN).text = reason
        await session.iq("set", chat.jid, q)

    async def set_role(self, chat: ChatItem, nick: str, role: str, reason: str = "") -> None:
        await self._admin(chat, {"nick": nick, "role": role}, reason)

    async def kick(self, chat: ChatItem, nick: str, reason: str = "") -> None:
        await self.set_role(chat, nick, "none", reason)

    async def set_affiliation(self, chat: ChatItem, jid: str, affiliation: str, reason: str = "") -> None:
        await self._admin(chat, {"jid": util.bare(jid), "affiliation": affiliation}, reason)
        room = self.room_of(chat)
        if room is not None:
            if affiliation in ("owner", "admin", "member"):
                room.members.add(util.bare(jid))
            else:
                room.members.discard(util.bare(jid))

    async def ban(self, chat: ChatItem, jid: str, reason: str = "") -> None:
        await self.set_affiliation(chat, jid, "outcast", reason)

    async def affiliation_list(self, chat: ChatItem, affiliation: str) -> list[tuple[str, str]]:
        session = self.core.messaging.session_for(chat)
        q = ET.Element("{%s}query" % NS_MUC_ADMIN)
        ET.SubElement(q, "{%s}item" % NS_MUC_ADMIN, {"affiliation": affiliation})
        res = await session.iq("get", chat.jid, q)
        return [(util.bare(i.get("jid", "")), i.get("nick", "")) for i in res.iter("{%s}item" % NS_MUC_ADMIN)]

    async def refresh_members(self, session, room: Room) -> None:
        members = set()
        for aff in ("owner", "admin", "member"):
            try:
                for jid, _nick in await self.affiliation_list(room.chat, aff):
                    if jid:
                        members.add(jid)
            except Exception:
                # only admins may read the lists; everyone else learns members from presences
                return
        room.members = members

    def encryption_recipients(self, chat: ChatItem) -> set[str]:
        """Who an encrypted group message goes to: every member, not just who is online."""
        room = self.room_of(chat)
        if room is None:
            return set()
        acc = self.core.accounts.get(chat.account_id)
        out = set(room.members) | {o.real_jid for o in (room.occupants.get_item(i) for i in
                                                        range(room.occupants.get_n_items())) if o.real_jid}
        if acc is not None:
            out.discard(acc.jid)
        return out

    async def set_subject(self, chat: ChatItem, subject: str) -> None:
        session = self.core.messaging.session_for(chat)
        el = session.new_message(chat.jid, "groupchat", util.random_id())
        ET.SubElement(el, "{%s}subject" % NS_CLIENT).text = subject
        session.send(el)

    async def change_nick(self, chat: ChatItem, nick: str) -> None:
        session = self.core.messaging.session_for(chat)
        acc = session.account
        session.send_presence(show=acc.show if acc.show != "offline" else "available", status=acc.status,
                              to="%s/%s" % (chat.jid, nick))
        chat.options["nick"] = nick
        await self.core.chats.save(chat, "options")

    def can_moderate(self, chat: ChatItem) -> bool:
        """The room supports XEP-0425 and we are a moderator."""
        room = self.room_of(chat)
        if room is None or chat.our_role != "moderator":
            return False
        return bool(room.features & {"urn:xmpp:message-moderate:1", "urn:xmpp:message-moderate:0"})

    async def moderate(self, chat: ChatItem, msg: MessageItem, reason: str = "") -> None:
        """Delete someone's message for everyone (XEP-0425); needs moderator rights."""
        if not msg.stanza_id:
            raise MucError("bad-request", _("This message cannot be moderated"))
        session = self.core.messaging.session_for(chat)
        current, legacy = stanzas.moderation_request(msg.stanza_id, reason)
        room = self.room_of(chat)
        use_legacy = room is not None and "urn:xmpp:message-moderate:1" not in room.features and \
            "urn:xmpp:message-moderate:0" in room.features
        await session.iq("set", chat.jid, legacy if use_legacy else current)
        await self.core.messaging.retract_row(chat, msg.id, 3, {"moderated_by": chat.our_nick,
                                                               "moderation_reason": reason})

    def request_voice(self, chat: ChatItem) -> None:
        session = self.core.messaging.session_for(chat)
        el = session.new_message(chat.jid, "normal", util.random_id())
        f = forms.Form(type="submit")
        f.fields.append(forms.Field(var="FORM_TYPE", type="hidden", values=[NS_MUC_REQUEST]))
        f.set("muc#role", "participant")
        el.append(forms.to_element(f, "submit"))
        session.send(el)

    async def approve_voice(self, chat: ChatItem, nick: str, approve: bool = True) -> None:
        if approve:
            await self.set_role(chat, nick, "participant")

    # ================================================================== bookmarks
    async def fetch_bookmarks(self, session) -> list[dict]:
        out: list[dict] = []
        try:
            items = await Pep.get_items(session, None, NS_BOOKMARKS2)
            for it in items:
                conf = it.find("{%s}conference" % NS_BOOKMARKS2)
                if conf is None:
                    continue
                out.append({"jid": util.bare(it.get("id", "")), "name": conf.get("name", ""),
                            "autojoin": conf.get("autojoin") in ("true", "1"),
                            "nick": conf.findtext("{%s}nick" % NS_BOOKMARKS2) or "",
                            "password": conf.findtext("{%s}password" % NS_BOOKMARKS2) or ""})
            if out:
                return out
        except Exception as e:
            log.debug("XEP-0402 bookmarks: %s", e)
        try:
            q = ET.Element("{%s}query" % NS_PRIVATE)
            ET.SubElement(q, "{%s}storage" % NS_BOOKMARKS_OLD)
            res = await session.iq("get", None, q)
            for c in res.iter("{%s}conference" % NS_BOOKMARKS_OLD):
                out.append({"jid": util.bare(c.get("jid", "")), "name": c.get("name", ""),
                            "autojoin": c.get("autojoin") in ("true", "1"),
                            "nick": c.findtext("{%s}nick" % NS_BOOKMARKS_OLD) or "",
                            "password": c.findtext("{%s}password" % NS_BOOKMARKS_OLD) or ""})
        except Exception as e:
            log.debug("XEP-0048 bookmarks: %s", e)
        return out

    async def set_bookmark(self, session, chat: ChatItem, autojoin: bool = True) -> None:
        conf = ET.Element("{%s}conference" % NS_BOOKMARKS2, {"autojoin": "true" if autojoin else "false"})
        if chat.name:
            conf.set("name", chat.name)
        nick = chat.options.get("nick")
        if nick:
            ET.SubElement(conf, "{%s}nick" % NS_BOOKMARKS2).text = nick
        if chat.options.get("password"):
            ET.SubElement(conf, "{%s}password" % NS_BOOKMARKS2).text = chat.options["password"]
        await Pep.publish(session, NS_BOOKMARKS2, conf, chat.jid,
                          {"pubsub#persist_items": "true", "pubsub#max_items": "max",
                           "pubsub#send_last_published_item": "never", "pubsub#access_model": "whitelist"})

    async def remove_bookmark(self, session, chat: ChatItem) -> None:
        with contextlib.suppress(Exception):
            await Pep.retract(session, NS_BOOKMARKS2, chat.jid)

    async def _on_bookmarks_event(self, session, frm: str, items, retracted) -> None:
        if frm != session.jid:
            return
        acc = session.account
        for it in items:
            conf = it.find("{%s}conference" % NS_BOOKMARKS2)
            jid = util.bare(it.get("id", ""))
            if conf is None or not jid:
                continue
            if conf.get("autojoin") in ("true", "1") and not (self.room(acc.id, jid) or Room(0, "", None)).joined:
                aio.spawn(self._join_quietly(acc, jid, conf.findtext("{%s}nick" % NS_BOOKMARKS2),
                                             conf.findtext("{%s}password" % NS_BOOKMARKS2)))
        for jid in retracted:
            chat = self.core.chats.find(acc.id, util.bare(jid), "groupchat")
            room = self.room(acc.id, jid)
            if chat is not None and room is not None and room.joined:
                await self.leave(chat, forget=False)

    # ================================================================== discovery
    async def list_rooms(self, session, service: str) -> list[tuple[str, str]]:
        q = ET.Element("{http://jabber.org/protocol/disco#items}query")
        res = await session.iq("get", service, q)
        return [(i.get("jid", ""), i.get("name", "")) for i in res.iter("{http://jabber.org/protocol/disco#items}item")]

    async def search_channels(self, session, text: str, service: str = DEFAULT_SEARCH) -> list[dict]:
        """XEP-0433 public channel search (search.jabber.network by default)."""
        s = ET.Element("{%s}search" % NS_CHANNEL_SEARCH)
        s.append(forms.simple("urn:xmpp:channel-search:0:search-params",
                              {"q": text, "sinname": True, "sindescription": True, "key": "{%s}nusers"
                               % NS_CHANNEL_SEARCH}))
        res = await session.iq("get", service, s)
        out = []
        for item in res.iter("{urn:xmpp:channel-search:0:search}item"):
            def t(tag, item=item):
                return item.findtext("{%s}%s" % (NS_CHANNEL_SEARCH, tag)) or ""
            users = t("nusers")
            out.append({"jid": item.get("address", ""), "name": t("name"), "description": t("description"),
                        "users": int(users) if users.isdigit() else 0, "language": t("language"),
                        "open": item.find("{%s}is-open" % NS_CHANNEL_SEARCH) is not None})
        return out

    # ================================================================== self-ping
    async def _self_ping_loop(self, session) -> None:
        while True:
            await asyncio.sleep(SELF_PING_EVERY)
            if not session.online:
                continue
            for (aid, jid), room in list(self.rooms.items()):
                if aid != session.account.id or not room.joined:
                    continue
                try:
                    await session.iq("get", "%s/%s" % (jid, room.nick), ET.Element("{%s}ping" % NS_PING), timeout=60)
                except Exception as e:
                    cond = getattr(e, "condition", "")
                    if cond in ("not-acceptable", "item-not-found", "remote-server-not-found"):
                        log.info("dropped from %s (%s): rejoining", jid, cond)
                        room.joined = False
                        room.chat.joined = False
                        aio.spawn(self._join_quietly(session.account, jid, room.nick, room.password))

    def sorted_occupants(self, chat: ChatItem) -> list[OccupantItem]:
        r = self.room_of(chat)
        if r is None:
            return []
        items = [r.occupants.get_item(i) for i in range(r.occupants.get_n_items())]
        return sorted(items, key=lambda o: (ROLE_ORDER.get(o.role, 9), o.nick.lower()))
