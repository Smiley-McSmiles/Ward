"""The open chat: header (who, presence or topic, encryption state), banners for what needs a decision, the
message list (a recycling Gtk.ListView over the chat's MessageList), the members panel of groups, in-chat search
and the composer."""

from __future__ import annotations

import logging
import os
import shutil

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from ..core import search as search_mod
from ..core.models import AttachmentItem, ChatItem, MessageItem
from ..i18n import _, ngettext
from . import compat, widgets
from .composer import Composer
from .members import MembersPanel
from .message_row import MessageRow

log = logging.getLogger("ward.gui")

ENC_ICONS = {"omemo": ("ward-lock-symbolic", ""), "omemo-legacy": ("ward-lock-symbolic", "trust-undecided"),
             "pgp": ("ward-key-symbolic", ""), "plain": ("ward-lock-open-symbolic", "lock-off"),
             "blocked": ("ward-shield-warning-symbolic", "trust-distrusted")}
ENC_TITLES = {"omemo": "Encrypted with OMEMO", "omemo-legacy": "Encrypted with legacy OMEMO",
              "pgp": "Encrypted with OpenPGP", "plain": "Not encrypted", "blocked": "Can't send encrypted"}


class ConversationView(Gtk.Box):
    __gtype_name__ = "WardConversationView"

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.core = window.core
        self.settings = self.core.settings
        self.chat: ChatItem | None = None
        self.ml = None
        self.first_unread_id = 0
        self.highlight_id = 0
        self.rows: dict[int, MessageRow] = {}
        self._chat_handlers: list[tuple[object, int]] = []
        self._stick = True
        self._loading_older = False
        self._menu_msg: MessageItem | None = None

        self.toolbar = Adw.ToolbarView()
        self.append(self.toolbar)
        self.toolbar.set_vexpand(True)
        self._build_header()
        self._build_banner()
        self._build_search()
        self._build_list()
        self.composer = Composer(self)
        self.toolbar.add_bottom_bar(self.composer)
        self._build_actions()

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)
        self.settings.connect("changed", self._on_setting)
        self._apply_layout()

    # ================================================================== building
    def _build_header(self) -> None:
        self.header = Adw.HeaderBar()
        self.toolbar.add_top_bar(self.header)
        self.title_button = Gtk.Button(css_classes=["flat"])
        tb = Gtk.Box(spacing=8)
        self.title_avatar = widgets.Avatar(28)
        tb.append(self.title_avatar)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.title_label = Gtk.Label(css_classes=["title"], xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.subtitle_label = Gtk.Label(css_classes=["subtitle"], xalign=0, ellipsize=Pango.EllipsizeMode.END)
        texts.append(self.title_label)
        texts.append(self.subtitle_label)
        tb.append(texts)
        self.title_button.set_child(tb)
        self.title_button.set_tooltip_text(_("Details"))
        self.title_button.connect("clicked", lambda *_: self.show_details())
        self.header.set_title_widget(self.title_button)

        self.menu_button = Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text=_("Chat menu"))
        self.header.pack_end(self.menu_button)
        self.members_button = Gtk.ToggleButton(icon_name="ward-members-symbolic", tooltip_text=_("Members"))
        self.members_button.connect("toggled", self._on_members_toggled)
        self.header.pack_end(self.members_button)
        self.search_button = Gtk.ToggleButton(icon_name="system-search-symbolic", tooltip_text=_("Search (Ctrl+F)"))
        self.header.pack_end(self.search_button)
        self.enc_button = Gtk.MenuButton(tooltip_text=_("Encryption"))
        self.enc_popover = Gtk.Popover()
        self.enc_popover.connect("show", lambda *_: self._fill_encryption_popover())
        self.enc_button.set_popover(self.enc_popover)
        self.header.pack_end(self.enc_button)

    def _build_banner(self) -> None:
        self.banner = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        box = Gtk.Box(spacing=12, css_classes=["conversation-banner"])
        self.banner_icon = Gtk.Image()
        box.append(self.banner_icon)
        self.banner_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.banner_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        box.append(self.banner_label)
        self.banner_buttons = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        box.append(self.banner_buttons)
        self.banner.set_child(box)
        self.toolbar.add_top_bar(self.banner)

    def _build_search(self) -> None:
        self.search_bar = Gtk.SearchBar(show_close_button=True)
        self.search_entry = Gtk.SearchEntry(hexpand=True, placeholder_text=_("Search this chat (from: has:file "
                                                                             "before: after:)"))
        self.search_bar.set_child(self.search_entry)
        self.search_bar.connect_entry(self.search_entry)
        self.search_button.bind_property("active", self.search_bar, "search-mode-enabled",
                                         GObject.BindingFlags.BIDIRECTIONAL)
        self.search_entry.connect("search-changed", self._on_search)
        self.search_entry.connect("stop-search", lambda *_: self.search_button.set_active(False))
        self.search_bar.connect("notify::search-mode-enabled", self._on_search_mode)
        self.toolbar.add_top_bar(self.search_bar)

    def _build_list(self) -> None:
        self.factory = Gtk.SignalListItemFactory()
        self.factory.connect("setup", self._setup_row)
        self.factory.connect("bind", self._bind_row)
        self.factory.connect("unbind", self._unbind_row)
        self.list = Gtk.ListView(factory=self.factory, css_classes=["message-list"])
        self.list.set_single_click_activate(False)
        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.scroller.set_child(self.list)
        self.vadj = self.scroller.get_vadjustment()
        self.vadj.connect("value-changed", self._on_scrolled)
        self.vadj.connect("notify::upper", self._on_upper)
        self.vadj.connect("notify::page-size", self._on_upper)

        overlay = Gtk.Overlay()
        overlay.set_child(self.scroller)
        self.jump_button = Gtk.Button(icon_name="go-bottom-symbolic", css_classes=["circular", "osd", "jump-button"],
                                      halign=Gtk.Align.END, valign=Gtk.Align.END, visible=False,
                                      tooltip_text=_("Jump to the latest message"))
        self.jump_button.connect("clicked", lambda *_: self.scroll_to_bottom())
        overlay.add_overlay(self.jump_button)
        self.loading_spinner = compat.spinner()
        self.loading_spinner.set_halign(Gtk.Align.CENTER)
        self.loading_spinner.set_valign(Gtk.Align.START)
        self.loading_spinner.set_margin_top(12)
        self.loading_spinner.set_size_request(24, 24)
        self.loading_spinner.set_visible(False)
        overlay.add_overlay(self.loading_spinner)

        self.empty = Adw.StatusPage(icon_name="ward-chat-symbolic", title=_("No messages yet"),
                                    description=_("Say hello!"))
        self.empty.add_css_class("compact")
        self.results = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE,
                                   margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        self.results.connect("row-activated", self._on_result)
        results_sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=720)
        clamp.set_child(self.results)
        results_sc.set_child(clamp)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.stack.add_named(overlay, "messages")
        self.stack.add_named(self.empty, "empty")
        self.stack.add_named(results_sc, "results")

        self.split = Adw.OverlaySplitView(sidebar_position=Gtk.PackType.END, show_sidebar=False,
                                          max_sidebar_width=300, min_sidebar_width=220)
        self.split.set_content(self.stack)
        self.members = MembersPanel(self)
        self.split.set_sidebar(self.members)
        self.split.connect("notify::show-sidebar", lambda *_: self.members_button.set_active(
            self.split.get_show_sidebar()))
        self.toolbar.set_content(self.split)

    def _build_actions(self) -> None:
        group = Gio.SimpleActionGroup()
        for name, cb in (("reply", lambda m: self.start_reply(m)),
                         ("copy", lambda m: widgets.copy_text(self, m.body)),
                         ("quote", lambda m: self.composer.quote(m)),
                         ("edit", lambda m: self.composer.start_edit(m)),
                         ("delete-me", self._delete_for_me),
                         ("delete-all", self._delete_for_all),
                         ("moderate", self._moderate),
                         ("forward", self._forward),
                         ("details", self._details),
                         ("select", self._select),
                         ("copy-link", self._copy_link)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, cb=cb: self._menu_msg is not None and cb(self._menu_msg))
            group.add_action(a)
        self.insert_action_group("msg", group)
        chat_group = Gio.SimpleActionGroup()
        for name, cb in (("details", self.show_details), ("search", lambda: self.search_button.set_active(True)),
                         ("pin", lambda: self._toggle_flag("pinned")),
                         ("archive", lambda: self._toggle_flag("archived")),
                         ("mute", lambda: self._mute(-1)), ("mute-hour", lambda: self._mute(3600)),
                         ("mute-day", lambda: self._mute(86400)), ("unmute", lambda: self._mute(0)),
                         ("clear", self._clear_history), ("close", self._close_chat), ("leave", self._leave),
                         ("invite", self._invite), ("rejoin", self._rejoin), ("devices", self.show_devices)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, cb=cb: self.chat is not None and cb())
            chat_group.add_action(a)
        self.insert_action_group("chat", chat_group)

    # ================================================================== showing a chat
    def show_chat(self, chat: ChatItem | None) -> None:
        if chat is self.chat:
            return
        self._detach()
        self.chat = chat
        self.rows.clear()
        self.highlight_id = 0
        if chat is None:
            self.list.set_model(None)
            self.ml = None
            self.composer.set_chat(None)
            return
        for prop in ("name", "show", "typing", "subject", "member-count", "encryption-state", "encryption-reason",
                     "avatar", "joined", "join-error", "is-request", "new-devices", "muted-until", "pinned",
                     "our-role"):
            self._chat_handlers.append((chat, chat.connect("notify::" + prop, self._on_chat_changed)))
        acc = self.core.accounts.get(chat.account_id)
        if acc is not None:
            self._chat_handlers.append((acc, acc.connect("notify::state", self._on_chat_changed)))
        self._update_header()
        self._update_banner()
        self._update_menu()
        is_group = chat.type == "groupchat"
        self.members_button.set_visible(is_group)
        self.split.set_show_sidebar(is_group and self.settings["ui.members_panel"] and not self.window.narrow)
        self.members.set_chat(chat if is_group else None)
        self.composer.set_chat(chat)
        self.search_button.set_active(False)
        self.stack.set_visible_child_name("messages")
        widgets.run(self, self._open(chat), error=_("Could not load messages"))

    async def _open(self, chat: ChatItem) -> None:
        ml = await self.core.chats.open_messages(chat)
        if chat is not self.chat:
            return
        self.ml = ml
        self.first_unread_id = 0
        if chat.unread:
            for m in ml.items():
                if m.id > chat.last_read_id and not m.outgoing and m.kind != "status":
                    self.first_unread_id = m.id
                    break
        model = Gtk.NoSelection.new(ml.store)
        self._chat_handlers.append((ml.store, ml.store.connect("items-changed", self._on_items_changed)))
        self._chat_handlers.append((ml, ml.connect("notify::loading", lambda *_: self.loading_spinner.set_visible(
            ml.loading))))
        self.list.set_model(model)
        self._update_empty()
        self._stick = True
        if self.first_unread_id:
            idx = ml.index(ml.get(self.first_unread_id))
            GLib.idle_add(self._scroll_to_index, idx, False)
        else:
            GLib.idle_add(self.scroll_to_bottom)
        self.core.set_active_chat(chat)
        if len(ml) < 30 and ml.has_older:
            await self._load_older()

    def _detach(self) -> None:
        for obj, h in self._chat_handlers:
            try:
                obj.disconnect(h)
            except TypeError:
                pass
        self._chat_handlers = []
        if self.chat is not None:
            self.composer.save_draft()

    # ------------------------------------------------------------------ rows
    def _setup_row(self, _f, item: Gtk.ListItem) -> None:
        item.set_activatable(False)
        item.set_selectable(False)
        if hasattr(item, "set_focusable"):
            item.set_focusable(False)
        item.set_child(MessageRow(self))

    def _bind_row(self, _f, item: Gtk.ListItem) -> None:
        row: MessageRow = item.get_child()
        msg: MessageItem = item.get_item()
        row.bind(msg, self.previous(msg))
        self.rows[msg.id] = row

    def _unbind_row(self, _f, item: Gtk.ListItem) -> None:
        row: MessageRow = item.get_child()
        if row.msg is not None and self.rows.get(row.msg.id) is row:
            del self.rows[row.msg.id]
        row.unbind()

    def _on_items_changed(self, store, pos: int, removed: int, added: int) -> None:
        # the message after an insertion may now continue a group, or need its date separator
        nxt = store.get_item(pos + added) if pos + added < store.get_n_items() else None
        if nxt is not None and nxt.id in self.rows:
            self.rows[nxt.id]._changed()
        self._update_empty()
        if added and pos + added == store.get_n_items():
            newest = store.get_item(store.get_n_items() - 1)
            if newest is not None and newest.outgoing:
                self._stick = True
            if self._stick:
                GLib.idle_add(self.scroll_to_bottom)
            else:
                self.jump_button.set_visible(True)

    def _update_empty(self) -> None:
        if self.stack.get_visible_child_name() == "results":
            return
        empty = self.ml is not None and len(self.ml) == 0 and not self.ml.has_older
        self.stack.set_visible_child_name("empty" if empty else "messages")

    # ------------------------------------------------------------------ scrolling
    def _at_bottom(self) -> bool:
        return self.vadj.get_value() + self.vadj.get_page_size() >= self.vadj.get_upper() - 40

    def _on_scrolled(self, adj) -> None:
        self._stick = self._at_bottom()
        self.jump_button.set_visible(not self._stick)
        if self._stick and self.chat is not None and self.chat.unread and self.core.window_focused:
            self.core.set_active_chat(self.chat)
        if adj.get_value() < adj.get_page_size() * 0.6 and self.ml is not None and self.ml.has_older:
            widgets.run(self, self._load_older())

    def _on_upper(self, *_a) -> None:
        if self._stick:
            self.vadj.set_value(self.vadj.get_upper() - self.vadj.get_page_size())
        else:
            self.jump_button.set_visible(not self._at_bottom())

    def scroll_to_bottom(self) -> bool:
        self._stick = True
        n = self.ml.store.get_n_items() if self.ml is not None else 0
        if n:
            self.list.scroll_to(n - 1, Gtk.ListScrollFlags.NONE, None)
        self.vadj.set_value(self.vadj.get_upper() - self.vadj.get_page_size())
        self.jump_button.set_visible(False)
        return False

    def _scroll_to_index(self, idx: int, focus: bool = True) -> bool:
        if idx >= 0:
            self._stick = False
            self.list.scroll_to(idx, Gtk.ListScrollFlags.FOCUS if focus else Gtk.ListScrollFlags.NONE, None)
        return False

    async def _load_older(self) -> None:
        if self._loading_older or self.chat is None:
            return
        self._loading_older = True
        try:
            chat = self.chat
            await self.core.chats.load_older(chat)
            self._update_empty()
        finally:
            self._loading_older = False

    # ================================================================== header, banner, menu
    def _on_chat_changed(self, *_a) -> None:
        self._update_header()
        self._update_banner()
        self._update_menu()

    def _update_header(self) -> None:
        chat = self.chat
        if chat is None:
            return
        self.title_label.set_text(chat.name or chat.jid)
        group = chat.type == "groupchat"
        if chat.typing and self.settings["chats.show_typing"]:
            sub = _("%s is typing…") % chat.typing if "," not in chat.typing else _("%s are typing…") % chat.typing
        elif group:
            if not chat.joined:
                sub = chat.join_error or _("Not joined")
            elif chat.subject:
                sub = chat.subject.splitlines()[0]
            else:
                sub = ngettext("%d member", "%d members", chat.member_count) % chat.member_count
        else:
            contact = self.core.contacts.get(chat.account_id, chat.jid)
            if contact is not None and contact.show == "offline" and contact.last_seen:
                sub = _("Last seen %s") % widgets.relative_seen(contact.last_seen)
            elif contact is not None:
                sub = widgets.presence_text(contact.show, contact.status)
            else:
                sub = chat.jid
        if len(self.core.accounts.list()) > 1:
            acc = self.core.accounts.get(chat.account_id)
            if acc is not None:
                sub = "%s · %s" % (sub, acc.display_name)
        self.subtitle_label.set_text(sub)
        self.subtitle_label.set_tooltip_text(chat.subject if group and chat.subject else None)
        self.title_avatar.update(chat.name or chat.jid, chat.avatar, None if group else chat.show)
        icon, css = ENC_ICONS.get(chat.encryption_state or "omemo", ENC_ICONS["omemo"])
        self.enc_button.set_icon_name(icon)
        for c in ("trust-undecided", "trust-distrusted", "lock-off"):
            self.enc_button.remove_css_class(c)
        if css:
            self.enc_button.add_css_class(css)
        title = _(ENC_TITLES.get(chat.encryption_state or "omemo", "Encryption"))
        self.enc_button.set_tooltip_text("%s%s" % (title, ("\n" + chat.encryption_reason)
                                                   if chat.encryption_reason else ""))

    def _update_banner(self) -> None:
        chat = self.chat
        widgets.clear(self.banner_buttons)
        if chat is None:
            self.banner.set_reveal_child(False)
            return
        acc = self.core.accounts.get(chat.account_id)

        def show(icon: str, text: str, buttons: list[tuple[str, callable, str]]) -> None:
            self.banner_icon.set_from_icon_name(icon)
            self.banner_label.set_text(text)
            for label, cb, style_ in buttons:
                b = Gtk.Button(label=label, css_classes=[style_] if style_ else [])
                b.connect("clicked", lambda *_a, cb=cb: cb())
                self.banner_buttons.append(b)
            self.banner.set_reveal_child(True)

        if chat.is_request:
            show("dialog-question-symbolic", _("%s is not in your contacts. Messages from strangers can be spam.")
                 % (chat.name or chat.jid),
                 [(_("Block"), self._block_request, "destructive-action"),
                  (_("Accept"), self._accept_request, "suggested-action")])
        elif acc is not None and acc.state != "online":
            text = {"connecting": _("Connecting…"), "error": acc.error or _("Not connected"),
                    "locked": _("Enter your password to connect.")}.get(acc.state, _("You are offline."))
            if acc.state != "locked":
                text = "%s %s" % (text.rstrip(".") + ".", _("Messages you write are sent when you are back online."))
            show("network-offline-symbolic", text,
                 [(_("Connect"), lambda: widgets.run(self, self.core.connect_account(acc)), "")]
                 if acc.state in ("offline", "error") and acc.enabled else [])
        elif chat.type == "groupchat" and not chat.joined:
            show("dialog-warning-symbolic", chat.join_error or _("You are not in this group right now"),
                 [(_("Join"), self._rejoin, "suggested-action")])
        elif chat.encryption_state == "blocked" and self.core.omemo is None and \
                self.core.e2e.method(chat) == "omemo":
            show("ward-shield-warning-symbolic", _("Encryption support is not installed yet, so messages can't be "
                                                   "sent encrypted."),
                 [(_("Send unencrypted"), lambda: self._set_encryption("off"), ""),
                  (_("Install"), lambda: self.window.app.install_encryption(), "suggested-action")])
        elif chat.encryption_state == "blocked":
            buttons = [(_("Devices"), self.show_devices, "")]
            if chat.type != "groupchat" or chat.encryptable:
                buttons.append((_("Send unencrypted"), lambda: self._set_encryption("off"), "destructive-action"))
            else:
                buttons = [(_("Turn off encryption"), lambda: self._set_encryption("off"), "")]
            show("ward-shield-warning-symbolic", chat.encryption_reason, buttons)
        elif chat.new_devices:
            show("ward-shield-warning-symbolic",
                 ngettext("%(name)s has a new device. Check it if you want to be sure who reads your messages.",
                          "%(name)s has %(n)d new devices. Check them if you want to be sure who reads your messages.",
                          chat.new_devices) % {"name": chat.name or chat.jid, "n": chat.new_devices},
                 [(_("Review"), self.show_devices, "suggested-action")])
        else:
            self.banner.set_reveal_child(False)

    def _update_menu(self) -> None:
        chat = self.chat
        if chat is None:
            return
        menu = Gio.Menu()
        sec = Gio.Menu()
        sec.append(_("Details"), "chat.details")
        sec.append(_("Search"), "chat.search")
        if chat.type == "groupchat":
            sec.append(_("Invite…"), "chat.invite")
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append(_("Unpin") if chat.pinned else _("Pin"), "chat.pin")
        if chat.muted(GLib.get_real_time() // 1000):
            sec.append(_("Unmute"), "chat.unmute")
        else:
            mute = Gio.Menu()
            mute.append(_("For an hour"), "chat.mute-hour")
            mute.append(_("For a day"), "chat.mute-day")
            mute.append(_("Until I unmute"), "chat.mute")
            sec.append_submenu(_("Mute"), mute)
        sec.append(_("Unarchive") if chat.archived else _("Archive"), "chat.archive")
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append(_("Clear history…"), "chat.clear")
        if chat.type == "groupchat":
            sec.append(_("Leave group"), "chat.leave")
        else:
            sec.append(_("Close chat"), "chat.close")
        menu.append_section(None, sec)
        self.menu_button.set_menu_model(menu)

    # ------------------------------------------------------------------ encryption popover
    def _fill_encryption_popover(self) -> None:
        chat = self.chat
        if chat is None:
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12, margin_end=12,
                      margin_top=12, margin_bottom=12)
        box.set_size_request(300, -1)
        state = chat.encryption_state or "omemo"
        title = Gtk.Label(label=_(ENC_TITLES.get(state, "Encryption")), xalign=0, css_classes=["heading"])
        box.append(title)
        if chat.encryption_reason:
            box.append(widgets.label(chat.encryption_reason, ["dim-label"], wrap=True))
        elif state in ("omemo", "pgp"):
            box.append(widgets.label(_("Only the people in this chat can read the messages, not the servers."),
                                     ["dim-label"], wrap=True))
        choices = [("default", _("Default")), ("omemo", "OMEMO")]
        if self.core.pgp is not None and self.core.pgp.enabled and chat.type == "chat":
            choices.append(("pgp", "OpenPGP"))
        choices.append(("off", _("Off")))
        group = None
        current = chat.encryption or "default"
        for value, label in choices:
            b = Gtk.CheckButton(label=label, active=current == value)
            if group is None:
                group = b
            else:
                b.set_group(group)
            b.connect("toggled", lambda btn, v=value: btn.get_active() and self._set_encryption(v))
            box.append(b)
        dev = Gtk.Button(label=_("Devices and keys…"), css_classes=["flat"])
        dev.connect("clicked", lambda *_: (self.enc_popover.popdown(), self.show_devices()))
        box.append(dev)
        self.enc_popover.set_child(box)

    def _set_encryption(self, value: str) -> None:
        chat = self.chat
        if chat is None or chat.encryption == value:
            return

        async def go():
            await self.core.chats.set_flags(chat, encryption=value)
            self.core.e2e.refresh(chat)
        widgets.run(self, go())

    # ================================================================== MessageRow API
    def previous(self, msg: MessageItem) -> MessageItem | None:
        return self.ml.previous(msg) if self.ml is not None else None

    def display_name(self, m: MessageItem) -> str:
        if m.outgoing and self.chat is not None and self.chat.type != "groupchat":
            return self.core.names.own_name(self.chat.account_id)
        return m.sender_name or m.sender

    def avatar_path(self, m: MessageItem) -> str:
        chat = self.chat
        if chat is None:
            return ""
        if m.outgoing:
            acc = self.core.accounts.get(chat.account_id)
            return acc.avatar if acc is not None else ""
        if chat.type == "groupchat":
            room = self.core.muc.room_of(chat)
            occ = room.by_nick.get(m.sender) if room is not None else None
            if occ is not None and occ.avatar:
                return occ.avatar
            if m.sender_jid:
                c = self.core.contacts.get(chat.account_id, m.sender_jid)
                return c.avatar if c is not None else ""
            return ""
        return chat.avatar

    def mention_names(self) -> list[str]:
        chat = self.chat
        if chat is None or chat.type != "groupchat":
            return []
        room = self.core.muc.room_of(chat)
        return list(room.by_nick) if room is not None else []

    def on_link(self, uri: str, text: str) -> bool:
        if uri.lower().startswith("xmpp:"):
            self.window.app.open_uri(uri)
            return True
        shown = text.strip()
        if shown and "." in shown and " " not in shown and shown.lower() not in uri.lower():
            # a link whose text looks like another address: ask before opening
            compat.alert(self, _("Open this link?"), _("The link text says “%(text)s” but it opens\n%(uri)s")
                         % {"text": shown, "uri": uri},
                         [("cancel", _("Cancel"), ""), ("open", _("Open"), "suggested")],
                         lambda r: r == "open" and widgets.open_uri(self, uri), default="open")
            return True
        return False

    def show_edit_history(self, m: MessageItem) -> None:
        async def go():
            rows = await self.core.db.fetchall("SELECT body, timestamp FROM message_edits WHERE message_id = ? "
                                               "ORDER BY timestamp, id", (m.id,))
            d = compat.Dialog(_("Edit history"), 460, 420)
            view = Adw.ToolbarView()
            view.add_top_bar(Adw.HeaderBar())
            lb = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
            versions = [(r["body"], r["timestamp"]) for r in rows] + [(m.body, 0)]
            for i, (body, ts) in enumerate(versions):
                row = Adw.ActionRow(title=GLib.markup_escape_text(body),
                                    subtitle=_("Current") if i == len(versions) - 1 else
                                    (widgets.day_label(ts) + " " + widgets.clock(ts, self.settings)))
                row.set_title_lines(0)
                lb.append(row)
            sc = Gtk.ScrolledWindow(vexpand=True)
            clamp = Adw.Clamp(margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
            clamp.set_child(lb)
            sc.set_child(clamp)
            view.set_content(sc)
            d.set_child(view)
            d.present(self)
        widgets.run(self, go())

    def jump_to(self, msg_id: int, ref: str = "") -> None:
        async def go():
            ml = self.ml
            if ml is None:
                return
            target = ml.get(msg_id) if msg_id else None
            if target is None and ref:
                target = ml.find_ref(ref)
            tries = 0
            while target is None and ml.has_older and tries < 50:
                tries += 1
                if not await self.core.chats.load_older(self.chat, 200):
                    break
                target = ml.get(msg_id) if msg_id else ml.find_ref(ref)
            if target is None:
                compat.toast(self, _("The original message is not available"))
                return
            self.highlight(target)
        widgets.run(self, go())

    def highlight(self, target: MessageItem) -> None:
        old = self.highlight_id
        self.highlight_id = target.id
        for mid in (old, target.id):
            if mid in self.rows:
                self.rows[mid]._changed()
        self._scroll_to_index(self.ml.index(target))

        def clear():
            if self.highlight_id == target.id:
                self.highlight_id = 0
                if target.id in self.rows:
                    self.rows[target.id]._changed()
            return False
        GLib.timeout_add(2500, clear)

    def open_attachment(self, m: MessageItem, att: AttachmentItem) -> None:
        path = att.local_path if att.local_path and os.path.exists(att.local_path) else ""
        if not path:
            async def go():
                p = await self.core.files.download(self.chat, m, att)
                if p:
                    self.open_attachment(m, att)
            widgets.run(self, go(), error=_("Download failed"))
            return
        if att.is_image:
            from .dialogs import ImageViewer

            ImageViewer(self, att).present(self)
        else:
            widgets.open_file(self, path)

    def save_attachment(self, att: AttachmentItem) -> None:
        path = att.local_path
        if not path or not os.path.exists(path):
            return
        dialog = Gtk.FileDialog(initial_name=att.name or os.path.basename(path))

        def done(d, res):
            try:
                f = d.save_finish(res)
            except GLib.Error:
                return
            if f is not None and f.get_path():
                try:
                    shutil.copyfile(path, f.get_path())
                    compat.toast(self, _("Saved"))
                except OSError as e:
                    compat.toast(self, str(e))
        dialog.save(self.get_root(), None, done)

    def download(self, m: MessageItem, att: AttachmentItem) -> None:
        widgets.run(self, self.core.files.download(self.chat, m, att), error=_("Download failed"))

    def react(self, m: MessageItem, emoji: str) -> None:
        widgets.run(self, self.core.messaging.react(self.chat, m, emoji), error=_("Could not react"))

    def retry(self, m: MessageItem) -> None:
        widgets.run(self, self.core.messaging.retry(self.chat, m), error=_("Could not send"))

    def start_reply(self, m: MessageItem) -> None:
        self.composer.start_reply(m)

    def message_menu(self, row: Gtk.Widget, m: MessageItem | None, x, y, button: Gtk.Widget | None = None) -> None:
        if m is None or m.kind == "status" or self.chat is None:
            return
        self._menu_msg = m
        menu = Gio.Menu()
        sec = Gio.Menu()
        if not m.retracted:
            sec.append(_("Reply"), "msg.reply")
            if m.body:
                sec.append(_("Copy text"), "msg.copy")
                sec.append(_("Quote"), "msg.quote")
            sec.append(_("Forward…"), "msg.forward")
            if self.chat.type == "groupchat" and m.stanza_id:
                sec.append(_("Copy link"), "msg.copy-link")
        menu.append_section(None, sec)
        sec = Gio.Menu()
        if m.outgoing and not m.retracted and m.kind in ("text", "me"):
            sec.append(_("Edit"), "msg.edit")
        sec.append(_("Delete for me"), "msg.delete-me")
        if m.outgoing and not m.retracted:
            sec.append(_("Delete for everyone"), "msg.delete-all")
        elif self.chat.type == "groupchat" and not m.retracted and self.core.muc.can_moderate(self.chat):
            sec.append(_("Remove as moderator…"), "msg.moderate")
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append(_("Message details"), "msg.details")
        menu.append_section(None, sec)
        pop = Gtk.PopoverMenu.new_from_model(menu)
        pop.set_has_arrow(button is not None)
        parent = button or row
        pop.set_parent(parent)
        if button is None and x is not None:
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
            pop.set_pointing_to(rect)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    # ------------------------------------------------------------------ message actions
    def _delete_for_me(self, m: MessageItem) -> None:
        widgets.run(self, self.core.chats.delete_for_me(self.chat, m))

    def _delete_for_all(self, m: MessageItem) -> None:
        def go(resp):
            if resp == "delete":
                widgets.run(self, self.core.messaging.retract(self.chat, m), error=_("Could not delete"))
        if not self.settings["chats.confirm_delete"]:
            go("delete")
            return
        compat.alert(self, _("Delete for everyone?"),
                     _("The message is deleted on all devices. Apps that don't support deleting keep showing it."),
                     [("cancel", _("Cancel"), ""), ("delete", _("Delete"), "destructive")], go)

    def _moderate(self, m: MessageItem) -> None:
        entry = Gtk.Entry(placeholder_text=_("Reason (optional)"))

        def go(resp):
            if resp == "remove":
                widgets.run(self, self.core.muc.moderate(self.chat, m, entry.get_text()), error=_("Could not remove"))
        compat.alert(self, _("Remove this message?"), _("It is removed for everyone in the group."),
                     [("cancel", _("Cancel"), ""), ("remove", _("Remove"), "destructive")], go, extra=entry)

    def _forward(self, m: MessageItem) -> None:
        from .dialogs import ChatPicker

        def picked(chat):
            text = m.body
            if m.attachments and not text:
                text = "\n".join(a.url for a in m.attachments if a.url)
            widgets.run(self, self.core.messaging.send_text(chat, text), error=_("Could not forward"))
            compat.toast(self, _("Forwarded to %s") % (chat.name or chat.jid))
        ChatPicker(self.window, _("Forward to…"), picked).present(self)

    def _details(self, m: MessageItem) -> None:
        from .dialogs import MessageDetails

        MessageDetails(self, m).present(self)

    def _select(self, m: MessageItem) -> None:
        m.selected = not m.selected

    def _copy_link(self, m: MessageItem) -> None:
        widgets.copy_text(self, "xmpp:%s?join;id=%s" % (self.chat.jid, m.stanza_id))

    # ------------------------------------------------------------------ chat actions
    def show_details(self) -> None:
        if self.chat is None:
            return
        from .details import DetailsDialog

        DetailsDialog(self.window, self.chat).present(self)

    def show_devices(self) -> None:
        if self.chat is None:
            return
        from .details import DetailsDialog

        DetailsDialog(self.window, self.chat, page="devices").present(self)

    def _toggle_flag(self, flag: str) -> None:
        chat = self.chat
        widgets.run(self, self.core.chats.set_flags(chat, **{flag: not getattr(chat, flag)}))

    def _mute(self, seconds: int) -> None:
        chat = self.chat
        until = -1 if seconds < 0 else (GLib.get_real_time() // 1000 + seconds * 1000 if seconds else 0)
        widgets.run(self, self.core.chats.set_flags(chat, muted_until=until))
        self._update_menu()

    def _clear_history(self) -> None:
        chat = self.chat
        compat.alert(self, _("Clear history?"), _("Messages are deleted from this computer only."),
                     [("cancel", _("Cancel"), ""), ("clear", _("Clear"), "destructive")],
                     lambda r: r == "clear" and widgets.run(self, self.core.chats.clear_history(chat)))

    def _close_chat(self) -> None:
        chat = self.chat
        widgets.run(self, self.core.chats.close(chat))
        self.window.show_chat(None)

    def _leave(self) -> None:
        chat = self.chat

        def go(resp):
            if resp == "leave":
                widgets.run(self, self.core.muc.leave(chat), error=_("Could not leave"))
                self.window.show_chat(None)
        compat.alert(self, _("Leave %s?") % (chat.name or chat.jid),
                     _("You stop getting its messages. You can join again later if the group allows it."),
                     [("cancel", _("Cancel"), ""), ("leave", _("Leave"), "destructive")], go)

    def _invite(self) -> None:
        from .dialogs import InviteDialog

        InviteDialog(self.window, self.chat).present(self)

    def _rejoin(self) -> None:
        chat = self.chat
        acc = self.core.accounts.get(chat.account_id)
        if acc is not None:
            widgets.run(self, self.core.muc.join(acc, chat.jid), error=_("Could not join"))

    def _accept_request(self) -> None:
        chat = self.chat
        acc = self.core.accounts.get(chat.account_id)
        widgets.run(self, self.core.accept_request(acc, chat.jid))

    def _block_request(self) -> None:
        chat = self.chat
        acc = self.core.accounts.get(chat.account_id)
        check = Gtk.CheckButton(label=_("Report as spam"))

        def go(resp):
            if resp == "block":
                widgets.run(self, self.core.decline_request(acc, chat.jid, block=True, spam=check.get_active()))
                self.window.show_chat(None)
        compat.alert(self, _("Block %s?") % chat.jid, _("You won't get messages or requests from them."),
                     [("cancel", _("Cancel"), ""), ("block", _("Block"), "destructive")], go, extra=check)

    # ------------------------------------------------------------------ members and search
    def _on_members_toggled(self, btn) -> None:
        active = btn.get_active()
        if self.split.get_show_sidebar() != active:
            self.split.set_show_sidebar(active)
        if not self.window.narrow and self.chat is not None and self.chat.type == "groupchat":
            self.settings["ui.members_panel"] = active

    def set_narrow(self, narrow: bool) -> None:
        self.split.set_collapsed(narrow)
        self.search_button.set_visible(not narrow)      # still in the chat menu and on Ctrl+F
        if narrow:
            self.split.set_show_sidebar(False)

    def _on_search_mode(self, *_a) -> None:
        if not self.search_bar.get_search_mode():
            self.search_entry.set_text("")
            self.stack.set_visible_child_name("messages")
            self._update_empty()

    def _on_search(self, entry) -> None:
        text = entry.get_text().strip()
        if not text or self.chat is None:
            self.stack.set_visible_child_name("messages")
            self._update_empty()
            return
        chat = self.chat

        async def go():
            rows = await search_mod.search(self.core.db, text, chat.id)
            if entry.get_text().strip() != text:
                return
            widgets.clear(self.results)
            if not rows:
                self.results.append(Adw.ActionRow(title=_("Nothing found"), sensitive=False))
            for r in rows:
                snippet = (r["snippet"] or "").replace("\x02", "\x00").replace("\x03", "\x01")
                snippet = widgets.escape(snippet).replace("\x00", "<b>").replace("\x01", "</b>")
                who = _("You") if r["direction"] == 1 else r["sender"]
                row = Adw.ActionRow(title=snippet, activatable=True,
                                    subtitle="%s · %s %s" % (widgets.escape(who), widgets.day_label(r["timestamp"]),
                                                             widgets.clock(r["timestamp"], self.settings)))
                row.set_title_lines(3)
                row.msg_id = r["id"]
                self.results.append(row)
            self.stack.set_visible_child_name("results")
        widgets.run(self, go())

    def _on_result(self, _lb, row) -> None:
        mid = getattr(row, "msg_id", 0)
        if not mid:
            return
        self.stack.set_visible_child_name("messages")
        self.jump_to(mid)

    # ------------------------------------------------------------------ misc
    def _on_drop(self, _target, value, _x, _y) -> bool:
        files = value.get_files() if hasattr(value, "get_files") else []
        paths = [f.get_path() for f in files if f.get_path()]
        if paths and self.chat is not None:
            self.composer.send_files(paths)
            return True
        return False

    def _apply_layout(self) -> None:
        for c in ("bubbles", "cozy", "compact"):
            self.list.remove_css_class(c)
        self.list.add_css_class(self.settings["appearance.layout"])

    def _on_setting(self, _s, key: str) -> None:
        if key == "appearance.layout":
            self._apply_layout()
        if key.startswith("appearance.") or key in ("chats.collapse_after", "chats.markdown_others",
                                                    "chats.quick_reactions"):
            for row in list(self.rows.values()):
                row._changed()

    def reply_last(self) -> None:
        """Ctrl+R: reply to the newest message from someone else."""
        if self.ml is None:
            return
        for m in reversed(self.ml.items()):
            if not m.outgoing and not m.retracted and m.kind in ("text", "me", "file"):
                self.start_reply(m)
                return

    def focus_composer(self) -> None:
        self.composer.grab_focus_text()
