"""The main window: sidebar and conversation in a split view that collapses to one pane on narrow screens, the
welcome flow when there is no account yet, and the dialogs that the core asks for (passwords, certificates)."""

from __future__ import annotations

import logging

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import APP_NAME
from ..core.models import AccountItem, ChatItem
from ..i18n import _
from . import compat, lifetime, widgets
from .conversation import ConversationView
from .sidebar import Sidebar

log = logging.getLogger("ward.gui")

SHORTCUTS = (
    (_("General"), (("<Ctrl>k", _("Find a chat or contact")), ("<Ctrl>n", _("New chat")),
                    ("<Ctrl><Shift>n", _("New group")), ("<Ctrl>j", _("Join a group")),
                    ("<Ctrl><Shift>a", _("Add a contact")), ("<Ctrl>comma", _("Preferences")),
                    ("<Ctrl>question F1", _("Keyboard shortcuts")), ("F10", _("Main menu")),
                    ("<Ctrl>w", _("Close the window (Ward keeps running in the background)")),
                    ("<Ctrl>q", _("Quit Ward, including the background service")))),
    (_("Chats"), (("<Alt>Down <Ctrl>Page_Down", _("Next chat")), ("<Alt>Up <Ctrl>Page_Up", _("Previous chat")),
                  ("<Ctrl>Tab", _("Next chat with unread messages")),
                  ("<Ctrl><Shift>Tab", _("Previous chat with unread messages")),
                  ("<Alt>1...9", _("Go to chat 1 to 9")), ("<Ctrl>f", _("Search in the chat")),
                  ("<Ctrl><Shift>f", _("Search chats")), ("<Ctrl><Shift>i", _("Chat details")),
                  ("<Ctrl><Shift>r", _("Mark as read")), ("<Ctrl><Shift>m", _("Mute or unmute")),
                  ("<Ctrl><Shift>p", _("Pin or unpin")), ("Escape", _("Back to the chat list (on small screens)")))),
    (_("Messages"), (("<Ctrl>r", _("Reply to the last message")), ("<Ctrl>e Up", _("Edit your last message")),
                     ("Page_Up Page_Down", _("Scroll the messages")), ("<Ctrl>End", _("Jump to the latest message")),
                     ("<Ctrl>l", _("Write a message")))),
    (_("Writing"), (("Return", _("Send")), ("<Shift>Return", _("New line")),
                    ("Escape", _("Cancel reply or edit")), ("<Ctrl>b", _("Bold")), ("<Ctrl>i", _("Italic")),
                    ("<Ctrl>u <Ctrl>o", _("Send a file")), ("<Ctrl>z", _("Undo")), ("<Ctrl><Shift>z", _("Redo")),
                    ("Tab", _("Complete a nickname")))),
    (_("Text size"), (("<Ctrl>plus", _("Larger")), ("<Ctrl>minus", _("Smaller")), ("<Ctrl>0", _("Normal")))),
)


def _shortcuts_window() -> Gtk.Window:
    xml = ['<interface><object class="GtkShortcutsWindow" id="w"><property name="modal">1</property>'
           '<child><object class="GtkShortcutsSection"><property name="section-name">main</property>']
    for title, items in SHORTCUTS:
        xml.append('<child><object class="GtkShortcutsGroup"><property name="title">%s</property>'
                   % GLib.markup_escape_text(title))
        for accel, label in items:
            xml.append('<child><object class="GtkShortcutsShortcut"><property name="accelerator">%s</property>'
                       '<property name="title">%s</property></object></child>'
                       % (GLib.markup_escape_text(accel), GLib.markup_escape_text(label)))
        xml.append('</object></child>')
    xml.append('</object></child></object></interface>')
    b = Gtk.Builder.new_from_string("".join(xml), -1)
    return b.get_object("w")


