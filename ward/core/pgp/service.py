"""OpenPGP for XMPP (XEP-0373) and its instant messaging profile (XEP-0374), optional.

Each account can pick one of the user's GnuPG secret keys (from the keyring shared with GPGMan, or imported from
a GPGMan export).  Its public key is published over PEP, contacts' keys are fetched the same way, and a chat
switched to OpenPGP sends <signcrypt/> elements: the whole content, signed and encrypted to the contact's keys and
our own (so our other devices can read it), with the recipient and time checked on arrival.

Trust works like OMEMO: with blind trust before verification, announced keys are used until one of the contact's
keys is verified; verified, trusted and distrusted decisions are kept per key in the database (pgp_keys), not in
GnuPG's web of trust."""

from __future__ import annotations

import base64
import contextlib
import logging
import secrets
import time
from xml.etree import ElementTree as ET

from gi.repository import GObject

from ... import aio, util
from ...i18n import _
from .. import stanzas
from ..chat import EncryptionBlocked, Incoming
from ..models import DeviceItem
from ..pep import Pep
from . import gnupg

log = logging.getLogger("ward.pgp")

NS = "urn:xmpp:openpgp:0"
NODE_META = NS + ":public-keys"
FALLBACK = "This message is encrypted with OpenPGP (XEP-0374)."
OPEN = {"pubsub#access_model": "open", "pubsub#persist_items": "true"}
MAX_SKEW = 7 * 24 * 3600


def build_signcrypt(content: list[ET.Element], to_jids: list[str]) -> bytes:
    sc = ET.Element("{%s}signcrypt" % NS)
    for j in to_jids:
        ET.SubElement(sc, "{%s}to" % NS, {"jid": j})
    ET.SubElement(sc, "{%s}time" % NS, {"stamp": stanzas.format_stamp(time.time())})
    ET.SubElement(sc, "{%s}rpad" % NS).text = secrets.token_urlsafe(150)[:secrets.randbelow(200)]
    payload = ET.SubElement(sc, "{%s}payload" % NS)
    for c in content:
        payload.append(c)
    return ET.tostring(sc, encoding="utf-8")


def parse_signcrypt(data: bytes, expect_to: set[str]) -> list[ET.Element]:
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError("refusing XML with a document type")
    el = ET.fromstring(data)
    if el.tag != "{%s}signcrypt" % NS:
        raise ValueError("not a signcrypt element")
    tos = {t.get("jid", "").split("/")[0].lower() for t in el.findall("{%s}to" % NS)}
    if not tos & {t.lower() for t in expect_to}:
        raise ValueError("the message was meant for someone else")
    t = el.find("{%s}time" % NS)
    stamp = stanzas.parse_stamp(t.get("stamp")) if t is not None else None
    if stamp is not None and stamp - time.time() > MAX_SKEW:
        raise ValueError("the message is dated in the future")
    payload = el.find("{%s}payload" % NS)
    return list(payload) if payload is not None else []


