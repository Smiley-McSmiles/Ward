"""Memory in the real window: switching between chats while messages come and go must not keep old message rows
alive (their handlers refer back to them, a cycle through GTK the garbage collector cannot see)."""

import asyncio
import gc
import weakref

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
        opened = []
        for _ in range(3):
            p = prefs.Preferences(w, "")
            p.present(w)
            await asyncio.sleep(0.2)
            p.d.close()
            d = trust.DevicesDialog(w, acc, acc.jid)
            d.present(w)
            await asyncio.sleep(0.2)
            d.close()
            opened += [weakref.ref(p), weakref.ref(d)]
        del p, d

        def alive() -> list[str]:
            gc.collect()
            return [type(r()).__name__ for r in opened if r() is not None]
        try:
            await wait_for(lambda: not alive(), 10, "closed dialogs to be freed")
        except AssertionError:
            held = [r() for r in opened if r() is not None]
            refs = ["%s <- %s" % (type(o).__name__, ", ".join(sorted({type(x).__name__ for x in gc.get_referrers(o)})))
                    for o in held]
            raise AssertionError("closed dialogs still in memory: %s" % refs) from None
        await app.core.shutdown()

    run(scenario(), timeout=120)


def test_background_window_is_freed(app, prosody, run, monkeypatch):  # noqa: F811
    """With Ward in the background, a closed window is let go (and with it what GTK and the renderer hold for it);
    opening Ward again builds a new one."""
    from ward.gui import lifetime

    monkeypatch.setattr(lifetime, "FORGET_AFTER", 1)

    async def scenario():
        await wait_for(lambda: app.core.started, 20, "core start")
        app.core.settings.set("general.background", True)
        user = fresh(prosody, "bg")
        opts = {"host": "127.0.0.1", "port": prosody.c2s, "tls": "starttls", "proxy": "none"}
        acc = await app.core.add_account("%s@localhost" % user, "password", "never", opts, connect=False)
        await app.core.connect_account(acc, "password")
        await wait_for(lambda: acc.state == "online" and acc.step == "", 30, "online")
        for _ in range(2):
            finalized: list[str] = []
            w = app.window
            w.weak_ref(lambda *_a, out=finalized: out.append("window"))
            w.conversation.weak_ref(lambda *_a, out=finalized: out.append("conversation"))
            w.close()
            del w
            await wait_for(lambda: app.window is None, 10, "the window let go")
            await asyncio.sleep(0.5)
            gc.collect()
            await asyncio.sleep(0.2)
            assert sorted(finalized) == ["conversation", "window"], finalized
            assert app.core.sessions[acc.id].online         # still connected in the background
            app.activate()
            assert app.window is not None and app.window.get_visible()
            await asyncio.sleep(0.5)
        await app.core.shutdown()

    run(scenario(), timeout=120)


def test_message_rows_are_light(app, prosody, run, tmp_path):  # noqa: F811
    """A message row must not carry an emoji picker until its reaction button is used: built eagerly, GTK's
    choosers made a chat of 60 messages take 600 MB.  Ward's own picker draws one category at a time."""
    import ctypes

    from ward.gui import emoji_picker

    class Mallinfo2(ctypes.Structure):
        _fields_ = [(n, ctypes.c_size_t) for n in ("arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks",
                                                     "fsmblks", "uordblks", "fordblks", "keepcost")]
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.mallinfo2.restype = Mallinfo2
    except (OSError, AttributeError):
        pytest.skip("needs glibc")

    def heap_mb() -> int:
        gc.collect()
        return libc.mallinfo2().uordblks >> 20

    async def scenario():
        await wait_for(lambda: app.core.started, 20, "core start")
        user = fresh(prosody, "rows")
        opts = {"host": "127.0.0.1", "port": prosody.c2s, "tls": "starttls", "proxy": "none"}
        acc = await app.core.add_account("%s@localhost" % user, "password", "never", opts, connect=False)
        await app.core.connect_account(acc, "password")
        await wait_for(lambda: acc.state == "online" and acc.step == "", 30, "online")
        app.core.settings.set("encryption.default", "off")
        peer = await make_core(tmp_path, prosody, fresh(prosody, "rowp"))
        try:
            peer.settings.set("encryption.default", "off")
            await befriend(app.core, peer)
            pchat = await peer.open_chat(peer.accounts.list()[0], acc.jid)
            for k in range(40):
                await peer.messaging.send_text(pchat, "message %d" % k)
            chat = await app.core.open_chat(acc, peer.accounts.list()[0].jid)
            await wait_for(lambda: "39" in chat.preview, 20, "messages")
            w = app.window
            w.set_default_size(900, 900)
            before = heap_mb()
            w.show_chat(chat)
            await wait_for(lambda: len(w.conversation.rows) >= 30, 20, "rows")
            await asyncio.sleep(1)
            grown = heap_mb() - before
            assert grown < 100, "showing %d messages took %d MB" % (len(w.conversation.rows), grown)

            row = next(r for r in w.conversation.rows.values() if getattr(r, "react_button", None) is not None)
            reacted = []
            row.view.react = lambda m, e: reacted.append(e)
            assert row.react_button.get_popover() is None
            row.react_button.popup()
            chooser = row.react_button.get_popover()
            assert isinstance(chooser, emoji_picker.EmojiPicker)
            await asyncio.sleep(0.3)
            # one category at a time, searchable, and the pick goes to the message
            assert 0 < len(chooser.grid.items) < 500
            chooser.search.set_text("party pop")
            await wait_for(lambda: [e for e, _n in chooser.grid.items] == ["🎉"], 5, "search")
            chooser.search.emit("activate")
            assert reacted == ["🎉"]
            # skin tones: the chosen tone for every emoji that takes one, every tone of one emoji on right click
            row.react_button.popup()
            chooser = row.react_button.get_popover()
            chooser._set_tone(4)
            assert app.core.settings["chats.emoji_skin_tone"] == 4
            chooser.search.set_text("waving hand")
            await wait_for(lambda: [e for e, _n in chooser.grid.items] == ["👋🏾"], 5, "toned search")
            assert emoji_picker.takes_tone("👋🏾") and emoji_picker.takes_tone("🎉") is None
            chooser.grid._variants(0)
            chooser.search.emit("activate")
            assert reacted[-1] == "👋🏾"
            app.core.settings.set("chats.emoji_skin_tone", 0)
            assert app.core.settings["chats.recent_emoji"][:2] == ["👋🏾", "🎉"]
            await wait_for(lambda: row.react_button.get_popover() is None, 5, "picker freed")
        finally:
            await stop(peer)
        await app.core.shutdown()

    run(scenario(), timeout=300)

