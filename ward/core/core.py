"""Core: everything Ward knows and does, without any Gtk.  The interface holds one Core and binds to its models.

    core = Core(); await core.start()          # opens the database, loads accounts/contacts/chats, connects
    core.accounts.store, core.chats.store       # Gio.ListModels for the sidebar
    await core.chats.open_messages(chat)        # a MessageList for the conversation view
    await core.messaging.send_text(chat, text)  # ... and so on through the services
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from gi.repository import Gio, GLib, GObject

from .. import aio, util
from ..i18n import N_, _
from ..settings import Settings
from . import secrets
from .accounts import Accounts
from .admin import AdminService
from .calls import Calls
from .chat import ChatService
from .chats import Chats
from .disco import DiscoService
from .e2e import E2E
from .files import FileService
from .history import HistoryService
from .models import AccountItem, ChatItem, ContactItem
from .muc import MucService
from .names import Names
from .notify import Notifier
from .pep import Pep
from .requests import Requests
from .roster import RosterService
from .session import AccountSession
from .storage.db import Database
from .turn import TurnService

log = logging.getLogger("ward.core")

ERRORS = {
    "service-unavailable": N_("The recipient's server does not accept this"),
    "recipient-unavailable": N_("The recipient is not available"),
    "item-not-found": N_("This address does not exist"),
    "remote-server-not-found": N_("The recipient's server could not be found"),
    "remote-server-timeout": N_("The recipient's server did not answer"),
    "forbidden": N_("You are not allowed to do this"),
    "not-acceptable": N_("The message was not accepted"),
    "policy-violation": N_("The server refused this because of its rules"),
    "resource-constraint": N_("The server is too busy right now"),
    "not-allowed": N_("This is not allowed"),
    "feature-not-implemented": N_("This is not supported"),
}


class Core(GObject.Object):
    __gsignals__ = {
        "session-added": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "cert-problem": (GObject.SignalFlags.RUN_LAST, None, (object, object)),   # account, CertInfo
        "online": (GObject.SignalFlags.RUN_LAST, None, (object, bool)),           # account, resumed
        "omemo-ready": (GObject.SignalFlags.RUN_LAST, None, ()),                  # Core.omemo is set (or failed)
    }

    def __init__(self, settings: Settings | None = None, db: Database | None = None, connect: bool = True):
        super().__init__()
        self.settings = settings or Settings()
        self.db = db or Database()
        self.connect_on_start = connect
        self.sessions: dict[int, AccountSession] = {}
        self.own_devices: dict[int, dict] = {}
        self.active_chat: ChatItem | None = None
        self.window_focused = False
        self.window_visible = True
        self.started = False

        self.accounts = Accounts(self.db)
        self.turn = TurnService(self)
        self.pep = Pep(self)
        self.roster = RosterService(self)
        self.chats = Chats(self)
        self.requests = Requests(self)
        self.names = Names(self)
        self.calls = Calls(self)
        self.messaging = ChatService(self)
        self.history = HistoryService(self)
        self.disco = DiscoService(self)
        self.muc = MucService(self)
        self.files = FileService(self)
        self.e2e = E2E(self)
        self.admin = AdminService(self)
        self.notifier = Notifier(self)
        self.omemo = None
        self.omemo_error = ""
        self._omemo_loaded = False
        self.pgp = None
        try:
            from .pgp.service import PgpService

            self.pgp = PgpService(self)
        except Exception as e:
            log.info("OpenPGP unavailable: %s", e)
        self.accounts.connect("removed", lambda _a, acc: aio.spawn(self._account_removed(acc)))
        self._monitor: Any = None

    @property
    def contacts(self):
        return self.roster.contacts

    # ================================================================== lifecycle
    async def start(self) -> None:
        if self.started:
            return
        await aio.run_in_thread(self.db.open)
        await self.load_omemo()
        await self.accounts.load()
        await self.roster.contacts.load()
        await self.chats.load()
        await self.requests.load()
        for chat in self.chats.items():
            self.e2e.refresh(chat)
        self.started = True
        self._watch_network()
        if self.connect_on_start:
            for acc in self.accounts.list():
                if acc.enabled and acc.option("autoconnect"):
                    aio.spawn(self.connect_account(acc))
        aio.spawn(self._retention())

    async def load_omemo(self) -> None:
        """Create the OMEMO service once its libraries have loaded.  The application starts that import in the
        background while it builds the window (it takes about 170 ms), so this normally waits for nothing."""
        if self._omemo_loaded:
            return
        self._omemo_loaded = True
        from .omemo import preload, result

        await aio.run_in_thread(preload().join)
        module, error = result()
        if module is None:
            from .. import deps

            self.omemo_error = "; ".join("%s: %s" % kv for kv in deps.IMPORT_ERRORS.items()) or \
                ("%s: %s" % (type(error).__name__, error) if error is not None else "OMEMO is not installed")
            log.warning("OMEMO unavailable: %s", self.omemo_error)
            self.emit("omemo-ready")
            return
        try:
            self.omemo = module.OmemoService(self)
        except Exception as e:  # missing libraries: encryption shows as unavailable, everything else works
            from .. import deps

            self.omemo_error = "; ".join("%s: %s" % kv for kv in deps.IMPORT_ERRORS.items()) or \
                "%s: %s" % (type(e).__name__, e)
            log.warning("OMEMO unavailable: %s", self.omemo_error)
        self.emit("omemo-ready")

    async def shutdown(self) -> None:
        for s in list(self.sessions.values()):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(s.stop(), 4)
        self.settings.flush()
        await aio.run_in_thread(self.db.close)
        self.started = False

    def _watch_network(self) -> None:
        try:
            self._monitor = Gio.NetworkMonitor.get_default()
            self._monitor.connect("network-changed", self._on_network_changed)
        except Exception:
            self._monitor = None
        # logind / elogind: reconnect right after the machine wakes up
        with contextlib.suppress(Exception):
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            bus.signal_subscribe("org.freedesktop.login1", "org.freedesktop.login1.Manager", "PrepareForSleep",
                                 "/org/freedesktop/login1", None, Gio.DBusSignalFlags.NONE, self._on_sleep)

    def _on_network_changed(self, monitor, available: bool) -> None:
        if available:
            for s in self.sessions.values():
                s.retry_now()

    def _on_sleep(self, conn, sender, path, iface, signal, params) -> None:
        going_to_sleep = params.unpack()[0]
        if not going_to_sleep:
            GLib.timeout_add_seconds(2, self._retry_all)

    def _retry_all(self) -> bool:
        for s in self.sessions.values():
            s.retry_now()
        return False

    # ================================================================== sessions
    def session_for(self, acc: AccountItem) -> AccountSession:
        s = self.sessions.get(acc.id)
        if s is not None:
            return s
        handlers = {}
        for svc in (self.roster, self.omemo, self.pgp, self.calls):
            for key, meth in (getattr(svc, "iq_handlers", None) or {}).items():
                handlers[key] = getattr(svc, meth)
        s = AccountSession(acc, self.settings, handlers)
        s.xmpp = s._make_client()
        self.sessions[acc.id] = s
        self.pep.attach(s)
        self.roster.attach(s)
        self.messaging.attach(s)
        self.muc.attach(s)
        self.calls.attach(s)
        for svc in (self.omemo, self.pgp):
            if svc is not None:
                svc.attach(s)
        s.connect("online", lambda sess, resumed: aio.spawn(self._on_online(sess, resumed)))
        s.connect("offline", lambda sess: self._on_offline(sess))
        s.connect("cert-problem", lambda sess, info: self.emit("cert-problem", sess.account, info))
        self.emit("session-added", s)
        return s

    async def connect_account(self, acc: AccountItem, password: str | None = None) -> AccountSession:
        s = self.session_for(acc)
        await s.start(password)
        return s

    async def disconnect_account(self, acc: AccountItem) -> None:
        s = self.sessions.get(acc.id)
        if s is not None:
            await s.stop()

    async def _on_online(self, session: AccountSession, resumed: bool) -> None:
        acc = session.account
        acc.step = "syncing"
        steps = [self.disco.on_online, self.roster.on_online]
        for step in steps:
            try:
                await step(session, resumed)
            except Exception:
                log.exception("login step %s failed", getattr(step, "__qualname__", step))
        if not resumed:
            with contextlib.suppress(Exception):
                self.roster.send_initial_presence(session)
        session.enable_keepalive()
        if not self.window_visible:
            session.set_inactive(True)
        later: list[tuple[str, Callable[[], Awaitable[None]]]] = [
            ("messages", lambda: self.messaging.on_online(session, resumed)),
            ("admin", lambda: self.admin.on_online(session, resumed))]
        if not resumed:
            later += [("blocklist", lambda: self.roster.fetch_blocklist(session)),
                      ("profile", lambda: self.roster.fetch_own_profile(session))]
        for name, svc in (("omemo", self.omemo), ("openpgp", self.pgp)):
            if svc is not None:
                later.append((name, functools.partial(svc.on_online, session, resumed)))
        later += [("history", lambda: self.history.on_online(session, resumed)),
                  ("groups", lambda: self.muc.on_online(session, resumed))]
        for name, run in later:
            try:
                await run()
            except Exception:
                log.exception("login step %s failed", name)
        acc.step = ""
        for chat in self.chats.for_account(acc.id):
            self.e2e.refresh(chat)
        self.emit("online", acc, resumed)

    def _on_offline(self, session: AccountSession) -> None:
        self.calls.account_offline(session.account.id)
        self.muc.on_offline(session)
        for c in self.roster.contacts.for_account(session.account.id):
            if c.resources:
                c.resources.clear()
                c.update(show="offline", status="")
                self.on_contact_presence(session.account, c)
        for chat in self.chats.for_account(session.account.id):
            if chat.typing:
                chat.typing = ""

    # ================================================================== accounts
    async def add_account(self, jid: str, password: str, store_mode: str = "keyring", options: dict | None = None,
                          connect: bool = True) -> AccountItem:
        acc = await self.accounts.add(jid, password, store_mode, options)
        if connect:
            await self.connect_account(acc, password)
        return acc

    async def remove_account(self, acc: AccountItem) -> None:
        s = self.sessions.pop(acc.id, None)
        if s is not None:
            await s.stop()
        self.messaging.detach(acc.id)
        self.calls.detach(acc.id)
        await self.accounts.remove(acc)

    async def _account_removed(self, acc: AccountItem) -> None:
        await self.chats.remove_account(acc.id)
        self.roster.contacts.drop(acc.id)
        await self.requests.drop_account(acc.id)

    async def set_enabled(self, acc: AccountItem, enabled: bool) -> None:
        await self.accounts.set_enabled(acc, enabled)
        if enabled:
            await self.connect_account(acc)
        else:
            await self.disconnect_account(acc)

    async def set_presence(self, acc: AccountItem, show: str, status: str | None = None) -> None:
        acc.show = show
        if status is not None:
            acc.status = status
        await self.accounts.set_option(acc, "_show", show)
        await self.accounts.set_option(acc, "_status", acc.status)
        s = self.sessions.get(acc.id)
        if show == "offline":
            if s is not None:
                await s.stop()
            return
        if s is None or not s.wanted:
            await self.connect_account(acc)
            return
        if s.online:
            s.send_presence(show=show, status=acc.status)
            for (aid, jid), room in self.muc.rooms.items():
                if aid == acc.id and room.joined:
                    s.send_presence(show=show, status=acc.status, to="%s/%s" % (jid, room.nick))

    async def reconnect(self, acc: AccountItem) -> None:
        """After connection settings changed."""
        s = self.sessions.pop(acc.id, None)
        if s is not None:
            await s.stop()
            self.messaging.detach(acc.id)
        if acc.enabled:
            await self.connect_account(acc)

    async def provide_password(self, acc: AccountItem, password: str, remember: bool = True) -> None:
        if remember:
            try:
                await secrets.store(acc.jid, password, acc.option("store_password"))
            except secrets.KeyringUnavailable:
                secrets.remember_for_session(acc.jid, password)
        else:
            secrets.remember_for_session(acc.jid, password)
        s = self.session_for(acc)
        s.password = password
        await s.start(password)

    # ================================================================== window state
    def set_active_chat(self, chat: ChatItem | None) -> None:
        self.active_chat = chat
        if chat is not None and self.window_focused:
            aio.spawn(self.messaging.mark_read(chat))
            self.notifier.chat_read(chat)

    def set_window_state(self, focused: bool, visible: bool) -> None:
        was_visible = self.window_visible
        self.window_focused = focused
        self.window_visible = visible
        if was_visible != visible:
            for s in self.sessions.values():
                s.set_inactive(not visible)
        if focused and self.active_chat is not None:
            aio.spawn(self.messaging.mark_read(self.active_chat))
            self.notifier.chat_read(self.active_chat)

    def is_viewing(self, chat: ChatItem) -> bool:
        return self.active_chat is chat and self.window_visible

    # ================================================================== small helpers for services
    def my_nick(self, chat: ChatItem) -> str:
        return self.muc.my_nick(chat)

    def my_reaction_id(self, chat: ChatItem) -> str:
        acc = self.accounts.get(chat.account_id)
        return acc.jid if acc is not None else ""

    def describe_error(self, condition: str) -> str:
        return _(ERRORS[condition]) if condition in ERRORS else _("Failed (%s)") % condition

    def on_contact_presence(self, acc: AccountItem, contact: ContactItem) -> None:
        chat = self.chats.find(acc.id, contact.jid, "chat")
        if chat is not None:
            chat.update(show=contact.show, avatar=contact.avatar)
            if contact.show == "offline" and chat.typing:
                chat.typing = ""

    async def open_chat(self, acc: AccountItem, jid: str) -> ChatItem:
        jid = util.bare(jid)
        chat = await self.chats.get_or_create(acc.id, jid, "chat", name=self.names.contact_name(acc.id, jid))
        contact = self.roster.contacts.get(acc.id, jid)
        if contact is not None:
            chat.update(show=contact.show, avatar=contact.avatar)
        self.e2e.refresh(chat)
        return chat

    async def accept_request(self, acc: AccountItem, jid: str) -> None:
        s = self.sessions.get(acc.id)
        if s is not None and s.online:
            c = self.roster.contacts.get(acc.id, jid)
            if c is None or not c.in_roster:
                await self.roster.add_contact(s, jid)
            self.roster.accept(s, jid)
        chat = self.chats.find(acc.id, util.bare(jid), "chat")
        if chat is not None and chat.is_request:
            await self.chats.set_flags(chat, is_request=False)
        await self.requests.remove(acc.id, jid)

    async def decline_request(self, acc: AccountItem, jid: str, block: bool = False, spam: bool = False) -> None:
        s = self.sessions.get(acc.id)
        if s is not None and s.online:
            self.roster.decline(s, jid)
            if block or spam:
                with contextlib.suppress(Exception):
                    await self.roster.block(s, jid, report_spam=spam)
        chat = self.chats.find(acc.id, util.bare(jid), "chat")
        if chat is not None and chat.is_request:
            await self.chats.delete(chat)
        await self.requests.remove(acc.id, jid)

    async def _retention(self) -> None:
        """Delete local history older than the account's (or a chat's) retention, once a day."""
        while True:
            for acc in self.accounts.list():
                days = int(acc.option("retention_days") or 0)
                if days:
                    with contextlib.suppress(Exception):
                        await self.chats.apply_retention(None, days, acc.id)
            for chat in self.chats.items():
                days = int(chat.options.get("retention_days") or 0)
                if days:
                    with contextlib.suppress(Exception):
                        await self.chats.apply_retention(chat, days)
            with contextlib.suppress(Exception):
                await self.files.enforce_limits()
            await asyncio.sleep(24 * 3600)
