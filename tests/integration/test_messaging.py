"""End to end: two people on a real server - contacts, presence, and every 1:1 messaging feature."""

import pytest

from helpers import account, befriend, fresh, make_core, session, stop, wait_for
from ward.core.models import STATUS_DELIVERED, STATUS_READ

pytestmark = pytest.mark.integration


def test_contacts_and_messaging(trust_test_ca, tmp_path, run):
    prosody = trust_test_ca

    async def main():
        ALICE = fresh(prosody, "alice")
        BOB = fresh(prosody, "bob")
        alice = await make_core(tmp_path, prosody, ALICE)
        bob = await make_core(tmp_path, prosody, BOB)
        try:
            a, b = account(alice), account(bob)
            for core in (alice, bob):
                core.settings.set("encryption.default", "off")   # this test is about plain messaging
            await befriend(alice, bob)
            c = alice.roster.contacts.get(a.id, b.jid)
            await wait_for(lambda: c.show == "available", what="bob's presence at alice")

            # --- send, receive, receipts
            chat = await alice.open_chat(a, b.jid)
            ml = await alice.chats.open_messages(chat)
            sent = await alice.messaging.send_text(chat, "Hello **Bob**")
            assert sent.body == "Hello **Bob**" and len(ml) == 1
            bchat = await wait_for(lambda: bob.chats.find(b.id, a.jid, "chat"), what="chat at bob")
            bml = await bob.chats.open_messages(bchat)
            await wait_for(lambda: len(bml) == 1, what="message at bob")
            got = bml.items()[0]
            assert got.body == "Hello **Bob**" and got.is_markdown and not got.outgoing
            assert bchat.unread == 1 and not bchat.is_request
            await wait_for(lambda: sent.status >= STATUS_DELIVERED, what="delivery receipt")

            # --- read marker
            bob.window_focused = True
            bob.set_active_chat(bchat)
            await wait_for(lambda: bchat.unread == 0, what="bob reading")
            await wait_for(lambda: sent.status == STATUS_READ, what="read marker at alice")

            # --- typing
            alice.messaging.set_chat_state(chat, "composing")
            await wait_for(lambda: bchat.typing, what="typing at bob")
            alice.messaging.set_chat_state(chat, "paused")
            await wait_for(lambda: not bchat.typing, what="typing stopped")

            # --- reactions both ways
            await bob.messaging.react(bchat, got, "👍")
            await wait_for(lambda: sent.reactions and sent.reactions[0][0] == "👍", what="reaction at alice")
            assert not sent.reactions[0][2]          # not ours
            await alice.messaging.react(chat, sent, "🎉")
            await wait_for(lambda: len(got.reactions) == 2, what="second reaction at bob")
            await bob.messaging.react(bchat, got, "👍")    # toggled off
            await wait_for(lambda: [e for e, _w, _m in sent.reactions] == ["🎉"], what="reaction removed")

            # --- correction
            await alice.messaging.correct(chat, sent, "Hello *Bob*!")
            await wait_for(lambda: got.edited and got.body == "Hello *Bob*!", what="correction at bob")
            assert sent.edited

            # --- reply
            reply = await bob.messaging.send_text(bchat, "Hi Alice", reply_to=got)
            assert reply.reply_preview
            await wait_for(lambda: len(ml) == 2, what="reply at alice")
            r = ml.items()[1]
            assert r.body == "Hi Alice" and r.reply_target == sent.id and r.reply_author

            # --- delete for everyone
            await alice.messaging.retract(chat, sent)
            await wait_for(lambda: got.retracted == 1 and got.body == "", what="retraction at bob")

            # --- search (the retracted message is gone from the index)
            hits = await bob.chats.core.db.fetchall("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'hello'")
            assert hits == []
            from ward.core import search
            assert [h["body"] for h in await search.search(bob.db, "alice")] == ["Hi Alice"]
        finally:
            await stop(alice, bob)

    run(main(), timeout=90)


def test_offline_messages_carbons_and_archive(trust_test_ca, tmp_path, run):
    prosody = trust_test_ca

    async def main():
        CAROL = fresh(prosody, "carol")
        DAVE = fresh(prosody, "dave")
        # carol and dave are fresh users; carol has two devices
        carol = await make_core(tmp_path, prosody, CAROL, "carol-laptop", resource="laptop")
        dave = await make_core(tmp_path, prosody, DAVE)
        try:
            for core in (carol, dave):
                core.settings.set("encryption.default", "off")
            await befriend(carol, dave)
            c, d = account(carol), account(dave)
            phone = await make_core(tmp_path, prosody, CAROL, "carol-phone", resource="phone")
            phone.settings.set("encryption.default", "off")
            pc = account(phone)

            # carbons: dave writes, both of carol's devices get it; carol answers from the laptop and the phone
            # sees her own answer
            dchat = await dave.open_chat(d, c.jid)
            await dave.messaging.send_text(dchat, "ping")
            await wait_for(lambda: carol.chats.find(c.id, d.jid, "chat"), what="laptop chat")
            pchat = await wait_for(lambda: phone.chats.find(pc.id, d.jid, "chat"), what="phone chat")
            lchat = carol.chats.find(c.id, d.jid, "chat")
            await carol.messaging.send_text(lchat, "pong")
            pml = await phone.chats.open_messages(pchat)
            await wait_for(lambda: [m.body for m in pml.items()] == ["ping", "pong"], what="carbons at phone")
            assert pml.items()[1].outgoing

            # offline + archive catch-up, without duplicates
            await dave.disconnect_account(d)
            await carol.messaging.send_text(lchat, "one")
            await carol.messaging.send_text(lchat, "two")
            await dave.connect_account(d, "password")
            await session(dave).wait_online(20)
            dml = await dave.chats.open_messages(dchat)
            await wait_for(lambda: [m.body for m in dml.items()][-2:] == ["one", "two"], 20, "catch-up at dave")
            rows = await dave.db.fetchall("SELECT body FROM messages ORDER BY timestamp, id")
            assert [r["body"] for r in rows] == ["ping", "pong", "one", "two"]

            # a fresh device loads history from the archive
            newdev = await make_core(tmp_path, prosody, DAVE, "dave-new", resource="new")
            rows = await newdev.db.fetchall("SELECT body, direction FROM messages ORDER BY timestamp, id")
            assert [r["body"] for r in rows] == ["ping", "pong", "one", "two"]
            assert [r["direction"] for r in rows] == [1, 0, 0, 0]
            await stop(newdev)
        finally:
            await stop(carol, dave, phone)

    run(main(), timeout=120)
