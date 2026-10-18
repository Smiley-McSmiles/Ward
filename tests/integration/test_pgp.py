"""End to end OpenPGP (XEP-0373/0374): keys published and found over PEP, signed and encrypted messages."""

import pytest
from helpers import account, befriend, fresh, make_core, stop, wait_for

from ward.core.pgp import gnupg

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(gnupg.find_gpg(use_gpgman=False) is None, reason="gpg not installed")]


def test_openpgp_chat(trust_test_ca, tmp_path, run):
    prosody = trust_test_ca

    async def setup(core, name):
        home = tmp_path / ("gnupg-" + name)
        home.mkdir(mode=0o700)
        core.settings.set("pgp.enabled", True)
        core.settings.set("pgp.use_gpgman", False)
        core.settings.set("pgp.gnupg_home", str(home))
        from ward import aio

        acc = account(core)
        fp = await aio.run_in_thread(core.pgp.engine().generate, "xmpp:" + acc.jid, "never", "")
        await core.pgp.choose_key(acc, fp)
        return fp

    async def main():
        A, B = fresh(prosody, "alice"), fresh(prosody, "bob")
        alice = await make_core(tmp_path, prosody, A, "pgp-alice")
        bob = await make_core(tmp_path, prosody, B, "pgp-bob")
        try:
            a, b = account(alice), account(bob)
            fa = await setup(alice, "a")
            fb = await setup(bob, "b")
            await befriend(alice, bob)
            chat = await alice.open_chat(a, b.jid)
            chat.encryption = "pgp"
            alice.e2e.refresh(chat)
            assert chat.encryption_state == "pgp", chat.encryption_reason
            sent = await alice.messaging.send_text(chat, "signed and sealed")
            assert sent.encryption == "pgp" and sent.error == "", sent.error
            # bob learned alice's key over PEP and stored it as announced
            keys = await alice.pgp.keys(a.id, b.jid)
            assert [k.fingerprint for k in keys] == [fb] and keys[0].trust == "trusted"
            bchat = await wait_for(lambda: bob.chats.find(b.id, a.jid, "chat"), 20, "chat at bob")
            bml = await bob.chats.open_messages(bchat)
            await wait_for(lambda: bml.items(), 20, "pgp message at bob")
            got = bml.items()[0]
            assert got.body == "signed and sealed", got.extra
            assert got.encryption == "pgp" and got.trust == "trusted"
            # bob verifies alice's key; from then on her other keys would wait for a decision
            await bob.pgp.set_trust(b.id, a.jid, fa, "verified")
            assert [k.trust for k in await bob.pgp.keys(b.id, a.jid)] == ["verified"]
            # and answers over OpenPGP too
            bchat.encryption = "pgp"
            back = await bob.messaging.send_text(bchat, "received")
            assert back.encryption == "pgp", back.error
            ml = await alice.chats.open_messages(chat)
            await wait_for(lambda: any(m.body == "received" for m in ml.items()), 20, "answer at alice")
        finally:
            for core in (alice, bob):
                core.pgp.engine().kill_agent()
            await stop(alice, bob)

    run(main(), timeout=120)
