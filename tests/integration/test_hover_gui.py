"""Quick reactions and reply float over the message corner, Discord-style.

They used to get their own line under the message, which grew the row and pushed every row below down.  They are
now an overlay child at the top-right: hovering shows them without changing the message's size or moving anything.
"""

import asyncio
import time

import pytest
from helpers import wait_for
from test_gui import app  # noqa: F401 - the fixture

pytestmark = [pytest.mark.integration, pytest.mark.gui]


def test_hover_actions_float_without_moving_the_message(app, run):  # noqa: F811
    from ward.core.chats import insert_message_job

    async def scenario():
        await wait_for(lambda: app.core.started, 20, "core start")
        acc = await app.core.add_account("hover@localhost", "x", "never", connect=False)
        chat = await app.core.open_chat(acc, "sam@localhost")
        app.core.settings.set("appearance.layout", "bubbles")
        now = int(time.time() * 1000)

        def fill(conn):
            # enough messages for the list to scroll, so a layout change would show up as a scroll jump
            for i in range(39):
                insert_message_job(conn, {
                    "chat_id": chat.id, "origin_id": "hover%d" % i, "msg_id": "hover%d" % i, "direction": 0,
                    "timestamp": now - (40 - i) * 60_000, "sender": "sam@localhost",
                    "sender_jid": "sam@localhost", "body": "message %d" % i, "is_markdown": 0,
                    "kind": "text", "status": 0, "extra": {}})
            insert_message_job(conn, {
                "chat_id": chat.id, "origin_id": "hover-long", "msg_id": "hover-long", "direction": 0,
                "timestamp": now, "sender": "sam@localhost", "sender_jid": "sam@localhost",
                "body": ("A message long enough to wrap over several lines.\n"
                         "- with a list, so squeezing it would change its height\n"
                         "- and some **bold** words"),
                "is_markdown": 1, "kind": "text", "status": 0, "extra": {}})

        await app.core.db.run(fill)
        w = app.window
        w.set_default_size(900, 700)
        w.show_chat(chat)
        view = w.conversation
        await wait_for(lambda: view.rows and view._at_bottom(), 10, "the list at the bottom")
        row = max(view.rows.values(), key=lambda r: r.msg.timestamp)
        await wait_for(lambda: row.row.get_width() > 0, 5, "the row laid out")
        await asyncio.sleep(0.2)

        value = view.vadj.get_value()
        width, height = row.row.get_width(), row.get_height()
        row._hover(True)
        await wait_for(row.actions.get_sensitive, 5, "the actions on hover")
        await asyncio.sleep(0.2)
        assert row.actions.get_opacity() > 0.99
        assert row.actions.get_parent() is row.overlay, "the actions float over the message"
        assert row.row.get_width() == width and row.get_height() == height, \
            "hovering must not move or resize the message"
        assert view.vadj.get_value() == value and view._at_bottom(), "hovering must not move the list"
        ok, box = row.actions.compute_bounds(row.row)
        assert ok
        assert box.get_y() < row.row.get_height() * 0.5, "the actions float near the top"
        assert box.get_x() + box.get_width() > row.row.get_width() * 0.7, "the actions sit on the right"

        row._hover(False)
        await wait_for(lambda: not row.actions.get_sensitive() and row.actions.get_opacity() < 0.01, 5,
                       "the actions hidden again")
        await app.core.shutdown()

    run(scenario(), timeout=60)
