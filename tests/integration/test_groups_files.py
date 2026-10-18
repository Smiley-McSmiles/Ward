"""End to end: group chats (presets, invites, messages, reactions, moderation, bookmarks) and files."""

import os

import pytest

from helpers import account, befriend, fresh, make_core, session, stop, wait_for

pytestmark = pytest.mark.integration


def test_private_group(trust_test_ca, tmp_path, run):
    prosody = trust_test_ca

    async def main():
        ALICE = fresh(prosody, "alice")
        BOB = fresh(prosody, "bob")
        alice = await make_core(tmp_path, prosody, ALICE, "g-alice")
        bob = await make_core(tmp_path, prosody, BOB, "g-bob")
        try:
            for core in (alice, bob):
                core.settings.set("encryption.default", "off")
            a, b = account(alice), account(bob)
            invites = []
            bob.muc.connect("invite", lambda _m, acc, inv: invites.append(inv))

            chat = await alice.muc.create_group(a, "Family", "private", invitees=[b.jid])
            assert chat.joined and chat.name == "Family"
            assert chat.encryptable, "a private group is members-only and non-anonymous"
            assert chat.our_affiliation == "owner"
            await wait_for(lambda: invites, what="invite at bob")
            assert invites[0]["room"] == chat.jid and invites[0]["from"] == a.jid

            bchat = await bob.muc.join(b, chat.jid)
            assert bchat.joined
            bob_nick, alice_nick = b.jid.split("@")[0], a.jid.split("@")[0]
            await wait_for(lambda: alice.muc.room_of(chat).by_nick.get(bob_nick), what="bob in alice's member list")
            occ = alice.muc.room_of(chat).by_nick[bob_nick]
            assert occ.real_jid == b.jid and occ.affiliation == "member"

            ml = await alice.chats.open_messages(chat)
            bml = await bob.chats.open_messages(bchat)
            sent = await alice.messaging.send_text(chat, "Dinner at 8?")
            await wait_for(lambda: [m.body for m in bml.items() if m.kind != "status"] == ["Dinner at 8?"],
                           what="group message at bob")
            await wait_for(lambda: sent.status >= 2 and sent.stanza_id, what="reflection marks it delivered")
            got = [m for m in bml.items() if m.kind != "status"][0]
            assert got.sender == alice_nick and got.stanza_id == sent.stanza_id

            # reactions use the room's stanza id
            await bob.messaging.react(bchat, got, "❤️")
            await wait_for(lambda: sent.reactions and sent.reactions[0][0] == "❤️", what="group reaction")

            # bob answers and corrects himself
            reply = await bob.messaging.send_text(bchat, "Yes!")
            await wait_for(lambda: any(m.body == "Yes!" for m in ml.items()), what="bob's message at alice")
            await bob.messaging.correct(bchat, reply, "Yes, 8 is fine")
            await wait_for(lambda: any(m.body == "Yes, 8 is fine" and m.edited for m in ml.items()),
                           what="correction in group")

            # alice (owner) moderates bob's message - when the server supports XEP-0425 (Prosody 0.12 does not)
            assert chat.our_role == "moderator"
            if alice.muc.can_moderate(chat):
                target = next(m for m in ml.items() if m.body == "Yes, 8 is fine")
                await wait_for(lambda: target.stanza_id, what="stanza id")
                await alice.muc.moderate(chat, target, "test")
                await wait_for(lambda: any(m.retracted == 3 for m in bml.items()), what="moderation at bob")
            # bob deletes his own message for everyone instead
            await bob.messaging.retract(bchat, reply)
            await wait_for(lambda: any(m.retracted == 1 for m in ml.items()), what="retraction in group")

            # alice's new device rejoins from the bookmark (XEP-0402)
            other = await make_core(tmp_path, prosody, ALICE, "g-alice2", resource="tablet")
            other.settings.set("encryption.default", "off")
            oacc = account(other)
            ochat = await wait_for(lambda: other.chats.find(oacc.id, chat.jid, "groupchat"), what="bookmark")
            await wait_for(lambda: ochat.joined, what="autojoin")
            oml = await other.chats.open_messages(ochat)
            await wait_for(lambda: any(m.body == "Dinner at 8?" for m in oml.items()), 20, "room archive catch-up")
            await stop(other)

            await bob.muc.leave(bchat)
            await wait_for(lambda: bob_nick not in alice.muc.room_of(chat).by_nick, what="bob left")
        finally:
            await stop(alice, bob)

    run(main(), timeout=120)


def test_public_channel_is_not_encryptable(trust_test_ca, tmp_path, run):
    prosody = trust_test_ca

    async def main():
        CAROL = fresh(prosody, "carol")
        carol = await make_core(tmp_path, prosody, CAROL, "p-carol")
        try:
            chat = await carol.muc.create_group(account(carol), "Open talk", "public", listed=True)
            assert chat.joined and not chat.encryptable
            carol.e2e.refresh(chat)
            assert chat.encryption_state == "plain"
            rooms = await carol.muc.list_rooms(session(carol), "conference.localhost")
            assert chat.jid in [j for j, _n in rooms]
        finally:
            await stop(carol)

    run(main(), timeout=60)


def test_file_transfer(trust_test_ca, tmp_path, run):
    prosody = trust_test_ca
    src = tmp_path / "notes.txt"
    src.write_text("hello file " * 1000)

    async def main():
        ALICE = fresh(prosody, "alice")
        BOB = fresh(prosody, "bob")
        alice = await make_core(tmp_path, prosody, ALICE, "f-alice")
        bob = await make_core(tmp_path, prosody, BOB, "f-bob")
        try:
            for core in (alice, bob):
                core.settings.set("encryption.default", "off")
            a, b = account(alice), account(bob)
            assert a.can_upload and a.upload_limit == 16 * 1024 * 1024
            await befriend(alice, bob)
            chat = await alice.open_chat(a, b.jid)
            msg = await alice.files.send_file(chat, str(src))
            att = msg.attachments[0]
            await wait_for(lambda: att.state == "done" and msg.status >= 1, 20, "upload")
            assert att.url.startswith("https://localhost:")
            bchat = await wait_for(lambda: bob.chats.find(b.id, a.jid, "chat"), what="chat at bob")
            bml = await bob.chats.open_messages(bchat)
            await wait_for(lambda: bml.items() and bml.items()[-1].attachments, what="attachment at bob")
            m = bml.items()[-1]
            ba = m.attachments[0]
            assert ba.name == "notes.txt" and ba.size == src.stat().st_size and m.body == ""
            path = await bob.files.download(bchat, m, ba)
            assert path and open(path).read() == src.read_text()
            assert ba.state == "done"
        finally:
            await stop(alice, bob)

    run(main(), timeout=90)


def test_aesgcm_roundtrip(tmp_path):
    from ward.core import files

    src = tmp_path / "plain.bin"
    src.write_bytes(os.urandom(700_000))
    enc, dec = tmp_path / "enc.bin", tmp_path / "dec.bin"
    key, iv = files.encrypt_file(str(src), str(enc))
    assert len(key) == 32 and len(iv) == 12 and enc.stat().st_size == src.stat().st_size + 16
    files.decrypt_file(str(enc), str(dec), key, iv)
    assert dec.read_bytes() == src.read_bytes()
    data = bytearray(enc.read_bytes())
    data[100] ^= 1
    enc.write_bytes(bytes(data))
    from cryptography.exceptions import InvalidTag

    with pytest.raises(InvalidTag):
        files.decrypt_file(str(enc), str(tmp_path / "bad.bin"), key, iv)
    assert not (tmp_path / "bad.bin").exists()
