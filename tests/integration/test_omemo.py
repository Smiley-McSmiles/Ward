"""End to end OMEMO: OMEMO 2 with SCE, legacy OMEMO, trust (blind trust, verification, new devices), encrypted
files and encrypted private groups."""

import asyncio

import pytest
from helpers import account, befriend, fresh, make_core, session, stop, wait_for

from ward import deps

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not deps.omemo_available(), reason="OMEMO libraries not installed")]


def capture(core):
    """Collect outgoing stanzas (as the XML console sees them)."""
    out = []
    s = session(core)
    s.console = True
    s.connect("xml", lambda _s, incoming, xml: None if incoming else out.append(xml))
    return out


def test_omemo2_markdown_flag_on_the_outer_message():
    """Some clients (Ward-Android) mark Markdown outside the SCE envelope; decrypting must not lose it."""
    import types
    from xml.etree import ElementTree as ET

    from ward.core import stanzas
    from ward.core.omemo.service import OmemoService

    el = ET.fromstring("<message xmlns='jabber:client' type='chat'>"
                       "<markdown xmlns='urn:xmpp:ward:markdown:0'/>"
                       "<encrypted xmlns='urn:xmpp:omemo:2'><payload>AAAA</payload></encrypted></message>")
    inc = types.SimpleNamespace(pm=stanzas.parse(el), extra={})
    body = ET.Element("{jabber:client}body")
    body.text = "**bold** and `code`"
    OmemoService._apply_content(inc, el, [body], True)
    assert inc.pm.markdown is True and inc.pm.body == "**bold** and `code`"


def test_omemo2_trust_and_new_devices(trust_test_ca, tmp_path, run):
    prosody = trust_test_ca

    async def main():
        ALICE = fresh(prosody, "alice")
        BOB = fresh(prosody, "bob")
        alice = await make_core(tmp_path, prosody, ALICE, "o-alice")
        bob = await make_core(tmp_path, prosody, BOB, "o-bob")
        bob2 = None
        try:
            a, b = account(alice), account(bob)
            assert alice.settings["encryption.default"] == "always"
            await wait_for(lambda: a.omemo_ready and b.omemo_ready, 20, "OMEMO ready")
            await befriend(alice, bob)
            wire = capture(alice)

            chat = await alice.open_chat(a, b.jid)
            sent = await alice.messaging.send_text(chat, "the *secret* plan")
            assert sent.error == "" and sent.encryption == "omemo2", sent.error
            bchat = await wait_for(lambda: bob.chats.find(b.id, a.jid, "chat"), what="chat at bob")
            bml = await bob.chats.open_messages(bchat)
            await wait_for(lambda: bml.items(), what="message at bob")
            got = bml.items()[0]
            assert got.body == "the *secret* plan", got.extra
            assert got.encryption == "omemo2" and got.trust == "trusted" and got.is_markdown
            out = [x for x in wire if "secret" in x or "urn:xmpp:omemo:2" in x]
            assert out and all("secret" not in x for x in out), "plaintext leaked to the wire"
            assert any("urn:xmpp:omemo:2" in x and "urn:xmpp:eme:0" in x for x in out)

            # reactions and replies travel inside the encrypted envelope too
            await bob.messaging.react(bchat, got, "🔒")
            await wait_for(lambda: sent.reactions, what="encrypted reaction")
            reply = await bob.messaging.send_text(bchat, "agreed", reply_to=got)
            assert reply.encryption == "omemo2"
            ml = await alice.chats.open_messages(chat)
            await wait_for(lambda: len(ml) == 2, what="encrypted reply")
            assert ml.items()[1].reply_target == sent.id and ml.items()[1].body == "agreed"

            # verification by link/QR code
            uri_text = await bob.omemo.verification_uri(session(bob))
            from ward.core.uri import parse_xmpp
            ok, bad = await alice.omemo.verify_uri(session(alice), parse_xmpp(uri_text))
            assert (ok, bad) == (1, 0)
            devs = await alice.omemo.devices(session(alice), b.jid)
            assert [d.trust for d in devs] == ["verified"]

            # bob gets a new device: after verification it is not blindly trusted
            new_devices = []
            alice.omemo.connect("new-devices", lambda _o, _c, jid, items: new_devices.append((jid, items)))
            bob2 = await make_core(tmp_path, prosody, BOB, "o-bob2", resource="second")
            b2 = account(bob2)
            await wait_for(lambda: b2.omemo_ready, 20, "bob2 OMEMO")
            await wait_for(lambda: new_devices, 20, "new device notice at alice")
            assert chat.new_devices == 1
            second = await alice.messaging.send_text(chat, "only for verified devices")
            assert second.error == ""
            b2chat = await wait_for(lambda: bob2.chats.find(b2.id, a.jid, "chat"), 20, "chat at bob2")
            b2ml = await bob2.chats.open_messages(b2chat)
            await wait_for(lambda: any(m.extra.get("decrypt_error") for m in b2ml.items()), 20,
                           "bob2 cannot read it")
            # alice trusts the new device: the next message reaches it
            new_id = new_devices[0][1][0].device_id
            await alice.omemo.set_trust(session(alice), b.jid, new_id, "TRUSTED")
            assert chat.new_devices == 0
            await alice.messaging.send_text(chat, "welcome, second device")
            await wait_for(lambda: any(m.body == "welcome, second device" for m in b2ml.items()), 20,
                           "bob2 reads after trust")
        finally:
            await stop(alice, bob, *( [bob2] if bob2 else []))

    run(main(), timeout=180)