class MainWindow(Adw.ApplicationWindow):
    __gtype_name__ = "WardWindow"

    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.core = app.core
        self.settings = self.core.settings
        self.current_chat: ChatItem | None = None
        self.narrow = False
        self._asked_password: set[int] = set()

        self.set_default_size(self.settings["window.width"], self.settings["window.height"])
        if self.settings["window.maximized"]:
            self.maximize()
        self.set_size_request(360, 294)

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        self.top = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.toasts.set_child(self.top)

        loading = Adw.StatusPage(title=APP_NAME, icon_name="io.github.smiley_mcsmiles.Ward")
        loading.set_child(compat.spinner())
        self.top.add_named(loading, "loading")

        self.split = Adw.NavigationSplitView(min_sidebar_width=280, max_sidebar_width=380, sidebar_width_fraction=0.3)
        self.sidebar = Sidebar(self)
        self.split.set_sidebar(Adw.NavigationPage(title=_("Chats"), child=self.sidebar, tag="sidebar"))
        self.content_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        empty_view = Adw.ToolbarView()
        empty_view.add_top_bar(Adw.HeaderBar(show_title=False))
        self.empty = Adw.StatusPage(icon_name="ward-chat-symbolic", title=_("No chat selected"),
                                    description=_("Pick a chat on the left, or start a new one"))
        start = Gtk.Button(label=_("New chat"), action_name="win.start-chat", halign=Gtk.Align.CENTER,
                           css_classes=["pill"])
        self.empty.set_child(start)
        empty_view.set_content(self.empty)
        self.content_stack.add_named(empty_view, "empty")
        self.conversation = ConversationView(self)
        self.content_stack.add_named(self.conversation, "chat")
        self.content_page = Adw.NavigationPage(title=_("Chat"), child=self.content_stack, tag="content")
        self.split.set_content(self.content_page)
        self.split.connect("notify::show-content", self._on_show_content)
        self.top.add_named(self.split, "main")
        self.welcome = None

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        bp.add_setter(self.split, "collapsed", True)
        bp.connect("apply", lambda *_: self._set_narrow(True))
        bp.connect("unapply", lambda *_: self._set_narrow(False))
        self.add_breakpoint(bp)

        self._install_actions()
        self.connect("close-request", self._on_close)
        self.connect("notify::is-active", self._on_focus)
        self.connect("notify::visible", self._on_focus)
        lifetime.watch(self.core.accounts.store, "items-changed", lambda *_: self._accounts_changed())
        lifetime.watch(self.core, "cert-problem", lambda _c, acc, info: self.show_certificate(acc, info))
        lifetime.watch(self.core.muc, "invite", lambda _m, acc, inv: self._on_invite(acc, inv))
        lifetime.watch(self.core, "session-added",
                       lambda _c, s: lifetime.watch(s.account, "notify::state", self._on_account_state))
        for s in self.core.sessions.values():
            lifetime.watch(s.account, "notify::state", self._on_account_state)
        if self.core.omemo is not None:
            lifetime.watch(self.core.omemo, "new-devices", self._on_new_devices)
        if self.core.started:
            self.ready()

    # ================================================================== startup
    def ready(self) -> None:
        """The core has loaded the database."""
        self._accounts_changed()
        last = self.settings["ui.last_chat"]
        if last and self.core.accounts.list():
            try:
                chat = self.core.chats.by_id.get(int(last))
            except ValueError:
                chat = None
            if chat is not None and chat.open and not self.narrow:
                self.show_chat(chat)

    def _accounts_changed(self) -> None:
        if not self.core.started:
            return
        if self.core.accounts.list():
            self.top.set_visible_child_name("main")
            if self.welcome is not None and self.welcome.get_parent() is self.top:
                self.top.remove(self.welcome)
                lifetime.dismantle_later(self.welcome)  # it refers to this window: it must not keep it
                self.welcome = None
        else:
            self.show_chat(None)
            self.show_welcome()

    def show_welcome(self, adding: bool = False):
        from .welcome import WelcomePage

        if adding:
            d = compat.Dialog(_("Add account"), 460, 640)
            page = WelcomePage(self, dialog=d)
            d.set_child(page)
            d.present(self)
            return page
        if self.welcome is None:
            self.welcome = WelcomePage(self)
            self.top.add_named(self.welcome, "welcome")
        self.top.set_visible_child_name("welcome")
        return self.welcome

    # ================================================================== chats
    def show_chat(self, chat: ChatItem | None, force: bool = False) -> None:
        if chat is self.current_chat and not force:
            if chat is not None:
                self.split.set_show_content(True)
            return
        if chat is not None and not chat.open:
            widgets.run(self, self.core.chats.set_flags(chat, open=True))
        self.current_chat = chat
        self.sidebar.select(chat)
        if chat is None:
            self.conversation.show_chat(None)
            self.content_stack.set_visible_child_name("empty")
            self.content_page.set_title(_("Chat"))
            self.core.set_active_chat(None)
            if self.narrow:
                self.split.set_show_content(False)
            return
        # the conversation sets up its members panel before it becomes visible (libadwaita 1.5 mis-sizes a split
        # view that starts showing its sidebar while it is being mapped)
        self.conversation.show_chat(chat)
        self.content_stack.set_visible_child_name("chat")
        self.content_page.set_title(chat.name or chat.jid)
        self.split.set_show_content(True)
        self.settings["ui.last_chat"] = str(chat.id)
        self.sidebar._refilter()

    def _on_show_content(self, *_a) -> None:
        if self.narrow and not self.split.get_show_content():
            # back to the list on a phone: the chat is no longer being read
            self.core.set_active_chat(None)
            self.current_chat = None
            self.conversation.show_chat(None)
            self.sidebar.select(None)

    def _set_narrow(self, narrow: bool) -> None:
        self.narrow = narrow
        self.conversation.set_narrow(narrow)

    def open_chat_id(self, chat_id: int) -> None:
        chat = self.core.chats.by_id.get(chat_id)
        if chat is not None:
            self.show_chat(chat)
            self.present()

    # ================================================================== actions
    def _install_actions(self) -> None:
        from . import dialogs

        def add(name: str, cb, accels: list[str] | None = None):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_: cb())
            self.add_action(a)
            if accels:
                self.app.set_accels_for_action("win." + name, accels)
        add("start-chat", lambda: self._need_account() and dialogs.StartChatDialog(self).present(self), ["<Ctrl>n"])
        add("new-group", lambda: self._need_account() and dialogs.NewGroupDialog(self).present(self),
            ["<Ctrl><Shift>n"])
        add("join-group", lambda: self._need_account() and dialogs.JoinGroupDialog(self).present(self), ["<Ctrl>j"])
        add("add-contact", lambda: self._need_account() and dialogs.AddContactDialog(self).present(self),
            ["<Ctrl><Shift>a"])
        add("quick-switcher", lambda: dialogs.QuickSwitcher(self).present(self), ["<Ctrl>k"])
        add("search-chats", self.sidebar.focus_search, ["<Ctrl><Shift>f"])
        add("next-chat", lambda: self.sidebar.select_relative(1), ["<Alt>Down", "<Ctrl>Page_Down"])
        add("prev-chat", lambda: self.sidebar.select_relative(-1), ["<Alt>Up", "<Ctrl>Page_Up"])
        add("next-unread", lambda: self.sidebar.select_unread(1), ["<Ctrl>Tab"])
        add("prev-unread", lambda: self.sidebar.select_unread(-1), ["<Ctrl><Shift>Tab", "<Ctrl><Shift>ISO_Left_Tab"])
        add("chat-details", self.conversation.show_details, ["<Ctrl><Shift>i"])
        add("mark-read", lambda: self.current_chat is not None and widgets.run(
            self, self.core.messaging.mark_read(self.current_chat)), ["<Ctrl><Shift>r"])
        add("toggle-mute", lambda: self.current_chat is not None and self.conversation._mute(
            0 if self.current_chat.muted(GLib.get_real_time() // 1000) else -1), ["<Ctrl><Shift>m"])
        add("toggle-pin", lambda: self.current_chat is not None and self.conversation._toggle_flag("pinned"),
            ["<Ctrl><Shift>p"])
        add("reply-last", lambda: self.current_chat is not None and self.conversation.reply_last(), ["<Ctrl>r"])
        add("edit-last", lambda: self.current_chat is not None and self.conversation.composer.edit_last(),
            ["<Ctrl>e"])
        add("focus-composer", lambda: self.current_chat is not None and self.conversation.focus_composer(),
            ["<Ctrl>l"])
        add("jump-latest", lambda: self.current_chat is not None and self.conversation.scroll_to_bottom(),
            ["<Ctrl>End"])
        # Escape last of all (bubble phase): dialogs, popovers and the composer get it first
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, kv, _code, st: kv == Gdk.KEY_Escape and not (
            st & Gtk.accelerator_get_default_mod_mask()) and self._back())
        self.add_controller(keys)
        self._keys = keys
        add("search-chat", lambda: self.current_chat is not None and self.conversation.search_button.set_active(True),
            ["<Ctrl>f"])
        add("send-file", lambda: self.current_chat is not None and self.conversation.composer.choose_files(),
            ["<Ctrl>u", "<Ctrl>o"])
        add("close", self.close, ["<Ctrl>w"])
        add("zoom-in", lambda: self._zoom(10), ["<Ctrl>plus", "<Ctrl>equal", "<Ctrl>KP_Add"])
        add("zoom-out", lambda: self._zoom(-10), ["<Ctrl>minus", "<Ctrl>KP_Subtract"])
        add("zoom-reset", lambda: self._zoom(0), ["<Ctrl>0", "<Ctrl>KP_0"])
        add("admin", self._admin)
        add("xml-console", self._xml_console)
        add("disco", self._disco)
        for i in range(1, 10):
            add("chat-%d" % i, lambda i=i: self.sidebar.select_index(i - 1), ["<Alt>%d" % i])
        # built when asked for, not with the window (set_help_overlay would keep one around all the time)
        add("show-help-overlay", self._show_shortcuts, ["<Ctrl>question", "F1"])

    def _show_shortcuts(self) -> None:
        w = _shortcuts_window()
        w.set_transient_for(self)
        w.present()

    def _back(self) -> bool:
        """Escape: close the in-chat search, or go back to the chat list on a narrow window."""
        conv = self.conversation
        if conv.search_button.get_active():
            conv.search_button.set_active(False)
            return True
        if self.narrow and self.split.get_show_content():
            self.split.set_show_content(False)
            return True
        return False

    def _need_account(self) -> bool:
        if not self.core.accounts.list():
            self.show_welcome()
            return False
        return True

    def _zoom(self, delta: int) -> None:
        cur = self.settings["appearance.text_scale"]
        self.settings["appearance.text_scale"] = 100 if delta == 0 else max(80, min(200, cur + delta))

    def _pick_account(self, cb) -> None:
        accounts = [a for a in self.core.accounts.list() if a.enabled]
        if not accounts:
            compat.toast(self, _("No account is enabled"))
            return
        if len(accounts) == 1:
            cb(accounts[0])
            return
        from .dialogs import AccountPicker

        AccountPicker(self, cb).present(self)

    def _admin(self) -> None:
        from .admin import AdminDialog

        self._pick_account(lambda acc: AdminDialog(self, acc).present(self))

    def _xml_console(self) -> None:
        if not self.settings["advanced.devtools"]:
            compat.alert(self, _("Turn on developer tools?"),
                         _("The XML console shows the raw protocol, including message contents."),
                         [("cancel", _("Cancel"), ""), ("on", _("Turn on"), "suggested")],
                         lambda r: r == "on" and (self.settings.__setitem__("advanced.devtools", True),
                                                  self._xml_console()))
            return
        from .tools import XmlConsole

        XmlConsole(self).present()

    def _disco(self) -> None:
        from .tools import DiscoBrowser

        self._pick_account(lambda acc: DiscoBrowser(self, acc).present(self))

    # ================================================================== window state
    def _on_focus(self, *_a) -> None:
        self.core.set_window_state(self.is_active(), self.get_visible())
        if self.is_active() and self.current_chat is not None:
            self.app.withdraw_chat_notification(self.current_chat)

    def teardown(self) -> None:
        """The window is being let go (Ward runs on in the background): break every link that would keep it."""
        self.conversation.show_chat(None)       # unsubscribes from the open chat (a core object that outlives us)
        popover = self.conversation.composer.popover       # see Composer: GtkTextView cannot remove it itself
        if popover.get_parent() is not None:
            popover.unparent()
        lifetime.disconnect_scope(getattr(self, "built_handlers", []))
        self.sidebar.drop_callbacks()
        for name in self.list_actions():
            self.remove_action(name)
        self.remove_controller(self._keys)
        if self.welcome is not None:
            lifetime.dismantle(self.welcome)
        lifetime.dismantle(self)

    def _on_close(self, *_a) -> bool:
        w, h = self.get_default_size()
        if not self.is_maximized() and w > 0 and h > 0:
            self.settings["window.width"] = w
            self.settings["window.height"] = h
        self.settings["window.maximized"] = self.is_maximized()
        self.conversation.composer.save_draft()
        self.settings.flush()
        if self.settings["general.background"]:
            self.set_visible(False)
            self.core.set_window_state(False, False)
            if self.settings["advanced.free_window"]:
                self.app.release_window_later(self)
            return True
        self.app.quit_app()
        return True

    # ================================================================== core requests
    def _on_account_state(self, acc: AccountItem, _p) -> None:
        if acc.state == "error" and acc.error_kind == "password-needed" and acc.id not in self._asked_password:
            self._asked_password.add(acc.id)
            GLib.idle_add(lambda: self.ask_password(acc) and False)
        elif acc.state == "error" and acc.error_kind == "auth":
            compat.toast(self, _("%s: the password was not accepted") % acc.jid)

    def ask_password(self, acc: AccountItem) -> None:
        from .dialogs import PasswordDialog

        PasswordDialog(self, acc).present(self)

    def show_certificate(self, acc: AccountItem, info) -> None:
        from .dialogs import CertificateDialog

        CertificateDialog(self, acc, info).present(self)

    def show_requests(self) -> None:
        from .dialogs import RequestsDialog

        RequestsDialog(self).present(self)

    def show_profile(self, acc: AccountItem) -> None:
        from .dialogs import ProfileDialog

        ProfileDialog(self, acc).present(self)

    def _on_invite(self, acc: AccountItem, inv: dict) -> None:
        from .dialogs import InvitationDialog

        InvitationDialog(self, acc, inv).present(self)

    def _on_new_devices(self, _omemo, chat, jid: str, devices) -> None:
        if self.settings["encryption.new_device_notice"] == "notify":
            name = (chat.name if chat is not None else "") or jid
            self.app.notify_simple("new-devices-%s" % jid, _("New device"), _("%s added a new device") % name)

    def toast(self, text: str, button: str = "", action=None, timeout: int = 4) -> None:
        t = Adw.Toast(title=text, timeout=timeout)
        if button and action is not None:
            t.set_button_label(button)
            t.connect("button-clicked", lambda *_: action())
        self.toasts.add_toast(t)
