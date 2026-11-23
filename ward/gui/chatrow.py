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
              "muted-until", "avatar", "show", "is-request")


class ChatRow(Gtk.Box):
    __gtype_name__ = "WardChatRow"

    def __init__(self, sidebar):
        super().__init__(spacing=10, css_classes=["chat-row"])
        self.sidebar = sidebar
        self.chat: ChatItem | None = None
        self._handlers: list[int] = []
        self._reset_cache()
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

    def _reset_cache(self) -> None:
        self._last_title: str | None = None
        self._last_avatar: tuple | None = None
        self._last_time: tuple | None = None
        self._last_pinned: bool | None = None
        self._last_muted: bool | None = None
        self._last_preview: tuple | None = None
        self._last_badge: tuple | None = None
        self._last_unread: bool | None = None
        self._last_stripe: tuple | None = None

    def bind(self, chat: ChatItem) -> None:
        self.unbind()
        self.chat = chat
        rows = getattr(self.sidebar, "_chat_rows", None)
        if rows is not None:
            rows.add(self)
        for p in CHAT_PROPS:
            self._handlers.append(chat.connect("notify::" + p, self._update))
        self._update(force=True)

    def unbind(self) -> None:
        if self.chat is not None:
            for h in self._handlers:
                if GObject.signal_handler_is_connected(self.chat, h):
                    self.chat.disconnect(h)
        rows = getattr(self.sidebar, "_chat_rows", None)
        if rows is not None:
            rows.discard(self)
        self._handlers = []
        self.chat = None
        self._reset_cache()

    def refresh_time(self) -> None:
        if self.chat is not None:
            self._last_time = None
            self._last_muted = None
            self._update()

    def _update(self, *_a, force: bool = False) -> None:
        chat = self.chat
        if chat is None or not hasattr(self, "title"):
            return
        settings = self.sidebar.core.settings
        title = chat.name or chat.jid
        if force or title != self._last_title:
            self._last_title = title
            self.title.set_text(title)
        group = chat.type == "groupchat"
        av_key = (title, chat.avatar, None if group else chat.show)
        if force or av_key != self._last_avatar:
            self._last_avatar = av_key
            self.avatar.update(*av_key)
        time_key = (chat.last_activity, settings["appearance.time_format"], settings["appearance.time_seconds"])
        if force or time_key != self._last_time:
            self._last_time = time_key
            self.time.set_text(widgets.short_time(chat.last_activity, settings))
        pinned = bool(chat.pinned)
        if force or pinned != self._last_pinned:
            self._last_pinned = pinned
            self.pin_icon.set_visible(pinned)
        muted = chat.muted(int(time.time() * 1000))
        if force or muted != self._last_muted:
            self._last_muted = muted
            self.muted_icon.set_visible(muted)

        typing = chat.typing if settings["chats.show_typing"] else ""
        draft = chat.draft if (not typing and chat.draft and self.sidebar.window.current_chat is not chat) else ""
        if typing:
            prev_key: tuple = ("typing", group, typing)
        elif draft:
            prev_key = ("draft", draft.partition("\n")[0].rstrip("\r"))
        elif chat.is_request:
            prev_key = ("request",)
        elif settings["appearance.list_previews"]:
            prev_key = ("msg", group, chat.preview_sender, chat.preview)
        else:
            prev_key = ("empty",)
        if force or prev_key != self._last_preview:
            self._last_preview = prev_key
            for c in ("typing", "draft"):
                self.preview.remove_css_class(c)
            kind = prev_key[0]
            if kind == "typing":
                self.preview.set_text(_("%s is typing…") % typing if group else _("typing…"))
                self.preview.add_css_class("typing")
            elif kind == "draft":
                self.preview.set_markup("<span>%s</span> %s" % (widgets.escape(_("Draft:")),
                                                                widgets.escape(prev_key[1])))
                self.preview.add_css_class("draft")
            elif kind == "request":
                self.preview.set_text(_("Message request"))
            elif kind == "msg":
                sender, text = prev_key[2], prev_key[3]
                if sender == "me":
                    text = _("You: %s") % text
                elif group and sender:
                    text = "%s: %s" % (sender, text)
                self.preview.set_text(text)
            else:
                self.preview.set_text("")

        n = chat.unread
        badge_key = (n, chat.mentions, muted)
        if force or badge_key != self._last_badge:
            self._last_badge = badge_key
            show_badge = n > 0 or chat.mentions > 0
            self.badge.set_visible(show_badge)
            if show_badge:
                self.badge.set_text("@" if chat.mentions and not n else (str(n) if n < 1000 else "999+"))
            for c in ("muted", "mention"):
                self.badge.remove_css_class(c)
            if chat.mentions:
                self.badge.add_css_class("mention")
            elif muted:
                self.badge.add_css_class("muted")

        is_unread = bool(n or chat.mentions)
        if force or is_unread != self._last_unread:
            self._last_unread = is_unread
            if is_unread:
                self.add_css_class("unread")
            else:
                self.remove_css_class("unread")

        accounts = self.sidebar.core.accounts
        n_accounts = accounts.store.get_n_items() if hasattr(accounts, "store") else len(accounts.list())
        if n_accounts > 1 and not self.sidebar.account_filter:
            acc = accounts.get(chat.account_id)
            color = (acc.color if acc is not None and acc.color else colors.hex_color(acc.jid if acc else ""))
            tip = acc.display_name if acc is not None else None
            stripe_key: tuple = (True, color, tip)
        else:
            stripe_key = (False, "", None)
        if force or stripe_key != self._last_stripe:
            self._last_stripe = stripe_key
            if stripe_key[0]:
                self.stripe.set_visible(True)
                self.sidebar.stripe_css(self.stripe, stripe_key[1])
                self.set_tooltip_text(stripe_key[2])
            else:
                self.stripe.set_visible(False)
                self.set_tooltip_text(None)