def test_legacy_omemo_and_encrypted_file(trust_test_ca, tmp_path, run):
    prosody = trust_test_ca

    async def main():
        CAROL = fresh(prosody, "carol")
        DAVE = fresh(prosody, "dave")
        carol = await make_core(tmp_path, prosody, CAROL, "l-carol")
        # dave plays an older app that only speaks legacy OMEMO (like Conversations today)
        dave = await make_core(tmp_path, prosody, DAVE, "l-dave", _omemo_backends="legacy")
        try:
            c, d = account(carol), account(dave)
            await wait_for(lambda: c.omemo_ready and d.omemo_ready, 20, "OMEMO ready")
            await befriend(carol, dave)
            chat = await carol.open_chat(c, d.jid)
            sent = await carol.messaging.send_text(chat, "legacy hello")
            assert sent.encryption == "omemo", sent.error
            await wait_for(lambda: chat.encryption_state == "omemo-legacy", what="legacy state in header")
            dchat = await wait_for(lambda: dave.chats.find(d.id, c.jid, "chat"), what="chat at dave")
            dml = await dave.chats.open_messages(dchat)
            await wait_for(lambda: dml.items() and dml.items()[0].body == "legacy hello", what="legacy decrypt")
            assert dml.items()[0].encryption == "omemo"
            # reactions: legacy OMEMO only encrypts a body, so they go out like Conversations sends them
            await carol.messaging.react(chat, sent, "👍")
            await wait_for(lambda: dml.items()[0].reactions, what="reaction at dave")
            assert sent.reactions and sent.reactions[0][0] == "👍"

            # an encrypted file: only an aesgcm:// link, inside the encrypted body
            src = tmp_path / "photo.bin"
            src.write_bytes(b"\x00\x01secret-bytes" * 5000)
            fmsg = await carol.files.send_file(chat, str(src))
            att = fmsg.attachments[0]
            await wait_for(lambda: att.state == "done" and fmsg.status >= 1, 30, "encrypted upload")
            assert att.url.startswith("aesgcm://") and fmsg.encryption == "omemo"
            await wait_for(lambda: dml.items()[-1].attachments, 20, "attachment at dave")
            m = dml.items()[-1]
            path = await dave.files.download(dchat, m, m.attachments[0])
            assert path
            with open(path, "rb") as fh:
                assert fh.read() == src.read_bytes()
        finally:
            await stop(carol, dave)

    run(main(), timeout=150)