class PgpService(GObject.Object):
    __gsignals__ = {
        "keys-changed": (GObject.SignalFlags.RUN_LAST, None, (int, str)),   # account id, jid
    }

    def __init__(self, core):
        super().__init__()
        self.core = core
        self._published: set[int] = set()
        core.pep.register(NODE_META, self._on_metadata)
        core.settings.connect("changed", lambda *_: self._engine_cache.clear())
        self._engine_cache: dict[str, gnupg.GnuPG] = {}

    # ================================================================== engine and setup
    @property
    def enabled(self) -> bool:
        return bool(self.core.settings["pgp.enabled"])

    def engine(self) -> gnupg.GnuPG:
        s = self.core.settings
        key = "%s|%s|%s" % (s["pgp.gpg_binary"], s["pgp.gnupg_home"], s["pgp.use_gpgman"])
        g = self._engine_cache.get(key)
        if g is None:
            g = gnupg.GnuPG(s["pgp.gpg_binary"] or None, s["pgp.gnupg_home"] or None, bool(s["pgp.use_gpgman"]))
            self._engine_cache[key] = g
        return g

    @property
    def available(self) -> bool:
        return self.engine().available

    def own_key(self, account_id: int) -> str:
        acc = self.core.accounts.get(account_id)
        return (acc.option("pgp_key") or "").upper() if acc is not None else ""

    def ready(self, account_id: int) -> bool:
        return self.enabled and self.available and bool(self.own_key(account_id))

    async def secret_keys(self) -> list[gnupg.Key]:
        """Keys the user can sign with: from the shared (GPGMan) keyring."""
        return [k for k in await aio.run_in_thread(self.engine().list_keys, True) if k.usable]

    async def choose_key(self, acc, fingerprint: str) -> None:
        await self.core.accounts.set_option(acc, "pgp_key", fingerprint.upper())
        self._published.discard(acc.id)
        session = self.core.sessions.get(acc.id)
        if session is not None and session.online:
            await self.publish(session)

    async def generate_key(self, acc) -> str:
        fp = await aio.run_in_thread(self.engine().generate, "xmpp:" + acc.jid)
        await self.choose_key(acc, fp)
        return fp

    async def import_file(self, path: str) -> list[str]:
        """Import keys from a file, e.g. an .asc that GPGMan exported (public or secret keys)."""
        return await aio.run_in_thread(self.engine().import_file, path)

    async def import_text(self, text: str) -> list[str]:
        return await aio.run_in_thread(self.engine().import_keys, text.encode("utf-8"))

    # ================================================================== lifecycle
    def attach(self, session) -> None:
        session.add_feature(NODE_META + "+notify")

    async def on_online(self, session, resumed: bool) -> None:
        if resumed or not self.ready(session.account.id):
            return
        with contextlib.suppress(Exception):
            await self.publish(session)

    async def publish(self, session) -> None:
        """Publish our public key and add it to our announced key list (keeping our other devices' keys)."""
        fp = self.own_key(session.account.id)
        if not fp or session.account.id in self._published:
            return
        data = await aio.run_in_thread(self.engine().export_public, fp)
        stamp = stanzas.format_stamp(time.time()).split(".")[0] + "Z"
        pub = ET.Element("{%s}pubkey" % NS, {"date": stamp})
        ET.SubElement(pub, "{%s}data" % NS).text = base64.b64encode(data).decode()
        await Pep.publish(session, "%s:%s" % (NODE_META, fp), pub, stamp, {**OPEN, "pubsub#max_items": "1"})
        existing: dict[str, str] = {}
        with contextlib.suppress(Exception):
            items = await Pep.get_items(session, None, NODE_META, max_items=1)
            for it in items:
                for m in it.iter("{%s}pubkey-metadata" % NS):
                    existing[m.get("v4-fingerprint", "").upper()] = m.get("date", "")
        existing[fp] = stamp
        lst = ET.Element("{%s}public-keys-list" % NS)
        for f, d in existing.items():
            if f:
                ET.SubElement(lst, "{%s}pubkey-metadata" % NS, {"v4-fingerprint": f, "date": d})
        await Pep.publish(session, NODE_META, lst, "current", {**OPEN, "pubsub#max_items": "1"})
        self._published.add(session.account.id)

    # ================================================================== contacts' keys
    async def _on_metadata(self, session, frm: str, items, retracted) -> None:
        for it in items:
            fps = [m.get("v4-fingerprint", "").upper() for m in it.iter("{%s}pubkey-metadata" % NS)]
            await self._announced(session, frm, [f for f in fps if f])

    async def _announced(self, session, jid: str, fps: list[str]) -> None:
        acc = session.account
        now = int(time.time() * 1000)

        def job(conn):
            conn.execute("UPDATE pgp_keys SET announced = 0 WHERE account_id = ? AND jid = ?", (acc.id, jid))
            for f in fps:
                conn.execute("INSERT INTO pgp_keys (account_id, jid, fingerprint, first_seen, last_seen) VALUES "
                             "(?, ?, ?, ?, ?) ON CONFLICT(account_id, jid, fingerprint) DO UPDATE SET announced = 1, "
                             "last_seen = excluded.last_seen", (acc.id, jid, f, now, now))
        await self.core.db.run(job)
        known = {k.fingerprint for k in await aio.run_in_thread(self.engine().list_keys)}
        for f in fps:
            if f not in known:
                await self._fetch_key(session, jid, f)
        self.emit("keys-changed", acc.id, jid)
        chat = self.core.chats.find(acc.id, jid, "chat")
        if chat is not None:
            self.core.e2e.refresh(chat)

    async def _fetch_key(self, session, jid: str, fp: str) -> None:
        try:
            items = await Pep.get_items(session, jid, "%s:%s" % (NODE_META, fp), max_items=1)
        except Exception as e:
            log.info("could not fetch OpenPGP key %s of %s: %s", fp, jid, e)
            return
        for it in items:
            data = it.findtext("{%s}pubkey/{%s}data" % (NS, NS))
            if not data:
                continue
            try:
                got = await aio.run_in_thread(self.engine().import_keys, base64.b64decode(data))
            except Exception as e:
                log.info("bad OpenPGP key from %s: %s", jid, e)
                continue
            if fp not in got:
                log.warning("%s published key data that does not match fingerprint %s", jid, fp)

    async def refresh(self, session, jid: str) -> None:
        try:
            items = await Pep.get_items(session, jid, NODE_META, max_items=1)
        except Exception:
            return
        for it in items:
            fps = [m.get("v4-fingerprint", "").upper() for m in it.iter("{%s}pubkey-metadata" % NS)]
            await self._announced(session, jid, [f for f in fps if f])

    async def keys(self, account_id: int, jid: str) -> list[DeviceItem]:
        rows = await self.core.db.fetchall(
            "SELECT * FROM pgp_keys WHERE account_id = ? AND jid = ? ORDER BY first_seen", (account_id, util.bare(jid)))
        keyring = {k.fingerprint: k for k in await aio.run_in_thread(self.engine().list_keys)} if rows else {}
        out = []
        for r in rows:
            k = keyring.get(r["fingerprint"])
            out.append(DeviceItem(jid=r["jid"], fingerprint=r["fingerprint"], label=k.name if k else "",
                                  trust=self._effective(rows, r), active=bool(r["announced"]) and k is not None,
                                  inactive=not r["announced"], first_seen=r["first_seen"], last_used=r["last_seen"],
                                  kind="pgp"))
        return out

    def _effective(self, rows, r) -> str:
        t = r["trust"]
        if t != "undecided":
            return t
        btbv = self.core.settings["encryption.trust"] == "btbv"
        any_verified = any(x["trust"] == "verified" for x in rows)
        return "trusted" if btbv and not any_verified else "undecided"

    async def set_trust(self, account_id: int, jid: str, fingerprint: str, trust: str) -> None:
        """trust: verified | trusted | distrusted | undecided"""
        await self.core.db.execute("UPDATE pgp_keys SET trust = ? WHERE account_id = ? AND jid = ? AND fingerprint = ?",
                                   (trust, account_id, util.bare(jid), fingerprint.upper()))
        self.emit("keys-changed", account_id, util.bare(jid))

    async def add_contact_key(self, account_id: int, jid: str, data: bytes | str, verified: bool = True) -> list[str]:
        """A key the user got another way (a file from GPGMan, pasted text): imported and marked verified."""
        raw = data.encode("utf-8") if isinstance(data, str) else data
        fps = await aio.run_in_thread(self.engine().import_keys, raw)
        now = int(time.time() * 1000)
        for f in fps:
            await self.core.db.execute(
                "INSERT INTO pgp_keys (account_id, jid, fingerprint, trust, announced, first_seen, last_seen) VALUES "
                "(?, ?, ?, ?, 0, ?, ?) ON CONFLICT(account_id, jid, fingerprint) DO UPDATE SET trust = excluded.trust",
                (account_id, util.bare(jid), f, "verified" if verified else "undecided", now, now))
        self.emit("keys-changed", account_id, util.bare(jid))
        return fps

    async def usable_keys(self, account_id: int, jid: str) -> list[str]:
        return [k.fingerprint for k in await self.keys(account_id, jid)
                if k.trust in ("verified", "trusted") and k.active]

    # ================================================================== header state
    def describe(self, chat) -> tuple[str, str]:
        if not self.enabled:
            return "blocked", _("OpenPGP is turned off in Preferences > Encryption")
        if not self.available:
            return "blocked", _("OpenPGP needs GnuPG (gpg), which is not installed")
        if not self.own_key(chat.account_id):
            return "blocked", _("Choose your OpenPGP key for this account first")
        return "pgp", ""

    # ================================================================== encrypting
    async def encrypt(self, session, chat, el: ET.Element) -> ET.Element:
        if chat.type != "chat":
            raise EncryptionBlocked(_("OpenPGP is only available in one-to-one chats"))
        state, reason = self.describe(chat)
        if state != "pgp":
            raise EncryptionBlocked(reason)
        acc = session.account
        await self.refresh(session, chat.jid)
        theirs = await self.usable_keys(acc.id, chat.jid)
        if not theirs:
            raise EncryptionBlocked(_("Not sent: %s has no trusted OpenPGP key") % (chat.name or chat.jid))
        mine = self.own_key(acc.id)
        inside, outside = stanzas.split_content(el)
        plain = build_signcrypt([_copy(e) for e in inside], [chat.jid])
        try:
            data = await aio.run_in_thread(self.engine().encrypt_sign, plain, [*theirs, mine], mine)
        except gnupg.GpgError as e:
            raise EncryptionBlocked(_("OpenPGP encryption failed: %s") % e) from None
        out = ET.Element(el.tag, dict(el.attrib))
        ET.SubElement(out, "{%s}openpgp" % NS).text = base64.b64encode(data).decode()
        for o in outside:
            out.append(o)
        stanzas.add_eme(out, NS, "OpenPGP for XMPP")
        stanzas.set_body(out, FALLBACK)
        if out.find("{%s}store" % stanzas.NS_HINTS) is None:
            stanzas.add_hint(out, "store")
        return out

    # ================================================================== decrypting
    async def decrypt(self, session, el: ET.Element, inc: Incoming) -> None:
        pm = inc.pm
        acc = session.account
        inc.encryption = "pgp"
        inner, _w = stanzas.unwrap(el)
        sender = acc.jid if inc.outgoing else util.bare(pm.from_jid)
        expect_to = {util.bare(inc.peer)} if inc.outgoing else {acc.jid}
        try:
            data = base64.b64decode((pm.openpgp.text or "").strip())
            res = await aio.run_in_thread(self.engine().decrypt_verify, data)
        except (ValueError, gnupg.GpgError) as e:
            inc.decrypt_error = _("Could not decrypt this OpenPGP message (%s)") % e
            return
        try:
            content = parse_signcrypt(res.plaintext, expect_to)
        except (ValueError, ET.ParseError) as e:
            inc.decrypt_error = _("Rejected an OpenPGP message: %s") % e
            return
        if not res.signature_ok:
            inc.trust = "distrusted" if not res.key_missing else "undecided"
        elif sender == acc.jid and res.signer == self.own_key(acc.id):
            inc.trust = "verified"
        else:
            if res.key_missing:
                await self.refresh(session, sender)
            rows = await self.core.db.fetchall("SELECT * FROM pgp_keys WHERE account_id = ? AND jid = ?",
                                               (acc.id, sender))
            match = [r for r in rows if r["fingerprint"] == res.signer]
            inc.trust = self._effective(rows, match[0]) if match else "undecided"
            if not match:
                inc.extra["pgp_unknown_signer"] = res.signer
        new = ET.Element(inner.tag, dict(inner.attrib))
        for child in inner:
            if child.tag not in stanzas.SCE_CONTENT and child.tag not in ("{%s}openpgp" % NS,
                                                                          "{%s}encryption" % stanzas.NS_EME):
                new.append(child)
        for c in content:
            new.append(c)
        parsed = stanzas.parse(new)
        parsed.wrapper = pm.wrapper
        parsed.stanza_ids = pm.stanza_ids or parsed.stanza_ids
        parsed.delay = pm.delay or parsed.delay
        inc.pm = parsed


def _copy(el: ET.Element) -> ET.Element:
    return ET.fromstring(ET.tostring(el))
