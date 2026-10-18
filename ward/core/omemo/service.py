"""OMEMO for Ward, written directly on python-omemo (no slixmpp-omemo).

OMEMO 2 (urn:xmpp:omemo:2) encrypts the whole message with Stanza Content Encryption; legacy OMEMO 0.3 (oldmemo,
optional, AGPL-3.0) encrypts only the body, for today's Conversations, Gajim and Dino.  Each recipient device gets
the best version it supports.

Trust: blind trust before verification (the default) trusts new devices automatically until one of the contact's
devices has been verified; after that, and in manual mode, new devices wait for a decision.  Devices waiting for a
decision are simply left out when encrypting (python-omemo would otherwise refuse to send at all): during
encryption only, an undecided device counts as distrusted.  That is done with a context variable, so it never
affects decryption running at the same time, which shows such messages with a "new device" warning."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import os
import time
from typing import Any
from xml.etree import ElementTree as ET

from gi.repository import GObject

from ... import aio, deps, util
from ...i18n import _
from .. import stanzas
from ..chat import EncryptionBlocked, Incoming, OnlyThisDevice
from ..models import DeviceItem
from ..pep import Pep
from . import sce, trust
from .storage import WardStorage

log = logging.getLogger("ward.omemo")

import omemo
import twomemo
import twomemo.etree
from omemo.session_manager import (
    MessageNotForUs,
    NoEligibleDevices,
    SenderDistrusted,
    SessionManager,
    StillUndecided,
    TrustDecisionFailed,
)
from omemo.types import TrustLevel

try:
    import oldmemo
    import oldmemo.etree
    HAVE_OLDMEMO = True
except Exception as _e:  # optional: OMEMO 2 only (missing, or broken by an incompatible dependency)
    logging.getLogger("ward.omemo").info("legacy OMEMO unavailable: %s", _e)
    oldmemo = None  # type: ignore[assignment]
    HAVE_OLDMEMO = False

NS2 = twomemo.twomemo.NAMESPACE
NS1 = oldmemo.oldmemo.NAMESPACE if HAVE_OLDMEMO else "eu.siacs.conversations.axolotl"
DEVICES2 = "urn:xmpp:omemo:2:devices"
BUNDLES2 = "urn:xmpp:omemo:2:bundles"
DEVICES1 = "eu.siacs.conversations.axolotl.devicelist"
FALLBACK = "This message is encrypted with OMEMO. Your app does not support it, or this device was not added."
OPEN = {"pubsub#access_model": "open", "pubsub#persist_items": "true"}

_encrypting: contextvars.ContextVar[bool] = contextvars.ContextVar("ward_omemo_encrypting", default=False)


def _manager_class(service: OmemoService, session):
    """A SessionManager whose XMPP hooks talk through this account's session."""

    class Manager(SessionManager):
        @staticmethod
        async def _upload_bundle(bundle) -> None:
            if bundle.namespace == NS2:
                item = twomemo.etree.serialize_bundle(bundle)
                await Pep.publish(session, BUNDLES2, item, str(bundle.device_id), {**OPEN, "pubsub#max_items": "max"})
            else:
                item = oldmemo.etree.serialize_bundle(bundle)
                await Pep.publish(session, "eu.siacs.conversations.axolotl.bundles:%d" % bundle.device_id, item,
                                  "current", {**OPEN, "pubsub#max_items": "1"})

        @staticmethod
        async def _download_bundle(namespace: str, bare_jid: str, device_id: int):
            try:
                if namespace == NS2:
                    items = await Pep.get_items(session, bare_jid, BUNDLES2, [str(device_id)])
                else:
                    items = await Pep.get_items(session, bare_jid, "eu.siacs.conversations.axolotl.bundles:%d"
                                                % device_id, max_items=1)
            except Exception as e:
                if getattr(e, "condition", "") == "item-not-found":
                    raise omemo.BundleNotFound(str(e)) from e
                raise omemo.BundleDownloadFailed(str(e)) from e
            if not items or len(items[0]) == 0:
                raise omemo.BundleNotFound("no bundle for %s/%d" % (bare_jid, device_id))
            el = items[0][0]
            try:
                if namespace == NS2:
                    return twomemo.etree.parse_bundle(el, bare_jid, device_id)
                return oldmemo.etree.parse_bundle(el, bare_jid, device_id)
            except Exception as e:
                raise omemo.BundleDownloadFailed("bad bundle: %s" % e) from e

        @staticmethod
        async def _delete_bundle(namespace: str, device_id: int) -> None:
            with contextlib.suppress(Exception):
                if namespace == NS2:
                    await Pep.retract(session, BUNDLES2, str(device_id), notify=False)
                else:
                    await Pep.delete_node(session, "eu.siacs.conversations.axolotl.bundles:%d" % device_id)

        @staticmethod
        async def _upload_device_list(namespace: str, device_list) -> None:
            if namespace == NS2:
                item, node = twomemo.etree.serialize_device_list(device_list), DEVICES2
            else:
                item, node = oldmemo.etree.serialize_device_list(device_list), DEVICES1
            try:
                await Pep.publish(session, node, item, "current", {**OPEN, "pubsub#max_items": "1"})
            except Exception as e:
                raise omemo.DeviceListUploadFailed(str(e)) from e

        @staticmethod
        async def _download_device_list(namespace: str, bare_jid: str):
            node = DEVICES2 if namespace == NS2 else DEVICES1
            try:
                items = await Pep.get_items(session, bare_jid, node, max_items=1)
            except Exception as e:
                if getattr(e, "condition", "") in ("item-not-found", "service-unavailable", "feature-not-implemented"):
                    return {}
                raise omemo.DeviceListDownloadFailed(str(e)) from e
            if not items or len(items[0]) == 0:
                return {}
            try:
                if namespace == NS2:
                    return twomemo.etree.parse_device_list(items[0][0])
                return oldmemo.etree.parse_device_list(items[0][0])
            except Exception as e:
                log.info("malformed device list of %s: %s", bare_jid, e)
                return {}

        async def _evaluate_custom_trust_level(self, device) -> TrustLevel:
            name = device.trust_level_name
            if name in trust.ENCRYPT_TO:
                return TrustLevel.TRUSTED
            if name == trust.DISTRUSTED:
                return TrustLevel.DISTRUSTED
            # UNDECIDED (or anything unknown): left out while encrypting, readable when decrypting
            return TrustLevel.DISTRUSTED if _encrypting.get() else TrustLevel.UNDECIDED

        async def _make_trust_decision(self, undecided, identifier) -> None:
            raise TrustDecisionFailed("undecided devices are decided before encrypting")

        @staticmethod
        async def _send_message(message, bare_jid: str) -> None:
            """Key exchange and session healing messages without content."""
            el = session.new_message(bare_jid, "chat", util.random_id(16))
            if message.namespace == NS2:
                el.append(twomemo.etree.serialize_message(message))
            else:
                el.append(oldmemo.etree.serialize_message(message))
            stanzas.add_hint(el, "store")
            session.send(el)

    return Manager


