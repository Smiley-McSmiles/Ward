"""The message list follows the bottom when it should, and only the user can leave it.

A rebuilt row (a receipt, a reaction, an edit) is cleared and refilled: the list measures it again and moves the view
while it does, which used to look like the user scrolling up.  The newest message then sat just below the viewport,
and sending again was the only way back.  Someone typing rebuilt the chat's banner and menu for the same effect.
Needs a display (CI runs it under Xvfb).
"""

import asyncio
import time

import pytest
from helpers import wait_for
from test_gui import app  # noqa: F401 - the fixture

pytestmark = [pytest.mark.integration, pytest.mark.gui]


def _values(chat_id: int, i: int, when: int, outgoing: bool = False, body: str = "") -> dict:
    sender = "scroll@localhost" if outgoing else "sam@localhost"
    return {"chat_id": chat_id, "origin_id": "s%d" % i, "msg_id": "s%d" % i, "direction": 1 if outgoing else 0,
            "timestamp": when, "sender": sender, "sender_jid": sender,
            "body": body or "message %d, with a sentence of text so the row has a real height" % i,
            "is_markdown": 0, "kind": "text", "status": 0, "extra": {}}


def test_the_list_stays_at_the_bottom(app, run):  # noqa: F811
    from ward.core.chats import insert_message_job

    async def scenario():
        await wait_for(lambda: app.core.started, 20, "core start")
        acc = await app.core.add_account("scroll@localhost", "x", "never", connect=False)
        chat = await app.core.open_chat(acc, "sam@localhost")
        w = app.window
        w.set_default_size(900, 420)
        base = int(time.time() * 1000) - 60 * 60_000

        def fill(conn):
            for i in range(40):
                insert_message_job(conn, _values(chat.id, i, base + i * 60_000))
        await app.core.db.run(fill)
        w.show_chat(chat)
        view = w.conversation
        await wait_for(lambda: len(view.rows) > 0 and view._at_bottom(), 10, "the list at the bottom")

        async def send(i: int, text: str):
            values = _values(chat.id, 100 + i, int(time.time() * 1000), outgoing=True, body=text)
            mid = await app.core.db.run(lambda conn: insert_message_job(conn, values))
            msg = await app.core.chats.reload_message(chat, mid)
            app.core.chats.show_message(chat, msg)
            return msg

        sent = await send(1, "the first message")
        await asyncio.sleep(0.35)
        assert view._stick and view._at_bottom(), "sending scrolls to your own message"

        # a receipt or read marker: the row is rebuilt with its status icon
        await app.core.chats.update_message_fields(sent.id, status=3)
        await app.core.chats.reload_message(chat, sent.id)
        await asyncio.sleep(0.35)
        assert view._stick and view._at_bottom(), "a receipt must not scroll the newest message out of view"

        # a reaction rebuilds the row as well
        await app.core.db.execute("INSERT INTO reactions (message_id, sender, emojis, timestamp) VALUES (?, ?, ?, ?)",
                                  (sent.id, "sam@localhost", '["👍"]', int(time.time() * 1000)))
        await app.core.chats.reload_message(chat, sent.id)
        await asyncio.sleep(0.35)
        assert view._stick and view._at_bottom(), "a reaction must not scroll the newest message out of view"

        # someone typing updates the chat, and with it the header and banner: not the messages
        chat.typing = "Sam"
        await asyncio.sleep(0.3)
        chat.typing = ""
        await asyncio.sleep(0.3)
        assert view._stick and view._at_bottom(), "a typing notification must not move the message list"

        # the user scrolling up still lets go of the bottom
        view.vadj.set_value(max(0, view.vadj.get_value() - 200))
        await asyncio.sleep(0.4)
        assert not view._stick and not view._at_bottom() and view.jump_button.get_visible()

        await send(2, "a message after scrolling away")
        await asyncio.sleep(0.35)
        assert view._stick and view._at_bottom(), "sending scrolls to your own message again"
        await app.core.shutdown()

    run(scenario(), timeout=120)
