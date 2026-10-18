"""The sidebar: accounts and presence, the chat list (pinned first, then by activity) with filters and search,
requests, and the contacts list."""

from __future__ import annotations

import time

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..core.models import AccountItem, ChatItem, ContactItem
from ..i18n import _, ngettext
from . import widgets
from .chatrow import ChatRow, ContactRow

FILTERS = (("all", "All"), ("unread", "Unread"), ("people", "People"), ("groups", "Groups"))
SHOWS = (("available", "Available", "user-available-symbolic"), ("away", "Away", "user-away-symbolic"),
         ("dnd", "Busy", "user-busy-symbolic"), ("xa", "Away for a while", "user-idle-symbolic"),
         ("offline", "Offline", "user-offline-symbolic"))


class Sidebar(Gtk.Box):
    __gtype_name__ = "WardSidebar"

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.core = window.core
        self.settings = self.core.settings
        self.account_filter = self.settings["ui.account_filter"]
        self.chat_filter = self.settings["ui.chat_filter"]
        self.show_archived = False
        self._selecting = False
        self._stripe_css: dict[str, Gtk.CssProvider] = {}

        view = Adw.ToolbarView()
        view.set_vexpand(True)
        self.append(view)
        header = Adw.HeaderBar()
        view.add_top_bar(header)
        self.title = Adw.WindowTitle(title="Ward")
        header.set_title_widget(self.title)

        self.account_button = Gtk.MenuButton(tooltip_text=_("Accounts and status"))
        self.account_avatar = widgets.Avatar(24)
        self.account_button.set_child(self.account_avatar)
        self.account_button.add_css_class("flat")
        self.account_popover = Gtk.Popover()
        self.account_popover.connect("show", lambda *_: self._fill_accounts())
        self.account_button.set_popover(self.account_popover)
        header.pack_start(self.account_button)

        main_menu = Gio.Menu()
        sec = Gio.Menu()
        sec.append(_("Preferences"), "app.preferences")
        sec.append(_("Keyboard Shortcuts"), "win.show-help-overlay")
        sec.append(_("About Ward"), "app.about")
        main_menu.append_section(None, sec)
        tools = Gio.Menu()
        tools.append(_("Server administration"), "win.admin")
        tools.append(_("XML console"), "win.xml-console")
        tools.append(_("Service discovery"), "win.disco")
        main_menu.append_section(None, tools)
        sec = Gio.Menu()
        sec.append(_("Quit"), "app.quit")
        main_menu.append_section(None, sec)
        self.main_menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=main_menu,
                                               tooltip_text=_("Main menu"), primary=True)
        header.pack_end(self.main_menu_button)
        new_menu = Gio.Menu()
        new_menu.append(_("New chat"), "win.start-chat")
        new_menu.append(_("New group"), "win.new-group")
        new_menu.append(_("Join group or channel"), "win.join-group")
        new_menu.append(_("Add contact"), "win.add-contact")
        self.new_button = Gtk.MenuButton(icon_name="list-add-symbolic", menu_model=new_menu,
                                         tooltip_text=_("New chat"))
        header.pack_end(self.new_button)

        # connection problems of any account
        self.status = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        sbox = Gtk.Box(spacing=8, css_classes=["conversation-banner"])
        self.status_label = Gtk.Label(xalign=0, hexpand=True, wrap=True, css_classes=["caption"])
        self.status_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        sbox.append(self.status_label)
        self.status_button = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["flat"])
        self.status_button.connect("clicked", lambda *_: self._status_action())
        sbox.append(self.status_button)
        self.status.set_child(sbox)
        view.add_top_bar(self.status)

        self.stack = Adw.ViewStack(vexpand=True)
        view.set_content(self.stack)
        self.stack.add_titled_with_icon(self._build_chats(), "chats", _("Chats"), "ward-chat-symbolic")
        self.stack.add_titled_with_icon(self._build_contacts(), "contacts", _("Contacts"), "ward-contacts-symbolic")
        bar = Adw.ViewSwitcherBar(stack=self.stack, reveal=True)
        view.add_bottom_bar(bar)

        self.core.accounts.store.connect("items-changed", lambda *_: self._accounts_changed())
        self.core.chats.connect("activity", lambda *_: self._resort())
        self.core.chats.connect("unread-changed", lambda *_: self._refilter())
        self.core.chats.connect("added", lambda *_: self._refilter())
        self.core.requests.connect("changed", lambda *_: self._update_requests())
        self.core.contacts.connect("changed", lambda *_: self.contact_sorter.changed(Gtk.SorterChange.DIFFERENT))
        self.settings.connect("changed", self._on_setting)
        self._account_handlers: dict[int, list[int]] = {}
        self._accounts_changed()
        self._update_requests()
        GLib.timeout_add_seconds(60, self._tick)

    # ================================================================== chats page
    def _build_chats(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.search = Gtk.SearchEntry(placeholder_text=_("Search chats"), margin_start=8, margin_end=8,
                                      margin_top=6, margin_bottom=4)
        self.search.connect("search-changed", lambda *_: self._refilter())
        self.search.connect("activate", lambda *_: self._activate_first())
        box.append(self.search)
        chips = Gtk.Box(spacing=4, css_classes=["filter-chips"], margin_start=8, margin_end=8, margin_bottom=4)
        self.chips: dict[str, Gtk.ToggleButton] = {}
        group = None
        for key, label in FILTERS:
            b = Gtk.ToggleButton(label=_(label), active=self.chat_filter == key)
            if group is None:
                group = b
            else:
                b.set_group(group)
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self._set_filter(k))
            chips.append(b)
            self.chips[key] = b
        self.archive_button = Gtk.ToggleButton(icon_name="folder-symbolic", tooltip_text=_("Archived chats"),
                                               css_classes=["flat"], halign=Gtk.Align.END, hexpand=True)
        self.archive_button.connect("toggled", lambda b: self._set_archived(b.get_active()))
        chips.append(self.archive_button)
        box.append(chips)

        self.requests_button = Gtk.Button(css_classes=["flat", "request-row"], visible=False, margin_start=6,
                                          margin_end=6)
        rb = Gtk.Box(spacing=10, margin_start=6)
        rb.append(Gtk.Image.new_from_icon_name("mail-unread-symbolic"))
        self.requests_label = Gtk.Label(xalign=0, hexpand=True)
        rb.append(self.requests_label)
        self.requests_button.set_child(rb)
        self.requests_button.connect("clicked", lambda *_: self.window.show_requests())
        box.append(self.requests_button)

        self.filter = Gtk.CustomFilter.new(self._filter_chat)
        self.sorter = Gtk.CustomSorter.new(self._sort_chats)
        filtered = Gtk.FilterListModel(model=self.core.chats.store, filter=self.filter)
        self.sorted = Gtk.SortListModel(model=filtered, sorter=self.sorter)
        self.selection = Gtk.SingleSelection(model=self.sorted, autoselect=False, can_unselect=True)
        self.selection.connect("notify::selected-item", self._on_selected)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, li: li.set_child(ChatRow(self)))
        factory.connect("bind", lambda _f, li: li.get_child().bind(li.get_item()))
        factory.connect("unbind", lambda _f, li: li.get_child().unbind())
        self.list = Gtk.ListView(model=self.selection, factory=factory, css_classes=["navigation-sidebar",
                                                                                    "chat-list"])
        self.list.connect("activate", lambda _l, pos: self._on_activate(pos))
        click = Gtk.GestureClick(button=3)
        click.connect("pressed", self._on_right_click)
        self.list.add_controller(click)
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(self.list)
        self.chats_stack = Gtk.Stack()
        self.chats_stack.add_named(sc, "list")
        empty = Adw.StatusPage(icon_name="ward-chat-symbolic", title=_("No chats"),
                               description=_("Start a chat or join a group with the + button"))
        empty.add_css_class("compact")
        self.chats_stack.add_named(empty, "empty")
        box.append(self.chats_stack)
        self.sorted.connect("items-changed", lambda *_: self._update_empty())
        self._update_density()
        return box

    def _filter_chat(self, chat: ChatItem) -> bool:
        if not chat.open or chat.is_request:
            return False
        if chat.archived != self.show_archived:
            return False
        if self.account_filter and str(chat.account_id) != self.account_filter:
            return False
        f = self.chat_filter
        if f == "unread" and not (chat.unread or chat.mentions or chat is self.window.current_chat):
            return False
        if f == "people" and chat.type == "groupchat":
            return False
        if f == "groups" and chat.type != "groupchat":
            return False
        text = self.search.get_text().strip().lower() if hasattr(self, "search") else ""
        return not text or text in (chat.name or "").lower() or text in chat.jid.lower()

    @staticmethod
    def _sort_chats(a: ChatItem, b: ChatItem, *_x) -> int:
        if a.pinned != b.pinned:
            return -1 if a.pinned else 1
        if a.last_activity != b.last_activity:
            return -1 if a.last_activity > b.last_activity else 1
        return (a.id > b.id) - (a.id < b.id)

    def _refilter(self) -> None:
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self._reselect()

    def _resort(self) -> None:
        self.sorter.changed(Gtk.SorterChange.DIFFERENT)
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self._reselect()

    def _update_empty(self) -> None:
        self.chats_stack.set_visible_child_name("list" if self.sorted.get_n_items() else "empty")

    def _set_filter(self, key: str) -> None:
        self.chat_filter = key
        self.settings["ui.chat_filter"] = key
        self._refilter()

    def _set_archived(self, archived: bool) -> None:
        self.show_archived = archived
        self.title.set_subtitle(_("Archived") if archived else self._subtitle())
        self._refilter()

    def _activate_first(self) -> None:
        if self.sorted.get_n_items():
            self.window.show_chat(self.sorted.get_item(0))

    def _on_selected(self, sel, _p) -> None:
        if self._selecting:
            return
        chat = sel.get_selected_item()
        if chat is not None and chat is not self.window.current_chat:
            self.window.show_chat(chat)

    def _on_activate(self, pos: int) -> None:
        chat = self.sorted.get_item(pos)
        if chat is not None:
            self.window.show_chat(chat, force=True)

    def select(self, chat: ChatItem | None) -> None:
        """Reflect the open chat in the list without re-opening it."""
        self._selecting = True
        try:
            if chat is None:
                self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
                return
            for i in range(self.sorted.get_n_items()):
                if self.sorted.get_item(i) is chat:
                    self.selection.set_selected(i)
                    return
            self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
        finally:
            self._selecting = False

    def _reselect(self) -> None:
        GLib.idle_add(lambda: self.select(self.window.current_chat) and False)

    def _on_right_click(self, gesture, _n, x, y) -> None:
        row = self.list.pick(x, y, Gtk.PickFlags.DEFAULT)
        while row is not None and not isinstance(row, ChatRow):
            row = row.get_parent()
        if row is not None and row.chat is not None:
            self.chat_menu(row, row.chat)

    def chat_menu(self, widget: Gtk.Widget, chat: ChatItem) -> None:
        self._menu_chat = chat
        group = Gio.SimpleActionGroup()

        def add(name, cb):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_: cb())
            group.add_action(a)
        add("pin", lambda: widgets.run(self, self.core.chats.set_flags(chat, pinned=not chat.pinned)))
        add("archive", lambda: widgets.run(self, self.core.chats.set_flags(chat, archived=not chat.archived)))
        add("read", lambda: widgets.run(self, self.core.messaging.mark_read(chat)))
        add("mute", lambda: widgets.run(self, self.core.chats.set_flags(
            chat, muted_until=0 if chat.muted(int(time.time() * 1000)) else -1)))
        add("close", lambda: self._close(chat))
        widget.insert_action_group("row", group)
        menu = Gio.Menu()
        menu.append(_("Unpin") if chat.pinned else _("Pin"), "row.pin")
        if chat.unread:
            menu.append(_("Mark as read"), "row.read")
        menu.append(_("Unmute") if chat.muted(int(time.time() * 1000)) else _("Mute"), "row.mute")
        menu.append(_("Unarchive") if chat.archived else _("Archive"), "row.archive")
        menu.append(_("Leave group") if chat.type == "groupchat" else _("Close chat"), "row.close")
        pop = Gtk.PopoverMenu.new_from_model(menu)
        pop.set_parent(widget)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    def _close(self, chat: ChatItem) -> None:
        if self.window.current_chat is chat:
            self.window.show_chat(None)
        if chat.type == "groupchat":
            widgets.run(self, self.core.muc.leave(chat))
        else:
            widgets.run(self, self.core.chats.close(chat))

    def stripe_css(self, widget: Gtk.Widget, color: str) -> None:
        cls = "stripe-" + color.lstrip("#")
        if cls not in self._stripe_css:
            from gi.repository import Gdk

            p = Gtk.CssProvider()
            css = ".%s { background-color: %s; }" % (cls, color)
            try:
                p.load_from_string(css)
            except AttributeError:
                p.load_from_data(css, -1)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), p,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self._stripe_css[cls] = p
        for c in widget.get_css_classes():
            if c.startswith("stripe-"):
                widget.remove_css_class(c)
        widget.add_css_class(cls)

    def _update_density(self) -> None:
        if self.settings["appearance.list_density"] == "compact":
            self.list.add_css_class("compact")
        else:
            self.list.remove_css_class("compact")

    # ================================================================== contacts page
    def _build_contacts(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        top = Gtk.Box(spacing=6, margin_start=8, margin_end=8, margin_top=6, margin_bottom=4)
        self.contact_search = Gtk.SearchEntry(placeholder_text=_("Search contacts"), hexpand=True)
        self.contact_search.connect("search-changed", lambda *_: self.contact_filter.changed(
            Gtk.FilterChange.DIFFERENT))
        top.append(self.contact_search)
        add = Gtk.Button(icon_name="contact-new-symbolic", tooltip_text=_("Add contact"), action_name="win.add-contact")
        top.append(add)
        box.append(top)
        self.contact_filter = Gtk.CustomFilter.new(self._filter_contact)
        self.contact_sorter = Gtk.CustomSorter.new(self._sort_contacts)
        filtered = Gtk.FilterListModel(model=self.core.contacts.store, filter=self.contact_filter)
        sorted_ = Gtk.SortListModel(model=filtered, sorter=self.contact_sorter)
        self.contacts_model = sorted_
        sel = Gtk.NoSelection(model=sorted_)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, li: li.set_child(ContactRow(self)))
        factory.connect("bind", lambda _f, li: li.get_child().bind(li.get_item()))
        factory.connect("unbind", lambda _f, li: li.get_child().unbind())
        lv = Gtk.ListView(model=sel, factory=factory, single_click_activate=True,
                          css_classes=["navigation-sidebar", "chat-list"])
        lv.connect("activate", lambda _l, pos: self._open_contact(sorted_.get_item(pos)))
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(lv)
        stack = Gtk.Stack()
        stack.add_named(sc, "list")
        empty = Adw.StatusPage(icon_name="ward-contacts-symbolic", title=_("No contacts yet"),
                               description=_("Add people by their address, like alex@example.org"))
        empty.add_css_class("compact")
        btn = Gtk.Button(label=_("Add contact"), action_name="win.add-contact", halign=Gtk.Align.CENTER,
                         css_classes=["pill", "suggested-action"])
        empty.set_child(btn)
        stack.add_named(empty, "empty")
        sorted_.connect("items-changed", lambda m, *_: stack.set_visible_child_name("list" if m.get_n_items()
                                                                                   else "empty"))
        stack.set_visible_child_name("list" if sorted_.get_n_items() else "empty")
        box.append(stack)
        return box

    def _filter_contact(self, c: ContactItem) -> bool:
        if not c.in_roster:
            return False
        if self.account_filter and str(c.account_id) != self.account_filter:
            return False
        text = self.contact_search.get_text().strip().lower() if hasattr(self, "contact_search") else ""
        return not text or text in c.name.lower() or text in c.jid.lower()

    @staticmethod
    def _sort_contacts(a: ContactItem, b: ContactItem, *_x) -> int:
        oa, ob = a.show == "offline", b.show == "offline"
        if oa != ob:
            return 1 if oa else -1
        na, nb = a.name.lower(), b.name.lower()
        return (na > nb) - (na < nb)

    def _open_contact(self, c: ContactItem | None) -> None:
        if c is None:
            return
        acc = self.core.accounts.get(c.account_id)

        async def go():
            chat = await self.core.open_chat(acc, c.jid)
            self.stack.set_visible_child_name("chats")
            self.window.show_chat(chat)
        widgets.run(self, go())

    # ================================================================== accounts
    def _accounts_changed(self) -> None:
        for acc in self.core.accounts.list():
            if acc.id not in self._account_handlers:
                self._account_handlers[acc.id] = [acc.connect("notify::" + p, lambda *_: self._update_account())
                                                  for p in ("state", "show", "avatar", "error", "step", "status")]
        if self.account_filter and self.core.accounts.get(int(self.account_filter)) is None:
            self.account_filter = ""
        self._update_account()
        self._refilter()

    def _main_account(self) -> AccountItem | None:
        accounts = self.core.accounts.list()
        if self.account_filter:
            return self.core.accounts.get(int(self.account_filter))
        return accounts[0] if accounts else None

    def _subtitle(self) -> str:
        accounts = self.core.accounts.list()
        acc = self._main_account()
        if acc is None:
            return ""
        if self.account_filter or len(accounts) == 1:
            return acc.display_name
        return ngettext("%d account", "%d accounts", len(accounts)) % len(accounts)

    def _update_account(self) -> None:
        acc = self._main_account()
        if acc is None:
            self.account_avatar.update("?")
            self.title.set_subtitle("")
            self.status.set_reveal_child(False)
            return
        show = acc.show if acc.state == "online" else "offline"
        self.account_avatar.update(acc.display_name, acc.avatar, show)
        if not self.show_archived:
            self.title.set_subtitle(self._subtitle())
        # the most urgent problem of any account goes into the status strip
        self._status_acc = None
        for a in self.core.accounts.list():
            if not a.enabled:
                continue
            if a.state in ("error", "locked") or (a.state == "connecting" and a.step):
                self._status_acc = a
                if a.state != "connecting":
                    break
        a = self._status_acc
        if a is None:
            self.status.set_reveal_child(False)
            return
        prefix = (a.display_name + ": ") if len(self.core.accounts.list()) > 1 else ""
        if a.state == "locked":
            self.status_label.set_text(prefix + _("Password needed"))
            self.status_button.set_label(_("Enter"))
            self.status_button.set_visible(True)
        elif a.state == "error":
            self.status_label.set_text(prefix + (a.error or _("Not connected")))
            self.status_button.set_label(_("Retry"))
            self.status_button.set_visible(True)
        else:
            step = {"resolving": _("Looking up the server…"), "connecting": _("Connecting…"),
                    "tls": _("Securing the connection…"), "auth": _("Signing in…"),
                    "syncing": _("Syncing…")}.get(a.step, _("Connecting…"))
            self.status_label.set_text(prefix + step)
            self.status_button.set_visible(False)
        self.status.set_reveal_child(True)

    def _status_action(self) -> None:
        a = getattr(self, "_status_acc", None)
        if a is None:
            return
        if a.state == "locked" or a.error_kind in ("auth", "password-needed"):
            self.window.ask_password(a)
        elif a.error_kind == "certificate":
            s = self.core.sessions.get(a.id)
            info = s.cert_problem if s is not None else None
            if info is not None:
                self.window.show_certificate(a, info)
            else:
                widgets.run(self, self.core.connect_account(a))
        else:
            widgets.run(self, self.core.connect_account(a))

    def _fill_accounts(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=6, margin_end=6, margin_top=6,
                      margin_bottom=6)
        box.set_size_request(280, -1)
        accounts = self.core.accounts.list()
        lb = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        if len(accounts) > 1:
            all_row = Adw.ActionRow(title=_("All accounts"), activatable=True)
            if not self.account_filter:
                all_row.add_suffix(Gtk.Image.new_from_icon_name("object-select-symbolic"))
            all_row.connect("activated", lambda *_: self._filter_account(""))
            lb.append(all_row)
        for acc in accounts:
            row = Adw.ExpanderRow(title=widgets.escape(acc.display_name),
                                  subtitle=widgets.escape(self._account_state_text(acc)))
            av = widgets.Avatar(32, acc.display_name, acc.avatar, acc.show if acc.state == "online" else "offline")
            row.add_prefix(av)
            if len(accounts) > 1:
                only = Gtk.Button(icon_name="object-select-symbolic" if self.account_filter == str(acc.id)
                                  else "view-list-symbolic", css_classes=["flat"], valign=Gtk.Align.CENTER,
                                  tooltip_text=_("Show only this account's chats"))
                only.connect("clicked", lambda *_a, acc=acc: self._filter_account(str(acc.id)))
                row.add_suffix(only)
            for show, label, icon in SHOWS:
                r = Adw.ActionRow(title=_(label), activatable=True)
                r.add_prefix(Gtk.Image.new_from_icon_name(icon))
                if acc.show == show and (acc.state == "online" or show == "offline"):
                    r.add_suffix(Gtk.Image.new_from_icon_name("object-select-symbolic"))
                r.connect("activated", lambda *_a, acc=acc, show=show: self._set_presence(acc, show))
                row.add_row(r)
            status = Adw.EntryRow(title=_("Status message"), text=acc.status, show_apply_button=True)
            status.connect("apply", lambda e, acc=acc: self._set_presence(acc, acc.show if acc.show != "offline"
                                                                           else "available", e.get_text()))
            row.add_row(status)
            prof = Adw.ActionRow(title=_("Profile…"), activatable=True)
            prof.connect("activated", lambda *_a, acc=acc: (self.account_popover.popdown(),
                                                            self.window.show_profile(acc)))
            row.add_row(prof)
            row.set_expanded(len(accounts) == 1)
            lb.append(row)
        box.append(lb)
        add = Gtk.Button(label=_("Add account…"), css_classes=["flat"])
        add.connect("clicked", lambda *_: (self.account_popover.popdown(), self.window.show_welcome(True)))
        box.append(add)
        manage = Gtk.Button(label=_("Manage accounts…"), css_classes=["flat"])
        manage.connect("clicked", lambda *_: (self.account_popover.popdown(),
                                              self.window.app.show_preferences("accounts")))
        box.append(manage)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                max_content_height=520)
        sc.set_child(box)
        self.account_popover.set_child(sc)

    @staticmethod
    def _account_state_text(acc: AccountItem) -> str:
        if not acc.enabled:
            return _("Disabled")
        if acc.state == "online":
            return widgets.presence_text(acc.show, acc.status)
        if acc.state == "error":
            return acc.error or _("Not connected")
        if acc.state == "connecting":
            return _("Connecting…")
        if acc.state == "locked":
            return _("Password needed")
        return _("Offline")

    def _filter_account(self, value: str) -> None:
        self.account_popover.popdown()
        self.account_filter = value
        self.settings["ui.account_filter"] = value
        self._update_account()
        self._refilter()
        self.contact_filter.changed(Gtk.FilterChange.DIFFERENT)

    def _set_presence(self, acc: AccountItem, show: str, status: str | None = None) -> None:
        self.account_popover.popdown()
        widgets.run(self, self.core.set_presence(acc, show, status), error=_("Could not change your status"))

    # ================================================================== misc
    def _update_requests(self) -> None:
        n = len(self.core.requests.items())
        self.requests_button.set_visible(n > 0)
        self.requests_label.set_text(ngettext("%d request", "%d requests", n) % n)

    def _on_setting(self, _s, key: str) -> None:
        if key == "appearance.list_density":
            self._update_density()
        elif key.startswith("appearance.") or key == "chats.show_typing":
            self.sorter.changed(Gtk.SorterChange.DIFFERENT)

    def _tick(self) -> bool:
        # relative times ("Mon", "12:30") and mute expiry move on their own
        self.sorter.changed(Gtk.SorterChange.DIFFERENT)
        return True

    def focus_search(self) -> None:
        self.stack.set_visible_child_name("chats")
        self.search.grab_focus()

    def select_relative(self, delta: int) -> None:
        n = self.sorted.get_n_items()
        if not n:
            return
        cur = self.selection.get_selected()
        i = 0 if cur == Gtk.INVALID_LIST_POSITION else (cur + delta) % n
        self.window.show_chat(self.sorted.get_item(i))

    def select_index(self, i: int) -> None:
        if 0 <= i < self.sorted.get_n_items():
            self.window.show_chat(self.sorted.get_item(i))

