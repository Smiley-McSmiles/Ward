"""Files received over the wire are fetched automatically within the account's limit."""

from helpers import account, befriend, fresh, make_core, stop, wait_for


def test_files_are_auto_downloaded_whether_the_chat_is_open(trust_test_ca, tmp_path, run):
    prosody = trust_test_ca

    async def main():
        ALICE = fresh(prosody, "alice")
        BOB = fresh(prosody, "bob")
        alice = await make_core(tmp_path, prosody, ALICE, "ad-alice")
        bob = await make_core(tmp_path, prosody, BOB, "ad-bob")
        try:
            for core in (alice, bob):
                core.settings.set("encryption.default", "off")
            a, b = account(alice), account(bob)
            assert b.option("autodownload_mb") == 1
            await befriend(alice, bob)
            chat = await alice.open_chat(a, b.jid)

            async def send(name: str) -> str:
                src = tmp_path / name
                src.write_text("hello file " * 1000)
                msg = await alice.files.send_file(chat, str(src))
                att = msg.attachments[0]
                await wait_for(lambda: att.state == "done" and msg.status >= 1, 20, "upload")
                return src.read_text()

            # The chat is not open when it arrives: the file is still fetched in the background.
            expected = await send("closed-before.txt")
            bchat = await wait_for(lambda: bob.chats.find(b.id, a.jid, "chat"), what="chat at bob")
            bml = await bob.chats.open_messages(bchat)
            m = await wait_for(lambda: bml.items() and bml.items()[-1].attachments, what="attachment at bob")
            m = bml.items()[-1]
            ba = m.attachments[0]
            await wait_for(lambda: ba.state == "done" and ba.local_path, 20, "auto download with the chat closed")
            with open(ba.local_path) as fh:
                assert fh.read() == expected

            # And with the chat already open.
            expected = await send("already-open.txt")
            m = await wait_for(lambda: len(bml.items()) >= 2 and bml.items()[-1].attachments, what="second file")
            m = bml.items()[-1]
            ba = m.attachments[0]
            await wait_for(lambda: ba.state == "done" and ba.local_path, 20, "auto download with the chat open")
            with open(ba.local_path) as fh:
                assert fh.read() == expected
        finally:
            await stop(alice, bob)

    run(main(), timeout=90)
