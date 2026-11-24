"""Unit tests for GUI performance, animation-contention, responsiveness, and lifecycle fixes.

Runs with stdlib unittest and never calls .present() on GTK windows (no live windows opened on display).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("WARD_LOG", "warning")
os.environ["GSETTINGS_BACKEND"] = "memory"
os.environ["WARD_SECRETS"] = "memory"

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from ward import aio
from ward.core.markup import parse_markdown
from ward.core.markup.tree import LINK, CodeBlock, Run, Table
from ward.core.models import AccountItem, AttachmentItem, ChatItem, ContactItem, MessageItem, MessageList
from ward.gui import render, style
from ward.gui.chatrow import ChatRow, ContactRow
from ward.gui.composer import Composer
from ward.gui.conversation import ConversationView
from ward.gui.message_row import STATUS_DELIVERED, STATUS_FAILED, STATUS_PENDING, STATUS_READ, STATUS_SENT, MessageRow
from ward.gui.sidebar import Sidebar
from ward.gui.window import MainWindow
from ward.settings import Settings


def _pump_idle(max_iters: int = 100) -> None:
    ctx = GLib.MainContext.default()
    for _ in range(max_iters):
        if not ctx.iteration(False):
            break


class _FakeAccounts:
    def __init__(self, items=None):
        self.store = Gio.ListStore.new(AccountItem)
        self.by_id: dict[int, AccountItem] = {}
        for a in (items or ()):
            self.store.append(a)
            self.by_id[a.id] = a

    def list(self):
        return [self.store.get_item(i) for i in range(self.store.get_n_items())]

    def get(self, aid: int):
        return self.by_id.get(aid)


class _FakeContacts:
    def __init__(self):
        from gi.repository import GObject

        class _Contacts(GObject.Object):
            __gsignals__ = {"changed": (GObject.SignalFlags.RUN_LAST, None, ())}

            def __init__(self):
                super().__init__()
                self.store = Gio.ListStore.new(ContactItem)

            def get(self, _aid, _jid):
                return None

        self.obj = _Contacts()


class _FakeChats:
    def __init__(self):
        from gi.repository import GObject

        class _Chats(GObject.Object):
            __gsignals__ = {
                "added": (GObject.SignalFlags.RUN_LAST, None, (object,)),
                "removed": (GObject.SignalFlags.RUN_LAST, None, (object,)),
                "activity": (GObject.SignalFlags.RUN_LAST, None, (object,)),
                "unread-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
            }

            def __init__(self):
                super().__init__()
                self.store = Gio.ListStore.new(ChatItem)
                self.lists: dict[int, MessageList] = {}
                self.drafts: list[tuple[int, str]] = []

            async def open_messages(self, chat):
                return self.lists.setdefault(chat.id, MessageList(chat))

            async def load_older(self, _chat, _limit=60):
                return 0

            def set_draft(self, chat, text):
                chat.draft = text
                self.drafts.append((chat.id, text))

        self.obj = _Chats()


class _FakeRequests:
    def __init__(self):
        from gi.repository import GObject

        class _Requests(GObject.Object):
            __gsignals__ = {"changed": (GObject.SignalFlags.RUN_LAST, None, ())}

            def items(self):
                return []

        self.obj = _Requests()


class _FakeMuc:
    def __init__(self):
        from gi.repository import GObject

        class _Muc(GObject.Object):
            __gsignals__ = {"occupants-changed": (GObject.SignalFlags.RUN_LAST, None, (object,))}

            def __init__(self):
                super().__init__()
                self.rooms = {}

            def room_of(self, chat):
                return self.rooms.get((chat.account_id, chat.jid))

            def sorted_occupants(self, _chat):
                return []

            def can_moderate(self, _chat):
                return False

        self.obj = _Muc()


class _FakeCore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.accounts = _FakeAccounts([AccountItem(id=1, jid="me@example.org", state="online")])
        self.contacts = _FakeContacts().obj
        self.chats = _FakeChats().obj
        self.requests = _FakeRequests().obj
        self.muc = _FakeMuc().obj
        self.calls = SimpleNamespace(available=lambda: False)
        self.messaging = SimpleNamespace(
            set_chat_state=lambda *_a: None,
            mark_read=lambda *_a: None,
        )
        self.names = SimpleNamespace(own_name=lambda _aid: "Me")
        self.db = SimpleNamespace()
        self.window_focused = True
        self.active_chat = None
        self.active_chat_calls: list[ChatItem | None] = []

    def set_active_chat(self, chat):
        self.active_chat = chat
        self.active_chat_calls.append(chat)


class _FakeRowView:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.chat = ChatItem(id=1, type="chat")
        self.first_unread_id = 0
        self.highlight_id = 0
        self.layout_changes = 0
        self.prev_calls = 0
        self.prev_map: dict[int, MessageItem | None] = {}
        self.core = SimpleNamespace(calls=SimpleNamespace(available=lambda: False))

    def _layout_changing(self):
        self.layout_changes += 1

    def _settle_bottom(self):
        pass

    def previous(self, msg: MessageItem):
        self.prev_calls += 1
        return self.prev_map.get(msg.id)

    def display_name(self, m: MessageItem):
        return "Me" if m.outgoing else "Sam"

    def avatar_path(self, _m: MessageItem):
        return ""

    def mention_names(self):
        return ["Sam", "Alex"]

    def on_link(self, *_a):
        return False

    def on_code_copy(self, *_a):
        pass

    def message_menu(self, *_a):
        pass

    def react(self, *_a):
        pass

    def start_reply(self, *_a):
        pass

    def jump_to(self, *_a):
        pass

    def show_edit_history(self, *_a):
        pass

    def retry(self, *_a):
        pass


class _CountingMessageList(MessageList):
    def __init__(self, chat: ChatItem):
        super().__init__(chat)
        self.items_calls = 0

    def items(self):
        self.items_calls += 1
        return super().items()


class GuiPerfFixesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._old_home = os.environ.get("WARD_HOME")
        os.environ["WARD_HOME"] = cls._tmpdir.name
        aio.install()

    @classmethod
    def tearDownClass(cls):
        if cls._old_home is None:
            os.environ.pop("WARD_HOME", None)
        else:
            os.environ["WARD_HOME"] = cls._old_home
        cls._tmpdir.cleanup()

    def setUp(self):
        self.settings = Settings(os.path.join(self._tmpdir.name, "settings-%s.json" % self._testMethodName))
        self.core = _FakeCore(self.settings)

    def _make_window_stub(self):
        win = SimpleNamespace(
            core=self.core,
            narrow=False,
            current_chat=None,
            split=Adw.OverlaySplitView(),
            toasts=Adw.ToastOverlay(),
            app=SimpleNamespace(open_uri=lambda *_a: None),
        )
        win.toggle_sidebar = lambda: None
        win.show_chat = lambda *_a, **_kw: None
        win.show_requests = lambda: None
        return win

    def test_message_row_incremental_updates_avoid_full_rebuilds(self):
        self.settings["appearance.layout"] = "bubbles"
        self.settings["appearance.avatars"] = "never"
        view = _FakeRowView(self.settings)
        row = MessageRow(view)

        m = MessageItem(
            id=10,
            chat_id=1,
            timestamp=1_700_000_000_000,
            direction=1,
            sender="me@example.org",
            body="hello world",
            status=STATUS_PENDING,
        )
        build_calls = 0
        orig_build = row._build

        def counted_build(prev):
            nonlocal build_calls
            build_calls += 1
            return orig_build(prev)

        row._build = counted_build
        row.bind(m, None)
        self.assertEqual(build_calls, 1)
        self.assertEqual(view.layout_changes, 0, "initial bind() must not call _layout_changing()")

        body_widget = row._col
        first_action = row.actions.get_first_child()

        # Selection toggle updates CSS class synchronously with zero rebuild
        m.selected = True
        self.assertTrue(row.row.has_css_class("selected"))
        m.selected = False
        self.assertFalse(row.row.has_css_class("selected"))
        _pump_idle()
        self.assertEqual(build_calls, 1)
        self.assertIs(row._col, body_widget)

        # Highlight toggle updates CSS class with zero rebuild
        view.highlight_id = m.id
        row.update_highlight()
        self.assertTrue(row.row.has_css_class("highlight"))
        view.highlight_id = 0
        row.update_highlight()
        self.assertFalse(row.row.has_css_class("highlight"))
        self.assertEqual(build_calls, 1)

        # Status progression (PENDING -> SENT -> DELIVERED -> READ) updates icons in-place
        for st in (STATUS_SENT, STATUS_DELIVERED, STATUS_READ):
            m.status = st
            _pump_idle()
        self.assertEqual(build_calls, 1)
        self.assertIs(row._col, body_widget)
        self.assertEqual(view.layout_changes, 0)

        # Reaction changes update reactions in-place without destroying message body
        m.reactions = [("👍", ["Sam"], False)]
        m.reactions_rev += 1
        _pump_idle()
        self.assertEqual(build_calls, 1)
        self.assertIsNotNone(row._reactions_widget)
        self.assertIs(row._col, body_widget)

        m.reactions = []
        m.reactions_rev += 1
        _pump_idle()
        self.assertEqual(build_calls, 1)
        self.assertIsNone(row._reactions_widget)

        # Failed status transition triggers full rebuild so Retry bar appears
        m.status = STATUS_FAILED
        _pump_idle()
        self.assertEqual(build_calls, 2)
        self.assertIsNotNone(row._failed_widget)
        # Hover bar buttons were reused across rebuilds
        self.assertIs(row.actions.get_first_child(), first_action)

        # Reply preview resolves in-place without full rebuild
        m2 = MessageItem(
            id=11,
            chat_id=1,
            timestamp=1_700_000_060_000,
            direction=0,
            sender="sam@example.org",
            body="reply body",
            reply_to="orig-1",
            reply_author="Me",
            reply_preview="...",
        )
        row.bind(m2, m)
        self.assertEqual(build_calls, 3)
        m2.reply_preview = "loaded preview"
        _pump_idle()
        self.assertEqual(build_calls, 3)
        self.assertEqual(row._reply_text_lbl.get_text(), "loaded preview")
        # Hover bar buttons still reused across bind() recycle
        self.assertIs(row.actions.get_first_child(), first_action)

        # Recycling to a status row resets time_hover so no stale detached label is held
        m_status = MessageItem(id=12, chat_id=1, timestamp=1_700_000_120_000, direction=0, kind="status", body="joined")
        row.bind(m_status, m2)
        self.assertIsNone(row.time_hover)
        self.assertIsNone(row.react_button)

        row.unbind()

    def test_conversation_chat_switch_revealer_waits_for_open_and_avoids_stale_switches(self):
        self.settings["appearance.chat_animations"] = True
        gtk_settings = Gtk.Settings.get_default()
        if gtk_settings is not None:
            gtk_settings.set_property("gtk-enable-animations", True)

        win = self._make_window_stub()
        view = ConversationView(win)
        chat1 = ChatItem(id=1, account_id=1, jid="a@example.org", name="Alice", unread=1, last_read_id=1)
        chat2 = ChatItem(id=2, account_id=1, jid="b@example.org", name="Bob", unread=0)

        ml1 = _CountingMessageList(chat1)
        ml1.has_older = False
        ml1.add_many([
            MessageItem(id=1, chat_id=1, timestamp=1000, direction=1, body="old"),
            MessageItem(id=2, chat_id=1, timestamp=2000, direction=0, body="unread"),
        ])
        ml2 = _CountingMessageList(chat2)
        ml2.has_older = False
        ml2.add_many([
            MessageItem(id=10, chat_id=2, timestamp=3000, direction=0, body="hello bob"),
        ])

        gate1 = asyncio.Event()
        gate2 = asyncio.Event()

        async def fake_open(chat):
            if chat is chat1:
                await gate1.wait()
                return ml1
            await gate2.wait()
            return ml2

        self.core.chats.open_messages = fake_open

        loop = aio.get_loop()

        async def scenario():
            view.show_chat(chat1)
            # Prepared for CROSSFADE synchronously, NOT revealing before DB fetch completes
            self.assertEqual(view.reveal.get_transition_type(), Gtk.RevealerTransitionType.CROSSFADE)
            self.assertFalse(view.reveal.get_reveal_child())
            _pump_idle()
            self.assertFalse(view.reveal.get_reveal_child(), "revealer must stay hidden until _open finishes")

            # Switch rapidly to chat2 before chat1's DB query returns
            view.show_chat(chat2)
            gate1.set()
            await asyncio.sleep(0)
            _pump_idle()
            self.assertIsNone(view.ml, "stale chat1 open result must be ignored")
            self.assertFalse(view.reveal.get_reveal_child())

            # Now chat2 finishes DB fetch
            gate2.set()
            await asyncio.sleep(0)
            self.assertIs(view.ml, ml2)
            self.assertEqual(ml2.items_calls, 0, "_open must not copy Gio.ListStore via ml.items()")
            _pump_idle()
            self.assertTrue(view.reveal.get_reveal_child(), "revealer starts after model & scroll are ready")

            # reply_last must not call ml.items()
            view.reply_last()
            self.assertEqual(ml2.items_calls, 0)
            self.assertIsNotNone(view.composer.reply_to)
            self.assertEqual(view.composer.reply_to.id, 10)

            # Switch back to chat1 and verify unread lookup works without ml.items()
            view.show_chat(chat1)
            await asyncio.sleep(0)
            _pump_idle()
            self.assertIs(view.ml, ml1)
            self.assertEqual(view.first_unread_id, 2)
            self.assertEqual(ml1.items_calls, 0)

            view.show_chat(None)

        loop.run_until_complete(scenario())

    def test_conversation_scroll_to_bottom_when_upper_is_zero_and_mark_read_dedup(self):
        win = self._make_window_stub()
        view = ConversationView(win)
        chat = ChatItem(id=7, account_id=1, jid="s@example.org", unread=2)
        ml = MessageList(chat)
        ml.has_older = False
        ml.add_many([
            MessageItem(id=i, chat_id=7, timestamp=i * 1000, direction=0, body="m%d" % i)
            for i in range(1, 5)
        ])
        view.chat = chat
        view.ml = ml

        scroll_targets: list[int] = []
        view.list.scroll_to = lambda pos, *_a: scroll_targets.append(pos)
        # Simulate unallocated list where upper == page_size == 0 (old _at_bottom returned True!)
        self.assertEqual(view.vadj.get_upper(), 0.0)
        view.scroll_to_bottom()
        self.assertEqual(scroll_targets, [3], "scroll_to_bottom must still scroll to last item when upper == 0")

        # _on_scrolled must not spam set_active_chat for the same unread count
        self.core.active_chat_calls.clear()
        view._stick = True
        view._on_scrolled(view.vadj)
        view._on_scrolled(view.vadj)
        view._on_scrolled(view.vadj)
        self.assertEqual(len(self.core.active_chat_calls), 1)
        view.show_chat(None)

    def test_search_is_debounced_cancels_stale_and_reuses_rows(self):
        win = self._make_window_stub()
        view = ConversationView(win)
        chat = ChatItem(id=3, account_id=1, jid="s@example.org")
        view.chat = chat
        view.search_bar.set_search_mode(True)

        calls: list[str] = []

        async def fake_search(_db, text, _chat_id, _limit=200):
            calls.append(text)
            if text == "missing":
                return []
            return [
                {"id": 100 + i, "snippet": "hit %s %d" % (text, i), "direction": 0,
                 "sender": "sam", "timestamp": 1_700_000_000_000 + i * 1000}
                for i in range(40)
            ]

        loop = aio.get_loop()

        async def scenario():
            with mock.patch("ward.gui.conversation.search_mod.search", side_effect=fake_search):
                # Empty search result first, then non-empty search reusing row 0 preserves title_lines=3
                view.search_entry.set_text("missing")
                view._on_search_activate(view.search_entry)
                await asyncio.sleep(0.05)
                _pump_idle()
                empty_row = view.results.get_row_at_index(0)
                self.assertIsNotNone(empty_row)
                self.assertEqual(empty_row.get_title_lines(), 3)

                # Rapid keystroke simulation calling _on_search directly before debounce fires
                for prefix in ("h", "he", "hel", "hell", "hello"):
                    view.search_entry.set_text(prefix)
                    view._on_search(view.search_entry)
                self.assertEqual(calls, ["missing"], "search must not fire synchronously on every keystroke")

                await asyncio.sleep(0.22)
                _pump_idle()
                self.assertEqual(calls, ["missing", "hello"], "debounced search fires once for final query")
                self.assertEqual(view.stack.get_visible_child_name(), "results")
                first_row = view.results.get_row_at_index(0)
                self.assertIs(first_row, empty_row)
                self.assertEqual(first_row.get_title_lines(), 3)
                # All 40 rows filled after idle chunks
                _pump_idle()
                self.assertIsNotNone(view.results.get_row_at_index(39))

                # Second search reuses the existing AdwActionRow instances instead of clear+recreate
                view.search_entry.set_text("world")
                view._on_search_activate(view.search_entry)
                await asyncio.sleep(0.05)
                _pump_idle()
                self.assertEqual(calls, ["missing", "hello", "world"])
                self.assertIs(view.results.get_row_at_index(0), first_row)
                self.assertEqual(first_row.get_title_lines(), 3)

                # Closing search clears result rows and cancels pending work
                view.search_button.set_active(False)
                self.assertIsNone(view.results.get_row_at_index(0))

            view.show_chat(None)

        loop.run_until_complete(scenario())

    def test_render_optimizations_cache_fast_paths_and_deferred_source_highlight(self):
        render._cache.clear()
        render._cache_chars = 0

        # Nicknames list changes must not bust cache when '@' is absent
        d1 = render.parsed(1, "hello **world**", True, nicknames=["Alice", "Bob"])
        d2 = render.parsed(1, "hello **world**", True, nicknames=["Alice", "Bob", "Carol"])
        self.assertIs(d1, d2)

        # Plain single-run paragraph skips markup/signal overhead
        r = render.Renderer(on_link=lambda *_a: False, on_code_copy=lambda *_a: None)
        plain_doc = parse_markdown("just plain text")
        box = r.build(plain_doc)
        lbl = box.get_first_child()
        self.assertIsInstance(lbl, Gtk.Label)
        self.assertEqual(lbl.get_text(), "just plain text")
        self.assertFalse(lbl.get_use_markup())

        # Heading builds markup once (no double work) and preserves links
        with mock.patch("ward.gui.render.markup", wraps=render.markup) as mk_spy:
            heading_doc = parse_markdown("# Title [link](https://example.org)")
            w = r.block(heading_doc.blocks[0])
            self.assertIsInstance(w, Gtk.Label)
            self.assertEqual(mk_spy.call_count, 1)
            self.assertTrue(any(LINK in run.styles for run in heading_doc.blocks[0].inlines))

        # _longest_line_sample avoids full splitlines and caps length
        self.assertEqual(render._longest_line_sample("short\n" + "x" * 500 + "\nend", limit=160), "x" * 160)

        # Code block without language uses lightweight Gtk.Label instead of GtkSourceView
        plain_code = r.block(CodeBlock("print(1)", ""))
        scroller = plain_code.get_last_child()
        inner = scroller.get_child()
        if isinstance(inner, Gtk.Viewport):
            inner = inner.get_child()
        self.assertIsInstance(inner, Gtk.Label)

        # Code block with known language defers highlighting until idle while rooted (never calls present!)
        py_code = r.block(CodeBlock("def f():\n    return 1", "python"))
        py_scroller = py_code.get_last_child()
        py_view = py_scroller.get_child()
        if isinstance(py_view, Gtk.Viewport):
            py_view = py_view.get_child()
        if py_view.__class__.__name__ == "View":
            buf = py_view.get_buffer()
            self.assertIsNone(buf.get_language(), "syntax highlight must be deferred during scroll")
            win = Gtk.Window()
            win.set_child(py_code)
            try:
                _pump_idle()
                self.assertIsNotNone(buf.get_language())
                self.assertTrue(buf.get_highlight_syntax())
            finally:
                win.set_child(None)
                win.destroy()

        # Large table defers selectable=True on cells until hovered; small table keeps selectable=True
        small_t = Table(
            header=[[Run("A")], [Run("B")]],
            rows=[[[Run("1")], [Run("2")]]],
            align=["left", "left"],
        )
        small_sc = r.table(small_t)
        small_grid = small_sc.get_child()
        if isinstance(small_grid, Gtk.Viewport):
            small_grid = small_grid.get_child()
        self.assertTrue(small_grid.get_child_at(0, 1).get_selectable())

        big_rows = [[[Run("c_%d_%d" % (ri, ci))] for ci in range(4)] for ri in range(20)]
        big_t = Table(header=[[Run("H%d" % ci)] for ci in range(4)], rows=big_rows, align=["left"] * 4)
        big_sc = r.table(big_t)
        big_grid = big_sc.get_child()
        if isinstance(big_grid, Gtk.Viewport):
            big_grid = big_grid.get_child()
        self.assertFalse(big_grid.get_child_at(0, 1).get_selectable())

    def test_sidebar_and_chatrow_diff_updates_and_burst_coalescing(self):
        win = self._make_window_stub()
        sidebar = Sidebar(win)
        chat = ChatItem(id=1, account_id=1, jid="sam@example.org", name="Sam", unread=0, last_activity=1000)
        self.core.chats.store.append(chat)

        row = ChatRow(sidebar)
        with mock.patch.object(row.avatar, "update", wraps=row.avatar.update) as av_spy:
            row.bind(chat)
            self.assertEqual(av_spy.call_count, 1)
            self.assertIn(row, sidebar._chat_rows)

            # Typing & unread changes must NOT re-decode/re-set avatar
            chat.typing = "Sam"
            chat.unread = 3
            self.assertEqual(av_spy.call_count, 1)
            self.assertTrue(row.has_css_class("unread"))
            self.assertEqual(row.badge.get_text(), "3")
            row.unbind()
            self.assertNotIn(row, sidebar._chat_rows)

        # Coalesced activity bursts only re-sort once on idle
        with mock.patch.object(sidebar.sorter, "changed", wraps=sidebar.sorter.changed) as sort_spy:
            for _ in range(20):
                self.core.chats.emit("activity", chat)
            self.assertEqual(sort_spy.call_count, 0)
            _pump_idle()
            self.assertEqual(sort_spy.call_count, 1)

        # Unread-changed skips refilter when chat_filter != "unread"
        with mock.patch.object(sidebar.filter, "changed", wraps=sidebar.filter.changed) as flt_spy:
            sidebar.chat_filter = "all"
            self.core.chats.emit("unread-changed")
            _pump_idle()
            self.assertEqual(flt_spy.call_count, 0)

        # ContactRow diff update
        contact = ContactItem(id=1, account_id=1, jid="sam@example.org", roster_name="Sam", subscription="both",
                              show="available", status="hi")
        crow = ContactRow(sidebar)
        with mock.patch.object(crow.avatar, "update", wraps=crow.avatar.update) as cav_spy:
            crow.bind(contact)
            self.assertEqual(cav_spy.call_count, 1)
            contact.status = "busy working"
            self.assertEqual(cav_spy.call_count, 1)
            crow.unbind()

        # drop_callbacks cancels _tick_id cleanly
        self.assertNotEqual(sidebar._tick_id, 0)
        sidebar.drop_callbacks()
        self.assertEqual(sidebar._tick_id, 0)

    def test_composer_typing_completion_and_single_pause_timer(self):
        chat = ChatItem(id=5, account_id=1, jid="room@conf.example.org", type="groupchat", name="Room",
                        our_nick="Me")
        ml = _CountingMessageList(chat)
        ml.add_many([
            MessageItem(id=1, chat_id=5, timestamp=1000, direction=1, kind="text", body="mine"),
            MessageItem(id=2, chat_id=5, timestamp=2000, direction=0, kind="text", body="theirs"),
        ])
        mock_view = SimpleNamespace(
            core=self.core,
            settings=self.settings,
            ml=ml,
            vadj=Gtk.Adjustment(),
            mention_names=lambda: ["Zed", "Aaron", "Alice", "Bob", "Me"],
            display_name=lambda _m: "Me",
            scroll_to_bottom=lambda: None,
        )
        comp = Composer(mock_view)
        comp.set_chat(chat)

        # Typing multiple characters reuses ONE _pause_timer without source_remove/timeout_add churn
        comp.buffer.insert_at_cursor("hello ")
        first_timer = comp._pause_timer
        self.assertNotEqual(first_timer, 0)
        comp.buffer.insert_at_cursor("world")
        self.assertEqual(comp._pause_timer, first_timer)

        # Long line before cursor must not falsely trigger '^' completion match
        comp.buffer.set_text("x" * 5 + ":" + "a" * 40)
        self.assertEqual(comp._word_before_cursor(), "")

        # Group @mention completion filters before sorting and updates cleanly
        comp.buffer.set_text("hi @al")
        self.assertEqual(comp._word_before_cursor(), "@al")
        self.assertEqual(comp._completions, [("Alice", "Alice")])

        # _last_own_message avoids ml.items()
        last = comp._last_own_message()
        self.assertIsNotNone(last)
        self.assertEqual(last.id, 1)
        self.assertEqual(ml.items_calls, 0)

        comp._cancel_timers()
        if comp.popover.get_parent() is not None:
            comp.popover.unparent()

    def test_style_skips_redundant_css_reload(self):
        style.install()
        with mock.patch("ward.gui.style._load", wraps=style._load) as load_spy:
            style.set_appearance(self.settings)
            first_calls = load_spy.call_count
            self.settings["appearance.chat_animations"] = False
            style.set_appearance(self.settings)
            self.assertEqual(load_spy.call_count, first_calls, "non-CSS setting must not reload CSS provider")

    def test_window_show_chat_skips_unnecessary_refilter(self):
        chat1 = ChatItem(id=1, account_id=1, jid="a@example.org", open=True)
        chat2 = ChatItem(id=2, account_id=1, jid="b@example.org", open=True)
        refilters = []
        fake_win = SimpleNamespace(
            core=self.core,
            settings=self.settings,
            current_chat=None,
            narrow=False,
            split=Adw.OverlaySplitView(),
            content_stack=Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE),
            conversation=SimpleNamespace(show_chat=lambda *_a: None),
            sidebar=SimpleNamespace(
                chat_filter="all",
                select=lambda *_a: None,
                _refilter=lambda: refilters.append(True),
            ),
        )
        fake_win.content_stack.add_named(Gtk.Box(), "empty")
        fake_win.content_stack.add_named(Gtk.Box(), "chat")

        MainWindow.show_chat(fake_win, chat1)
        self.assertEqual(refilters, [])
        fake_win.sidebar.chat_filter = "unread"
        MainWindow.show_chat(fake_win, chat2)
        self.assertEqual(refilters, [True])

    def test_message_row_unbind_cancels_pending_idles_and_retracted_hides_attachments(self):
        self.settings["appearance.layout"] = "bubbles"
        self.settings["appearance.avatars"] = "never"
        view = _FakeRowView(self.settings)
        row = MessageRow(view)
        att = AttachmentItem(id=1, url="https://upload.example/x.jpg", name="x.jpg", mime="image/jpeg", state="done")
        msg = MessageItem(id=20, chat_id=1, timestamp=1_700_000_000_000, direction=0, body="pic", attachments=[att])
        row.bind(msg, None)
        msg.body = "edited"
        row.animate_in()
        self.assertNotEqual(row._rebuild_idle, 0)
        row.unbind()
        self.assertEqual(row._rebuild_idle, 0)
        self.assertEqual(row._reveal_idle, 0)

        retracted = MessageItem(
            id=21, chat_id=1, timestamp=1_700_000_000_000, direction=0, body="", retracted=1, attachments=[att],
        )
        with mock.patch.object(row, "_attachment", wraps=row._attachment) as att_spy:
            row.bind(retracted, None)
            self.assertEqual(att_spy.call_count, 0)
        row.unbind()

    def test_new_message_pill_when_scrolled_up_and_bottom_snap_when_stuck(self):
        win = self._make_window_stub()
        view = ConversationView(win)
        chat = ChatItem(id=8, account_id=1, jid="p@example.org", name="Pat")
        ml = MessageList(chat)
        ml.has_older = False
        ml.add_many([
            MessageItem(id=i, chat_id=8, timestamp=i * 1000, direction=0, body="m%d" % i)
            for i in range(1, 5)
        ])
        view.chat = chat
        view.ml = ml
        view.vadj.configure(0.0, 0.0, 1000.0, 20.0, 200.0, 400.0)

        # Scrolled away from bottom: incoming message shows top pill and increments count
        view._stick = False
        view.vadj.set_value(0.0)
        m5 = MessageItem(id=5, chat_id=8, timestamp=5000, direction=0, body="hello")
        ml.add(m5)
        view._on_items_changed(ml.store, 4, 0, 1)
        self.assertTrue(view.new_msg_revealer.get_reveal_child())
        self.assertEqual(view.new_msg_label.get_text(), "New message")

        m6 = MessageItem(id=6, chat_id=8, timestamp=6000, direction=0, body="second")
        ml.add(m6)
        view._on_items_changed(ml.store, 5, 0, 1)
        self.assertEqual(view.new_msg_label.get_text(), "2 new messages")

        # Clicking the pill hides it, re-enables _stick, and scrolls to the newest item
        scroll_targets: list[int] = []
        view.list.scroll_to = lambda pos, *_a: scroll_targets.append(pos)
        view.new_msg_button.emit("clicked")
        self.assertFalse(view.new_msg_revealer.get_reveal_child())
        self.assertTrue(view._stick)
        self.assertEqual(scroll_targets, [5])
        self.assertEqual(view.vadj.get_value(), 600.0)

        # While stuck at bottom, upper growth updates vadj and queues an idle settle outside size_allocate
        view.vadj.set_upper(1120.0)
        self.assertEqual(view.vadj.get_value(), 720.0)
        self.assertNotEqual(view._settle_idle, 0)
        _pump_idle()
        self.assertEqual(view._settle_idle, 0)
        view.show_chat(None)


if __name__ == "__main__":
    unittest.main()