class ContactRow(Gtk.Box):
    __gtype_name__ = "WardContactRow"

    def __init__(self, sidebar):
        super().__init__(spacing=10, css_classes=["chat-row"])
        self.sidebar = sidebar
        self.contact: ContactItem | None = None
        self._handlers: list[int] = []
        self._reset_cache()
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

    def _reset_cache(self) -> None:
        self._last_title: str | None = None
        self._last_avatar: tuple | None = None
        self._last_sub: str | None = None
        self._last_pending: bool | None = None

    def bind(self, c: ContactItem) -> None:
        self.unbind()
        self.contact = c
        rows = getattr(self.sidebar, "_contact_rows", None)
        if rows is not None:
            rows.add(self)
        for p in ("name", "show", "status", "avatar", "subscription", "ask", "blocked", "last-seen"):
            self._handlers.append(c.connect("notify::" + p, self._update))
        self._update(force=True)

    def unbind(self) -> None:
        if self.contact is not None:
            for h in self._handlers:
                if GObject.signal_handler_is_connected(self.contact, h):
                    self.contact.disconnect(h)
        rows = getattr(self.sidebar, "_contact_rows", None)
        if rows is not None:
            rows.discard(self)
        self._handlers = []
        self.contact = None
        self._reset_cache()

    def refresh_time(self) -> None:
        if self.contact is not None and self.contact.show == "offline" and self.contact.last_seen:
            self._last_sub = None
            self._update()

    def _update(self, *_a, force: bool = False) -> None:
        c = self.contact
        if c is None or not hasattr(self, "title"):
            return
        if force or c.name != self._last_title:
            self._last_title = c.name
            self.title.set_text(c.name)
        av_key = (c.name, c.avatar, c.show if c.we_see else None)
        if force or av_key != self._last_avatar:
            self._last_avatar = av_key
            self.avatar.update(*av_key)
        if c.blocked:
            sub = _("Blocked")
        elif c.show != "offline":
            sub = widgets.presence_text(c.show, c.status)
        elif c.last_seen:
            sub = _("Last seen %s") % widgets.relative_seen(c.last_seen)
        else:
            sub = c.jid
        if force or sub != self._last_sub:
            self._last_sub = sub
            self.subtitle.set_text(sub)
        pending = bool(c.ask == "subscribe" and not c.we_see)
        if force or pending != self._last_pending:
            self._last_pending = pending
            self.pending.set_visible(pending)


GObject.type_register(ChatRow)
GObject.type_register(ContactRow)