def test_force_omemo_version_per_chat(trust_test_ca, tmp_path, run):
    """A chat can be pinned to OMEMO 2 or to legacy OMEMO, whatever the other devices support."""
    prosody = trust_test_ca

    async def main():
        EVE = fresh(prosody, "eve")
        FRANK = fresh(prosody, "frank")
        eve = await make_core(tmp_path, prosody, EVE, "f-eve")
        frank = await make_core(tmp_path, prosody, FRANK, "f-frank")
        try:
            e, f = account(eve), account(frank)
            await wait_for(lambda: e.omemo_ready and f.omemo_ready, 20, "OMEMO ready")
            await befriend(eve, frank)
            chat = await eve.open_chat(e, f.jid)

            await eve.chats.set_flags(chat, encryption="omemo2")
            eve.e2e.refresh(chat)
            assert chat.encryption_state == "omemo"
            a = await eve.messaging.send_text(chat, "omemo 2 only")
            assert a.encryption == "omemo2", a.error
            fchat = await wait_for(lambda: frank.chats.find(f.id, e.jid, "chat"), 20, "chat at frank")
            fml = await frank.chats.open_messages(fchat)
            await wait_for(lambda: any(m.body == "omemo 2 only" for m in fml.items()), 20, "omemo2 at frank")

            await eve.chats.set_flags(chat, encryption="omemo-legacy")
            eve.e2e.refresh(chat)
            assert chat.encryption_state == "omemo-legacy"
            b = await eve.messaging.send_text(chat, "legacy only")
            assert b.encryption == "omemo", b.error
            await wait_for(lambda: any(m.body == "legacy only" for m in fml.items()), 20, "legacy at frank")

            versions = {m.body: m.encryption for m in fml.items()}
            assert versions["omemo 2 only"] == "omemo2" and versions["legacy only"] == "omemo"

            # forgetting the contact drops the local device list, trust and sessions
            n = await eve.omemo.forget_contact(session(eve), f.jid)
            assert n > 0
            remaining = await eve.db.fetchall("SELECT key FROM omemo_store WHERE account_id = ? AND key LIKE ?",
                                              (e.id, "%%/%s/%%" % f.jid))
            assert remaining == [], [r["key"] for r in remaining]
            assert await eve.db.fetchall("SELECT * FROM omemo_devices WHERE account_id = ? AND jid = ?",
                                         (e.id, f.jid)) == []
        finally:
            await stop(eve, frank)

    run(main(), timeout=120)


def test_forget_legacy_and_force_omemo2(trust_test_ca, tmp_path, run):
    """Removing legacy OMEMO drops its keys and bundle; forcing OMEMO 2 then reports a legacy-only peer."""
    prosody = trust_test_ca

    async def main():
        GINA = fresh(prosody, "gina")
        HANK = fresh(prosody, "hank")
        gina = await make_core(tmp_path, prosody, GINA, "g-gina")
        hank = await make_core(tmp_path, prosody, HANK, "g-hank", _omemo_backends="legacy")
        try:
            g, h = account(gina), account(hank)
            await wait_for(lambda: g.omemo_ready and h.omemo_ready, 20, "OMEMO ready")
            await befriend(gina, hank)
            chat = await gina.open_chat(g, h.jid)
            await gina.chats.set_flags(chat, encryption="omemo-legacy")
            gina.e2e.refresh(chat)
            sent = await gina.messaging.send_text(chat, "hello older app")
            assert sent.encryption == "omemo" and sent.error == "", sent.error

            # forcing OMEMO 2 with a legacy-only peer is refused with a clear reason, even when the account
            # policy would normally send plain rather than fail
            await gina.accounts.set_option(g, "encryption", "available")
            await gina.chats.set_flags(chat, encryption="omemo2")
            gina.e2e.refresh(chat)
            await wait_for(lambda: chat.encryption_state == "blocked", what="blocked OMEMO 2 state")
            refused = await gina.messaging.send_text(chat, "not for you")
            assert "OMEMO 2" in (refused.error or ""), refused.error

            # removing legacy OMEMO clears the stored legacy keys and turns the setting off
            n = await gina.omemo.forget_legacy(session(gina))
            assert n > 0
            assert gina.settings["encryption.legacy_omemo"] is False
            assert gina.omemo.legacy_enabled is False
            removed = await gina.db.fetchall("SELECT key FROM omemo_store WHERE account_id = ? AND key LIKE "
                                             "'/eu.siacs.conversations.axolotl%'", (g.id,))
            assert removed == [], [r["key"] for r in removed]
        finally:
            await stop(gina, hank)

    run(main(), timeout=120)


def test_encrypted_private_group(trust_test_ca, tmp_path, run):
    prosody = trust_test_ca

    async def main():
        ALICE = fresh(prosody, "alice")
        BOB = fresh(prosody, "bob")
        alice = await make_core(tmp_path, prosody, ALICE, "e-alice")
        bob = await make_core(tmp_path, prosody, BOB, "e-bob")
        try:
            a, b = account(alice), account(bob)
            await wait_for(lambda: a.omemo_ready and b.omemo_ready, 20, "OMEMO ready")
            await befriend(alice, bob)
            chat = await alice.muc.create_group(a, "Secret club", "private", invitees=[b.jid])
            bchat = await bob.muc.join(b, chat.jid)
            await wait_for(lambda: b.jid in alice.muc.encryption_recipients(chat), what="bob is a recipient")
            assert chat.encryption_state in ("omemo", "omemo-legacy")
            ml = await alice.chats.open_messages(chat)
            bml = await bob.chats.open_messages(bchat)
            sent = await alice.messaging.send_text(chat, "encrypted to the group")
            assert sent.encryption in ("omemo2", "omemo"), sent.error
            await wait_for(lambda: any(m.body == "encrypted to the group" for m in bml.items()), 20,
                           "group decrypt at bob")
            await wait_for(lambda: sent.status >= 2, what="own reflection recognised")
            assert not any(m.extra.get("decrypt_error") for m in ml.items())
            back = await bob.messaging.send_text(bchat, "and back")
            assert back.encryption in ("omemo2", "omemo"), back.error
            await wait_for(lambda: any(m.body == "and back" for m in ml.items()), 20, "group decrypt at alice")
        finally:
            await stop(alice, bob)

    run(main(), timeout=150)


