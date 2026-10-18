"""End to end OMEMO: OMEMO 2 with SCE, legacy OMEMO, trust (blind trust, verification, new devices), encrypted
files and encrypted private groups."""

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