class OmemoService(GObject.Object):
    __gsignals__ = {
        # (chat or None, jid, [DeviceItem]) new devices the user should know about
        "new-devices": (GObject.SignalFlags.RUN_LAST, None, (object, str, object)),
        "devices-changed": (GObject.SignalFlags.RUN_LAST, None, (int, str)),   # account id, jid
        "ready": (GObject.SignalFlags.RUN_LAST, None, (int,)),
    }

    def __init__(self, core):
        super().__init__()
        if not deps.omemo_available():
            raise ImportError("python-omemo is not installed")
        self.core = core
        self.available = True
        self.managers: dict[int, SessionManager] = {}
        self._creating: dict[int, asyncio.Task] = {}
        self._refreshed: dict[int, set[tuple[str, str]]] = {}   # account -> {(jid, namespace)}
        self._namespaces: dict[int, list[str]] = {}
        self._devices: dict[tuple[int, str], frozenset] = {}
        self._reflections: dict[str, list[ET.Element]] = {}
        core.pep.register(DEVICES2, lambda s, f, i, r: self._on_device_list(s, f, i, NS2))
        if HAVE_OLDMEMO:
            core.pep.register(DEVICES1, lambda s, f, i, r: self._on_device_list(s, f, i, NS1))

    @property
    def legacy_enabled(self) -> bool:
        return HAVE_OLDMEMO and bool(self.core.settings["encryption.legacy_omemo"])

    def btbv(self, account_id: int) -> bool:
        return self.core.settings["encryption.trust"] == "btbv"

    # ================================================================== lifecycle
    def attach(self, session) -> None:
        session.add_feature(DEVICES2 + "+notify")
        if HAVE_OLDMEMO:
            session.add_feature(DEVICES1 + "+notify")
        session.connect("offline", lambda s: self._refreshed.pop(s.account.id, None))

    def ready(self, session) -> bool:
        return session.account.id in self.managers

    async def on_online(self, session, resumed: bool) -> None:
        if resumed and self.ready(session):
            return
        try:
            await self.manager(session)
        except Exception as e:
            log.warning("OMEMO could not start for %s: %s", session.jid, e)

    async def manager(self, session) -> SessionManager:
        aid = session.account.id
        m = self.managers.get(aid)
        if m is not None:
            return m
        task = self._creating.get(aid)
        if task is None:
            task = aio.spawn(self._create(session))
            self._creating[aid] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._creating.pop(aid, None)

    async def _create(self, session) -> SessionManager:
        acc = session.account
        storage = WardStorage(self.core.db, acc.id)
        # the internal option _omemo_backends (legacy | omemo2) lets the test-suite play an older client
        only = acc.options.get("_omemo_backends") or os.environ.get("WARD_OMEMO_BACKENDS", "")
        backends: list[Any] = [] if only == "legacy" else [twomemo.Twomemo(storage)]
        if HAVE_OLDMEMO and only != "omemo2":
            backends.append(oldmemo.Oldmemo(storage))
        cls = _manager_class(self, session)
        rotation = int(self.core.settings["advanced.spk_rotation_days"]) * 86400
        # Device labels are an OMEMO 2 feature; python-omemo cannot sign one while legacy OMEMO is loaded
        label_ok = not any(getattr(b, "namespace", "") == NS1 for b in backends)
        m = await cls.create(backends, storage, acc.jid, None, trust.UNDECIDED,
                             signed_pre_key_rotation_period=rotation,
                             pre_key_refill_threshold=int(self.core.settings["advanced.prekey_threshold"]))
        if label_ok:
            with contextlib.suppress(Exception):
                await m.set_own_label("Ward")
        with contextlib.suppress(Exception):
            await m.ensure_data_consistency()
        with contextlib.suppress(Exception):
            await m.after_history_sync()
        m._ward_storage = storage   # type: ignore[attr-defined]
        self._namespaces[acc.id] = [b.namespace for b in backends]
        self.managers[acc.id] = m
        acc.omemo_ready = True
        self.emit("ready", acc.id)
        for chat in self.core.chats.for_account(acc.id):
            self.core.e2e.refresh(chat)
        return m

    def history_sync(self, session, started: bool) -> None:
        m = self.managers.get(session.account.id)
        if m is None:
            return
        if started:
            with contextlib.suppress(Exception):
                m.before_history_sync()
        else:
            aio.spawn(m.after_history_sync())

    # ================================================================== devices and trust
    async def _on_device_list(self, session, frm: str, items, namespace: str) -> None:
        m = self.managers.get(session.account.id)
        if m is None or not items:
            return
        el = items[-1]
        child = el[0] if len(el) else None
        if child is None:
            return
        try:
            device_list = twomemo.etree.parse_device_list(child) if namespace == NS2 else \
                oldmemo.etree.parse_device_list(child)
        except Exception:
            return
        try:
            await m.update_device_list(namespace, frm, device_list)
        except Exception as e:
            log.info("device list update for %s failed: %s", frm, e)
            return
        self._refreshed.setdefault(session.account.id, set()).add((frm, namespace))
        await self.review(session, frm)

    async def refresh(self, session, jid: str, force: bool = False) -> None:
        """Make sure we have every device list of a JID, one per OMEMO version (PEP keeps contacts' lists current
        afterwards, but the two lists arrive separately)."""
        done = self._refreshed.setdefault(session.account.id, set())
        m = await self.manager(session)
        for ns in self._namespaces.get(session.account.id, [NS2]):
            if (jid, ns) in done and not force:
                continue
            try:
                await m.refresh_device_list(ns, jid)
            except Exception as e:
                log.debug("device list %s of %s: %s", ns, jid, e)
            done.add((jid, ns))

    async def review(self, session, jid: str) -> list:
        """Apply blind trust to new devices, remember first-seen dates, refresh chat banners."""
        m = await self.manager(session)
        acc = session.account
        try:
            devices = await m.get_device_information(jid)
        except Exception as e:
            log.info("device information of %s: %s", jid, e)
            return []
        verified = any(d.trust_level_name == trust.VERIFIED for d in devices)
        undecided = []
        now = int(time.time() * 1000)
        for d in devices:
            await self.core.db.execute("INSERT OR IGNORE INTO omemo_devices (account_id, jid, device_id, first_seen) "
                                       "VALUES (?, ?, ?, ?)", (acc.id, jid, d.device_id, now))
            if d.trust_level_name != trust.UNDECIDED:
                continue
            if self.btbv(acc.id) and not verified:
                await m.set_trust(jid, d.identity_key, trust.BLIND)
            elif not (jid == acc.jid and d.device_id == (await m.get_own_device_information())[0].device_id):
                undecided.append(d)
        devices = await m.get_device_information(jid)
        self._devices[(acc.id, jid)] = devices
        self.emit("devices-changed", acc.id, jid)
        chat = self.core.chats.find(acc.id, jid, "chat")
        if chat is not None:
            chat.new_devices = len(undecided)
            self.core.e2e.refresh(chat)
        if undecided:
            self.emit("new-devices", chat, jid, [self._item(acc, d, None) for d in undecided])
        return list(devices)

    def _item(self, acc, d, own_id: int | None) -> DeviceItem:
        versions = []
        if NS2 in d.namespaces:
            versions.append("OMEMO 2")
        if NS1 in d.namespaces:
            versions.append(_("legacy"))
        active = any(a for _ns, a in d.active)
        return DeviceItem(jid=d.bare_jid, device_id=d.device_id, fingerprint=trust.fingerprint(d.identity_key),
                          label=d.label or "", trust=trust.UI.get(d.trust_level_name, "undecided"),
                          active=active, inactive=not active, own=d.bare_jid == acc.jid,
                          this_device=own_id is not None and d.device_id == own_id, versions=", ".join(versions))

    async def devices(self, session, jid: str) -> list[DeviceItem]:
        """Every device of a contact (or our own account), for the encryption page."""
        await self.refresh(session, jid)
        m = await self.manager(session)
        acc = session.account
        own_id = (await m.get_own_device_information())[0].device_id
        await self.review(session, jid)
        devices = await m.get_device_information(jid)
        meta = {r["device_id"]: r for r in await self.core.db.fetchall(
            "SELECT * FROM omemo_devices WHERE account_id = ? AND jid = ?", (acc.id, jid))}
        out = []
        inactive_days = int(self.core.settings["encryption.inactive_days"] or 0)
        for d in sorted(devices, key=lambda d: d.device_id):
            it = self._item(acc, d, own_id)
            r = meta.get(d.device_id)
            if r is not None:
                it.first_seen = r["first_seen"] or 0
                it.last_used = r["last_message"] or 0
            if inactive_days and it.last_used and time.time() * 1000 - it.last_used > inactive_days * 86400000:
                it.inactive = True
            out.append(it)
        return out

    async def own_fingerprint(self, session) -> tuple[int, str]:
        m = await self.manager(session)
        own = (await m.get_own_device_information())[0]
        return own.device_id, trust.fingerprint(own.identity_key)

    async def set_trust(self, session, jid: str, device_id: int, level: str) -> None:
        """level: VERIFIED | TRUSTED | DISTRUSTED | UNDECIDED"""
        m = await self.manager(session)
        for d in await m.get_device_information(jid):
            if d.device_id == device_id:
                await m.set_trust(jid, d.identity_key, level)
                if level == trust.VERIFIED:
                    await self.core.db.execute("UPDATE omemo_devices SET verified_at = ? WHERE account_id = ? AND "
                                               "jid = ? AND device_id = ?", (int(time.time() * 1000),
                                                                             session.account.id, jid, device_id))
        await self.review(session, jid)

    async def verify_uri(self, session, uri) -> tuple[int, int]:
        """Apply an xmpp:jid?omemo-sid-N=FP link: (devices verified, fingerprints that did not match)."""
        m = await self.manager(session)
        jid = util.bare(uri.jid)
        await self.refresh(session, jid, force=True)
        ok = bad = 0
        devices = {d.device_id: d for d in await m.get_device_information(jid)}
        for dev_id, fp in uri.omemo.items():
            d = devices.get(dev_id)
            if d is not None and trust.fingerprint(d.identity_key) == fp:
                await m.set_trust(jid, d.identity_key, trust.VERIFIED)
                ok += 1
            else:
                bad += 1
        await self.review(session, jid)
        return ok, bad

    async def verification_uri(self, session) -> str:
        """Our own link/QR content with every trusted device of ours, like Conversations shows."""
        from ..uri import build_xmpp

        m = await self.manager(session)
        own, others = await m.get_own_device_information()
        fps = {own.device_id: trust.fingerprint(own.identity_key)}
        for d in others:
            if d.trust_level_name in (trust.VERIFIED,):
                fps[d.device_id] = trust.fingerprint(d.identity_key)
        return build_xmpp(session.jid, omemo=fps)

    async def set_label(self, session, label: str) -> None:
        m = await self.manager(session)
        try:
            await m.set_own_label(label or None)
        except NotImplementedError:
            raise ValueError(_("Device names need OMEMO 2 only (turn off legacy OMEMO)")) from None

    async def remove_own_device(self, session, device_id: int) -> None:
        """Take one of our other devices out of our published device lists."""
        m = await self.manager(session)
        own = (await m.get_own_device_information())[0]
        if device_id == own.device_id:
            raise ValueError("cannot remove this device")
        cls = type(m)
        for ns in [NS2] + ([NS1] if HAVE_OLDMEMO else []):
            lst = dict(await cls._download_device_list(ns, session.jid))
            if device_id in lst:
                del lst[device_id]
                await cls._upload_device_list(ns, lst)
                with contextlib.suppress(Exception):
                    await m.update_device_list(ns, session.jid, lst)
        await self.review(session, session.jid)

    async def remove_inactive_own_devices(self, session) -> int:
        n = 0
        for d in await self.devices(session, session.jid):
            if d.inactive and not d.this_device:
                await self.remove_own_device(session, int(d.device_id))
                n += 1
        return n

    async def reset_identity(self, session) -> None:
        """Start over with a new identity (contacts will see a new device)."""
        m = self.managers.pop(session.account.id, None)
        old_id = None
        if m is not None:
            old_id = (await m.get_own_device_information())[0].device_id
            for ns in [NS2] + ([NS1] if HAVE_OLDMEMO else []):
                with contextlib.suppress(Exception):
                    await m.purge_backend(ns)
            with contextlib.suppress(Exception):
                await m.shutdown()
        await WardStorage(self.core.db, session.account.id).wipe()
        self._refreshed.pop(session.account.id, None)
        m = await self.manager(session)
        if old_id is not None:
            with contextlib.suppress(Exception):
                await self.remove_own_device(session, old_id)

    # ------------------------------------------------------------------ backups
    async def export_trust(self, passphrase: str) -> bytes:
        entries = []
        for acc in self.core.accounts.list():
            m = self.managers.get(acc.id)
            if m is None:
                continue
            jids = {r["jid"] for r in await self.core.db.fetchall("SELECT DISTINCT jid FROM omemo_devices WHERE "
                                                                   "account_id = ?", (acc.id,))}
            for jid in jids:
                for d in await m.get_device_information(jid):
                    entries.append(trust.TrustEntry(jid, trust.fingerprint(d.identity_key), d.trust_level_name))
        return await aio.run_in_thread(trust.export_trust, entries, passphrase)

    async def import_trust(self, blob: bytes, passphrase: str) -> int:
        entries = await aio.run_in_thread(trust.import_trust, blob, passphrase)
        wanted = {(e.jid, e.fingerprint): e.level for e in entries}
        n = 0
        for acc in self.core.accounts.list():
            session = self.core.sessions.get(acc.id)
            m = self.managers.get(acc.id)
            if m is None or session is None:
                continue
            for jid in {e.jid for e in entries}:
                with contextlib.suppress(Exception):
                    await self.refresh(session, jid)
                    for d in await m.get_device_information(jid):
                        lvl = wanted.get((jid, trust.fingerprint(d.identity_key)))
                        if lvl and lvl != d.trust_level_name:
                            await m.set_trust(jid, d.identity_key, lvl)
                            n += 1
        return n

    async def export_device(self, session, passphrase: str) -> bytes:
        """Move this device to another computer: the identity and every session.  Use it on one machine only."""
        m = await self.manager(session)
        data = await m._ward_storage.dump()  # type: ignore[attr-defined]
        return await aio.run_in_thread(trust.seal, {"jid": session.jid, "store": data}, passphrase, "device")

    async def import_device(self, session, blob: bytes, passphrase: str) -> None:
        payload = await aio.run_in_thread(trust.unseal, blob, passphrase, "device")
        if util.bare(payload.get("jid", "")) != session.jid:
            raise trust.BackupError(_("This file belongs to another account"))
        m = self.managers.pop(session.account.id, None)
        if m is not None:
            with contextlib.suppress(Exception):
                await m.shutdown()
        await WardStorage(self.core.db, session.account.id).replace_all(payload["store"])
        await self.manager(session)

    # ================================================================== chat header state
    def describe(self, chat) -> tuple[str, str]:
        acc = self.core.accounts.get(chat.account_id)
        if acc is None or acc.id not in self.managers:
            return "omemo", ""     # optimistic until OMEMO is up; sending will tell
        if chat.type == "groupchat":
            return "omemo", ""
        jid = chat.jid if chat.type == "chat" else ""
        devices = self._devices.get((acc.id, jid))
        if devices is None:
            return "omemo", ""
        usable = [d for d in devices if d.trust_level_name in trust.ENCRYPT_TO and any(a for _n, a in d.active)]
        if not devices:
            return "blocked", _("%s has no encryption keys. They need an app with OMEMO, or you can turn "
                                "encryption off for this chat.") % (chat.name or chat.jid)
        if not usable:
            return "blocked", _("None of %s's devices is trusted yet. Verify or trust a device to write.") % (
                chat.name or chat.jid)
        legacy_only = [d for d in usable if NS2 not in d.namespaces]
        if legacy_only:
            if not self.legacy_enabled:
                if len(legacy_only) == len(usable):
                    return "blocked", _("%s only has devices with legacy OMEMO, which is turned off") % (
                        chat.name or chat.jid)
                return "omemo", _("Some devices use legacy OMEMO, which is turned off: they won't get your messages")
            return "omemo-legacy", _("Some devices use legacy OMEMO: only the text is encrypted, reactions and "
                                     "replies are not")
        return "omemo", ""

    # ================================================================== encrypting
    def _recipients(self, chat) -> set[str]:
        if chat.type == "groupchat":
            return self.core.muc.encryption_recipients(chat)
        if chat.type == "pm":
            return set()
        return {chat.jid}

    async def encrypt(self, session, chat, el: ET.Element) -> tuple[ET.Element, str]:
        m = await self.manager(session)
        acc = session.account
        recipients = self._recipients(chat)
        if not recipients and chat.type != "groupchat":
            raise EncryptionBlocked(_("Private messages in groups cannot be encrypted"))
        for jid in recipients | {acc.jid}:
            await self.refresh(session, jid)
            await self.review(session, jid)
        inside, outside = stanzas.split_content(el)
        body = el.findtext("{%s}body" % stanzas.NS_CLIENT)
        to_jid = chat.jid
        plaintexts: dict[str, bytes] = {NS2: sce.build([_copy(e) for e in inside], to_jid, acc.jid)}
        order = [NS2]
        if self.legacy_enabled and body is not None:
            plaintexts[NS1] = body.encode("utf-8")
            order.append(NS1)
        token = _encrypting.set(True)
        try:
            messages, errors = await m.encrypt(frozenset(recipients), plaintexts, backend_priority_order=order,
                                               identifier=str(chat.id))
        except NoEligibleDevices as e:
            if recipients == {acc.jid}:
                raise OnlyThisDevice() from None
            names = ", ".join(sorted(getattr(e, "bare_jids", recipients) or recipients))
            raise EncryptionBlocked(_("Not sent: %s has no trusted device that can receive encrypted messages. "
                                      "Check the contact's devices, or turn encryption off for this chat.")
                                    % names) from None
        except StillUndecided:
            raise EncryptionBlocked(_("Not sent: decide about the new devices first")) from None
        finally:
            _encrypting.reset(token)
        for err in errors:
            log.info("OMEMO encryption error (non-fatal): %s", err)
        out = ET.Element(el.tag, dict(el.attrib))
        namespaces = set()
        for msg in messages:
            if not msg.keys:
                continue        # python-omemo builds one message per backend, even when no device uses it
            if msg.namespace == NS2:
                out.append(twomemo.etree.serialize_message(msg))
            else:
                out.append(oldmemo.etree.serialize_message(msg))
            namespaces.add(msg.namespace)
        if not namespaces:
            if recipients == {acc.jid}:
                raise OnlyThisDevice()
            if chat.type == "groupchat" and not recipients:
                raise EncryptionBlocked(_("Not sent: nobody else in this group can read encrypted messages yet. "
                                          "Invite people first, or turn encryption off for this group."))
            raise EncryptionBlocked(_("Not sent: nothing could be encrypted for the recipients"))
        for o in outside:
            if o.tag != "{%s}body" % stanzas.NS_CLIENT:
                out.append(o)
        if NS1 in namespaces and not self.core.settings["encryption.limit_legacy_extras"]:
            # legacy OMEMO only carries the body: everything else travels next to it, in the clear
            for child in inside:
                if child.tag not in ("{%s}body" % stanzas.NS_CLIENT,):
                    out.append(_copy(child))
        elif NS1 in namespaces:
            for child in inside:
                if child.tag in ("{%s}replace" % stanzas.NS_CORRECT, "{%s}fallback" % stanzas.NS_FALLBACK,
                                 "{%s}markable" % stanzas.NS_MARKERS):
                    out.append(_copy(child))
        stanzas.add_eme(out, NS2 if NS2 in namespaces else NS1, "OMEMO")
        stanzas.set_body(out, FALLBACK)
        if out.find("{%s}store" % stanzas.NS_HINTS) is None:
            stanzas.add_hint(out, "store")
        if chat.type == "groupchat":
            self._reflections[el.get("id", "")] = [_copy(e) for e in inside]
            if len(self._reflections) > 500:
                self._reflections.pop(next(iter(self._reflections)))
        return out, "omemo2" if namespaces == {NS2} else "omemo"

    # ================================================================== decrypting
    async def decrypt(self, session, el: ET.Element, inc: Incoming, is_pm: bool = False) -> None:
        pm = inc.pm
        acc = session.account
        inner, _wrapper = stanzas.unwrap(el)
        inc.encryption = "omemo2" if pm.omemo2 is not None else "omemo"
        group = pm.type == "groupchat"
        if group:
            room = self.core.muc.room(acc.id, util.bare(pm.from_jid))
            nick = util.resource(pm.from_jid)
            occ = None
            if room is not None:
                occ = room.by_occupant_id.get(pm.occupant_id) if pm.occupant_id else None
                occ = occ or room.by_nick.get(nick)
            sender = occ.real_jid if occ is not None and occ.real_jid else ""
            if not sender and pm.muc_user is not None and pm.muc_user.jid:
                sender = util.bare(pm.muc_user.jid)
            expect_to = {util.bare(pm.from_jid)}
        else:
            sender = util.bare(pm.from_jid) if not inc.outgoing else acc.jid
            expect_to = {acc.jid, util.bare(pm.to_jid or acc.jid)} if not inc.outgoing else {util.bare(inc.peer)}
        try:
            m = await self.manager(session)
        except Exception:
            inc.decrypt_error = _("Encryption is not ready yet on this device")
            return
        if inc.outgoing and group and pm.id in self._reflections:
            self._apply_content(inc, inner, self._reflections.pop(pm.id), True)
            return
        if not sender:
            inc.decrypt_error = _("Can't decrypt: this group does not show who sent the message")
            return
        try:
            if pm.omemo2 is not None:
                message = twomemo.etree.parse_message(pm.omemo2, sender)
            elif pm.omemo is None:
                return
            else:
                if not HAVE_OLDMEMO:
                    inc.decrypt_error = _("This message uses legacy OMEMO, which is not installed here")
                    return
                message = await oldmemo.etree.parse_message(pm.omemo, sender, acc.jid, m)
            plaintext, device, _key = await m.decrypt(message)
        except MessageNotForUs:
            if group and inc.outgoing:
                inc.decrypt_error = _("Sent from this account before this device was added")
            else:
                inc.decrypt_error = _("This message was not encrypted for this device")
            return
        except SenderDistrusted:
            inc.decrypt_error = _("Sent from a device you distrusted; not shown")
            return
        except Exception as e:
            log.info("OMEMO decryption failed: %s", e)
            inc.decrypt_error = _("Could not decrypt this message (%s)") % type(e).__name__
            return
        inc.sender_device = device.device_id
        inc.trust = trust.UI.get(device.trust_level_name, "undecided")
        await self.core.db.execute("INSERT INTO omemo_devices (account_id, jid, device_id, first_seen, last_message) "
                                   "VALUES (?, ?, ?, ?, ?) ON CONFLICT(account_id, jid, device_id) DO UPDATE SET "
                                   "last_message = excluded.last_message",
                                   (acc.id, sender, device.device_id, inc.when, inc.when))
        if device.trust_level_name == trust.UNDECIDED:
            aio.spawn(self.review(session, sender))
        if plaintext is None:
            inc.pm = stanzas.ParsedMessage(type=pm.type, from_jid=pm.from_jid, to_jid=pm.to_jid, id=pm.id,
                                           wrapper=pm.wrapper)   # key exchange only: nothing to show
            return
        if message.namespace == NS2:
            try:
                content, _stamp = sce.parse(plaintext, sender, expect_to)
            except sce.EnvelopeError as e:
                inc.decrypt_error = _("Rejected an encrypted message: %s") % e
                return
            self._apply_content(inc, inner, content, True)
        else:
            body = ET.Element("{%s}body" % stanzas.NS_CLIENT)
            body.text = plaintext.decode("utf-8", "replace")
            self._apply_content(inc, inner, [body], False)

    @staticmethod
    def _apply_content(inc: Incoming, inner: ET.Element, content: list[ET.Element], replace_all: bool) -> None:
        """Rebuild the message with the decrypted content instead of the encrypted element and fallback body."""
        new = ET.Element(inner.tag, dict(inner.attrib))
        drop = {"{%s}encrypted" % stanzas.NS_OMEMO2, "{%s}encrypted" % stanzas.NS_OMEMO, "{%s}body" % stanzas.NS_CLIENT,
                "{%s}encryption" % stanzas.NS_EME}
        if replace_all:
            drop |= stanzas.SCE_CONTENT
        for child in inner:
            if child.tag not in drop:
                new.append(child)
        for c in content:
            new.append(c)
        pm = stanzas.parse(new)
        pm.wrapper = inc.pm.wrapper
        pm.stanza_ids = inc.pm.stanza_ids or pm.stanza_ids
        pm.delay = inc.pm.delay or pm.delay
        inc.pm = pm


def _copy(el: ET.Element) -> ET.Element:
    return ET.fromstring(ET.tostring(el))