def test_encrypted_note_to_self(trust_test_ca, tmp_path, run):
    """With one device an encrypted note stays on it; with a second device the note reaches it, decrypted."""
    prosody = trust_test_ca

    async def main():
        user = fresh(prosody, "notes")
        one = await make_core(tmp_path, prosody, user, "n-one", resource="one")
        two = None
        try:
            a1 = account(one)
            await wait_for(lambda: a1.omemo_ready, 20, "OMEMO ready")
            chat = await one.open_chat(a1, a1.jid)
            alone = await one.messaging.send_text(chat, "only here")
            await wait_for(lambda: alone.status >= 1 or alone.error, what="kept")
            assert alone.extra.get("local_only") and not alone.error

            two = await make_core(tmp_path, prosody, user, "n-two", resource="two")
            a2 = account(two)
            await wait_for(lambda: a2.omemo_ready, 20, "second device OMEMO")
            note = await one.messaging.send_text(chat, "on both")
            assert not note.error and not note.extra.get("local_only"), note.error
            chat2 = await wait_for(lambda: two.chats.find(a2.id, a2.jid, "chat"), what="note at the second device")
            ml2 = await two.chats.open_messages(chat2)
            await wait_for(lambda: any(m.body == "on both" for m in ml2.items()), 20, "decrypted note")
            assert all(m.outgoing for m in ml2.items())
        finally:
            await stop(*(c for c in (one, two) if c is not None))

    run(main(), timeout=90)


def test_own_messages_reach_other_devices(trust_test_ca, tmp_path, run):
    """A carbon copy of our own message must be encrypted for our other devices too."""
    prosody = trust_test_ca

    async def main():
        ALICE = fresh(prosody, "alice-own")
        BOB = fresh(prosody, "bob-own")
        laptop = await make_core(tmp_path, prosody, ALICE, "alice-laptop", resource="laptop")
        bob = await make_core(tmp_path, prosody, BOB, "bob-own")
        phone = None
        try:
            a, b = account(laptop), account(bob)
            await wait_for(lambda: a.omemo_ready and b.omemo_ready, 20, "OMEMO ready")
            await befriend(laptop, bob)
            phone = await make_core(tmp_path, prosody, ALICE, "alice-phone", resource="phone")
            p = account(phone)
            await wait_for(lambda: p.omemo_ready, 20, "phone OMEMO")
            # Trust the two own devices in both directions: the default blind-trust policy asks about new devices.
            async def other_device(owner):
                for _ in range(100):
                    devs = await owner.omemo.devices(session(owner), account(owner).jid)
                    found = next((d for d in devs if not d.this_device), None)
                    if found is not None:
                        return found
                    await asyncio.sleep(0.2)
                raise AssertionError("the other own device did not appear")

            for owner in (laptop, phone):
                other_dev = await other_device(owner)
                if other_dev.trust != "trusted":
                    await owner.omemo.set_trust(session(owner), account(owner).jid, other_dev.device_id, "TRUSTED")

            chat = await laptop.open_chat(a, b.jid)
            await laptop.messaging.send_text(chat, "hello from the laptop")
            pchat = await wait_for(lambda: phone.chats.find(p.id, b.jid, "chat"), 20, "phone chat")
            pml = await phone.chats.open_messages(pchat)
            await wait_for(lambda: any(m.body == "hello from the laptop" for m in pml.items()), 20,
                           "carbon at phone")
            got = next(m for m in pml.items() if m.body == "hello from the laptop")
            assert got.outgoing
            assert not got.extra.get("decrypt_error"), got.extra
        finally:
            await stop(laptop, bob, *([phone] if phone else []))

    run(main(), timeout=120)
