"""Memory in the real window: switching between chats while messages come and go must not keep old message rows
alive (their handlers refer back to them, a cycle through GTK the garbage collector cannot see)."""

import asyncio
import gc

import pytest
from helpers import befriend, fresh, make_core, stop, wait_for
from test_gui import app  # noqa: F401 - the fixture

pytestmark = [pytest.mark.integration, pytest.mark.gui]


def test_switching_chats_frees_rows(app, prosody, run, tmp_path):  # noqa: F811
    from ward.gui.message_row import MessageRow

    async def scenario():
        await wait_for(lambda: app.core.started, 20, "core start")
        user = fresh(prosody, "mem")
        opts = {"host": "127.0.0.1", "port": prosody.c2s, "tls": "starttls", "proxy": "none"}
        acc = await app.core.add_account("%s@localhost" % user, "password", "never", opts, connect=False)
        await app.core.connect_account(acc, "password")
        await wait_for(lambda: acc.state == "online" and acc.step == "", 30, "online")
        app.core.settings.set("encryption.default", "off")
        w = app.window
        w.set_default_size(900, 600)
        peers, chats, pchats = [], [], []
        try:
            for i in range(2):
                p = await make_core(tmp_path, prosody, fresh(prosody, "mp%d" % i))
                p.settings.set("encryption.default", "off")
                await befriend(app.core, p)
                peers.append(p)
                chats.append(await app.core.open_chat(acc, p.accounts.list()[0].jid))
                pchats.append(await p.open_chat(p.accounts.list()[0], acc.jid))
            for rnd in range(8):
                for i, chat in enumerate(chats):
                    w.show_chat(chat)
                    await asyncio.sleep(0.05)
                    await peers[i].messaging.send_text(pchats[i], "round %d\n- with\n- a list" % rnd)
                    w.conversation.composer.buffer.set_text("reply %d" % rnd)
                    w.conversation.composer.send()
                    await asyncio.sleep(0.1)
            await asyncio.sleep(0.5)
            gc.collect()
            rows = [o for o in gc.get_objects() if isinstance(o, MessageRow)]
            orphans = [r for r in rows if r.get_parent() is None]
            assert not orphans, "%d message rows outlived their list view" % len(orphans)
            # (rows of other tests' windows can still be around: the app only ever hides its window)
            mine = [r for r in rows if r.get_root() is w]
            assert len(mine) <= len(w.conversation.rows) + 5
        finally:
            await stop(*peers)
        await app.core.shutdown()

    run(scenario(), timeout=300)


def test_closed_dialogs_are_freed(app, prosody, run, monkeypatch):  # noqa: F811
    from ward.gui import lifetime, prefs, trust

    monkeypatch.setattr(lifetime, "FORGET_AFTER", 1)

    async def scenario():
        await wait_for(lambda: app.core.started, 20, "core start")
        user = fresh(prosody, "dlg")
        opts = {"host": "127.0.0.1", "port": prosody.c2s, "tls": "starttls", "proxy": "none"}
        acc = await app.core.add_account("%s@localhost" % user, "password", "never", opts, connect=False)
        await app.core.connect_account(acc, "password")
        await wait_for(lambda: acc.state == "online" and acc.step == "", 30, "online")
        w = app.window
        for _ in range(3):
            p = prefs.Preferences(w, "")
            p.present(w)
            await asyncio.sleep(0.2)
            p.d.close()
            d = trust.DevicesDialog(w, acc, acc.jid)
            d.present(w)
            await asyncio.sleep(0.2)
            d.close()
        del p, d
        await asyncio.sleep(2.5)
        gc.collect()
        for cls in (prefs.Preferences, trust.DevicesDialog):
            alive = sum(1 for o in gc.get_objects() if isinstance(o, cls))
            assert alive == 0, "%d closed %s still in memory" % (alive, cls.__name__)
        await app.core.shutdown()

    run(scenario(), timeout=120)
