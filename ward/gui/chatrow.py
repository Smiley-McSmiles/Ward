"""Rows of the chat list and the contacts list.  Both are recycled by their list views: bind() follows one item's
properties until unbind()."""

from __future__ import annotations

import time

from gi.repository import GObject, Gtk, Pango

from ..core import colors
from ..core.models import ChatItem, ContactItem
from ..i18n import _
from . import widgets

CHAT_PROPS = ("name", "preview", "preview-sender", "unread", "mentions", "last-activity", "typing", "draft", "pinned",
              "muted-until", "avatar", "show", "is-request", "joined", "encryption-state")


class ChatRow(Gtk.Box):
    __gtype_name__ = "WardChatRow"

    def __init__(self, sidebar):
        super().__init__(spacing=10, css_classes=["chat-row"])
        self.sidebar = sidebar
        self.chat: ChatItem | None = None
        self._handlers: list[int] = []
        self.stripe = Gtk.Box(css_classes=["account-stripe"], visible=False)
        self.append(self.stripe)
        self.avatar = widgets.Avatar(42)
        self.avatar.set_valign(Gtk.Align.CENTER)
        self.append(self.avatar)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER, spacing=2)
        top = Gtk.Box(spacing=4)
        self.title = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                               css_classes=["chat-row-title"])
        top.append(self.title)
        self.pin_icon = Gtk.Image.new_from_icon_name("ward-pin-symbolic")
        self.pin_icon.add_css_class("dim-label")
        top.append(self.pin_icon)
        self.muted_icon = Gtk.Image.new_from_icon_name("ward-muted-symbolic")
        self.muted_icon.add_css_class("dim-label")
        top.append(self.muted_icon)
        self.time = Gtk.Label(css_classes=["chat-row-time"])
        top.append(self.time)
        col.append(top)
        bottom = Gtk.Box(spacing=4)
        self.preview = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                                 css_classes=["chat-row-preview"], single_line_mode=True)
        bottom.append(self.preview)
        self.badge = Gtk.Label(css_classes=["unread-badge"], valign=Gtk.Align.CENTER)
        bottom.append(self.badge)
        col.append(bottom)
        self.append(col)

    def bind(self, chat: ChatItem) -> None:
        self.unbind()
        self.chat = chat
        for p in CHAT_PROPS:
            self._handlers.append(chat.connect("notify::" + p, self._update))
        self._update()

    def unbind(self) -> None:
        if self.chat is not None:
            for h in self._handlers:
                self.chat.disconnect(h)
        self._handlers = []
        self.chat = None

    def _update(self, *_a) -> None:
        chat = self.chat
        if chat is None:
            return
        settings = self.sidebar.core.settings
        self.title.set_text(chat.name or chat.jid)
        group = chat.type == "groupchat"
        self.avatar.update(chat.name or chat.jid, chat.avatar, None if group else chat.show)
        self.time.set_text(widgets.short_time(chat.last_activity, settings))
        self.pin_icon.set_visible(chat.pinned)
        muted = chat.muted(int(time.time() * 1000))
        self.muted_icon.set_visible(muted)
        for c in ("typing", "draft"):
            self.preview.remove_css_class(c)
        if chat.typing and settings["chats.show_typing"]:
            self.preview.set_text(_("%s is typing…") % chat.typing if group else _("typing…"))
            self.preview.add_css_class("typing")
        elif chat.draft and self.sidebar.window.current_chat is not chat:
            self.preview.set_markup("<span>%s</span> %s" % (widgets.escape(_("Draft:")),
                                                            widgets.escape(chat.draft.splitlines()[0])))
            self.preview.add_css_class("draft")
        elif chat.is_request:
            self.preview.set_text(_("Message request"))
        elif settings["appearance.list_previews"]:
            sender = chat.preview_sender
            text = chat.preview
            if sender == "me":
                text = _("You: %s") % text
            elif group and sender:
                text = "%s: %s" % (sender, text)
            self.preview.set_text(text)
        else:
            self.preview.set_text("")
        n = chat.unread
        self.badge.set_visible(n > 0 or chat.mentions > 0)
        self.badge.set_text("@" if chat.mentions and not n else (str(n) if n < 1000 else "999+"))
        for c in ("muted", "mention"):
            self.badge.remove_css_class(c)
        if chat.mentions:
            self.badge.add_css_class("mention")
        elif muted:
            self.badge.add_css_class("muted")
        if n or chat.mentions:
            self.add_css_class("unread")
        else:
            self.remove_css_class("unread")
        accounts = self.sidebar.core.accounts.list()
        if len(accounts) > 1 and not self.sidebar.account_filter:
            acc = self.sidebar.core.accounts.get(chat.account_id)
            color = (acc.color if acc is not None and acc.color else colors.hex_color(acc.jid if acc else ""))
            self.stripe.set_visible(True)
            self.sidebar.stripe_css(self.stripe, color)
            self.set_tooltip_text(acc.display_name if acc is not None else None)
        else:
            self.stripe.set_visible(False)


class ContactRow(Gtk.Box):
    __gtype_name__ = "WardContactRow"

    def __init__(self, sidebar):
        super().__init__(spacing=10, css_classes=["chat-row"])
        self.sidebar = sidebar
        self.contact: ContactItem | None = None
        self._handlers: list[int] = []
        self.avatar = widgets.Avatar(36)
        self.avatar.set_valign(Gtk.Align.CENTER)
        self.append(self.avatar)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["chat-row-title"])
        self.subtitle = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["chat-row-preview"])
        col.append(self.title)
        col.append(self.subtitle)
        self.append(col)
        self.pending = Gtk.Image.new_from_icon_name("ward-clock-symbolic")
        self.pending.set_tooltip_text(_("Waiting for them to accept"))
        self.pending.add_css_class("dim-label")
        self.append(self.pending)

    def bind(self, c: ContactItem) -> None:
        self.unbind()
        self.contact = c
        for p in ("name", "show", "status", "avatar", "subscription", "ask", "blocked", "last-seen"):
            self._handlers.append(c.connect("notify::" + p, self._update))
        self._update()

    def unbind(self) -> None:
        if self.contact is not None:
            for h in self._handlers:
                self.contact.disconnect(h)
        self._handlers = []
        self.contact = None

    def _update(self, *_a) -> None:
        c = self.contact
        if c is None:
            return
        self.title.set_text(c.name)
        self.avatar.update(c.name, c.avatar, c.show if c.we_see else None)
        if c.blocked:
            sub = _("Blocked")
        elif c.show != "offline":
            sub = widgets.presence_text(c.show, c.status)
        elif c.last_seen:
            sub = _("Last seen %s") % widgets.relative_seen(c.last_seen)
        else:
            sub = c.jid
        self.subtitle.set_text(sub)
        self.pending.set_visible(c.ask == "subscribe" and not c.we_see)


GObject.type_register(ChatRow)
GObject.type_register(ContactRow)
