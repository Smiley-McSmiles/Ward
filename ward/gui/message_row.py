"""One message in the conversation.  Rows are recycled by the list view: bind() fills a row for a message (and
the one before it, for grouping and date separators), unbind() lets it go.  While bound, the row follows the
message's properties (edits, receipts, reactions, downloads) through notify signals."""

from __future__ import annotations

import os
import weakref

from gi.repository import Gio, GLib, GObject, Gtk, Pango

from ..core import colors
from ..core.markup.emoji import emoji_only
from ..core.models import (
    STATUS_DELIVERED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_READ,
    STATUS_SENT,
    AttachmentItem,
    MessageItem,
)
from ..i18n import _
from . import render, style, video, widgets

GROUP_WINDOW = 5 * 60 * 1000
STATUS_ICON = {STATUS_PENDING: ("ward-clock-symbolic", "Sending"), STATUS_SENT: ("ward-check-symbolic", "Sent"),
               STATUS_DELIVERED: ("ward-check-double-symbolic", "Delivered"),
               STATUS_READ: ("ward-check-double-symbolic", "Read"), STATUS_FAILED: ("ward-error-symbolic", "Failed")}
ENC_NAMES = {"omemo2": "OMEMO 2", "omemo": "OMEMO", "pgp": "OpenPGP"}
_MIME_ICONS: dict[str, Gio.Icon | None] = {}


def _mime_gicon(mime: str) -> Gio.Icon | None:
    if mime in _MIME_ICONS:
        return _MIME_ICONS[mime]
    ctype = Gio.content_type_from_mime_type(mime or "") or ""
    icon = Gio.content_type_get_symbolic_icon(ctype) if ctype else None
    if len(_MIME_ICONS) > 256:
        _MIME_ICONS.clear()
    _MIME_ICONS[mime] = icon
    return icon


def continues(prev: MessageItem | None, m: MessageItem) -> bool:
    if prev is None or prev.kind in ("status", "system", "call") or m.kind in ("status", "system", "call"):
        return False
    return (prev.direction == m.direction and prev.sender == m.sender and
            0 <= m.timestamp - prev.timestamp < GROUP_WINDOW and widgets.same_day(prev.timestamp, m.timestamp)
            and not m.reply_to)


class MessageRow(Gtk.Box):
    __gtype_name__ = "WardMessageRow"

    def __init__(self, view):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.view = view
        self.msg: MessageItem | None = None
        self._handlers: list[int] = []
        self._att_handlers: list[tuple[AttachmentItem, int]] = []
        self._rebuild_queued = False
        self._rebuild_idle = 0
        self._reveal_idle = 0
        self._dirty_full = False
        self._dirty_meta = False
        self._dirty_reactions = False
        self._dirty_reply = False
        self._layout = ""
        self._col: Gtk.Box | None = None
        self._meta_container: Gtk.Box | None = None
        self._meta_anchor: Gtk.Widget | None = None
        self._bubbles_meta: Gtk.Box | None = None
        self._reactions_widget: Gtk.Widget | None = None
        self._failed_widget: Gtk.Widget | None = None
        self._reply_author_lbl: Gtk.Label | None = None
        self._reply_text_lbl: Gtk.Label | None = None
        self._showing_failed = False
        self._showing_edited = False
        self._hover_quick: tuple[str, ...] | None = None
        self._react_btn: Gtk.MenuButton | None = None
        # the content is wrapped so a newly sent or received message can slide up into its place
        self.reveal = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.NONE, transition_duration=200,
                                   overflow=Gtk.Overflow.HIDDEN)
        self.reveal.connect("notify::child-revealed", self._on_revealed)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.reveal.set_child(self.content)
        self.reveal.set_reveal_child(True)
        self.append(self.reveal)
        self.separator = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.content.append(self.separator)
        self.overlay = Gtk.Overlay()
        self.content.append(self.overlay)
        self.row = Gtk.Box(spacing=10, css_classes=["message-row"])
        self.overlay.set_child(self.row)
        # quick reactions and reply: a floating pill over the top-right corner, as in Discord, so showing it
        # never takes room from the message or moves the rows below
        # Keep the pill visible and fade it instead of toggling visibility: toggling a descendant's visibility
        # makes GtkListView reset the scroll position.  It is not measured either, so it takes no row space.
        self.actions = Gtk.Box(spacing=0, css_classes=["hover-bar"], opacity=0, sensitive=False,
                               halign=Gtk.Align.END, valign=Gtk.Align.START, margin_top=2, margin_end=12)
        self.overlay.add_overlay(self.actions)
        self.overlay.set_measure_overlay(self.actions, False)
        self.hover = None
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self._hover(True))
        motion.connect("leave", lambda *_: self._hover(False))
        self.add_controller(motion)
        click = Gtk.GestureClick(button=3)
        click.connect("pressed", self._on_right_click)
        self.add_controller(click)
        press = Gtk.GestureLongPress()
        press.connect("pressed", lambda g, x, y: self.view.message_menu(self, self.msg, x, y))
        self.add_controller(press)
        self.time_hover: Gtk.Label | None = None

    # ------------------------------------------------------------------ binding
    def bind(self, msg: MessageItem, prev: MessageItem | None) -> None:
        self.unbind()
        # rows are recycled: always start visible and without a transition from the previous message
        self.reveal.set_transition_type(Gtk.RevealerTransitionType.NONE)
        self.reveal.set_reveal_child(True)
        self.msg = msg
        for prop in ("body", "edited", "retracted", "status", "error", "reactions-rev", "attachments-rev",
                     "trust", "selected", "reply-preview"):
            self._handlers.append(msg.connect("notify::" + prop, self._changed))
        self._build(prev)

    def _reset_dirty(self) -> None:
        self._dirty_full = False
        self._dirty_meta = False
        self._dirty_reactions = False
        self._dirty_reply = False

    def _clear_build_refs(self) -> None:
        self.hover = None
        self.react_button = None
        self.time_hover = None
        self._col = None
        self._meta_container = None
        self._meta_anchor = None
        self._bubbles_meta = None
        self._reactions_widget = None
        self._failed_widget = None
        self._reply_author_lbl = None
        self._reply_text_lbl = None
        self._showing_failed = False
        self._showing_edited = False

    def unbind(self) -> None:
        if self.msg is not None:
            for h in self._handlers:
                if GObject.signal_handler_is_connected(self.msg, h):
                    self.msg.disconnect(h)
        for att, h in self._att_handlers:
            if GObject.signal_handler_is_connected(att, h):
                att.disconnect(h)
        self._handlers = []
        self._att_handlers = []
        self._expanded = False
        self._rebuild_queued = False
        self._reset_dirty()
        self._clear_build_refs()
        if self._rebuild_idle:
            GLib.source_remove(self._rebuild_idle)
            self._rebuild_idle = 0
        if self._reveal_idle:
            GLib.source_remove(self._reveal_idle)
            self._reveal_idle = 0
        self.msg = None

    def update_selection(self) -> None:
        m = self.msg
        if m is None or not hasattr(self, "row") or m.kind in ("status", "call"):
            return
        if m.selected:
            if not self.row.has_css_class("selected"):
                self.row.add_css_class("selected")
        elif self.row.has_css_class("selected"):
            self.row.remove_css_class("selected")

    def update_highlight(self) -> None:
        m = self.msg
        if m is None or not hasattr(self, "row") or m.kind in ("status", "call"):
            return
        if self.view.highlight_id == m.id:
            if not self.row.has_css_class("highlight"):
                self.row.add_css_class("highlight")
        elif self.row.has_css_class("highlight"):
            self.row.remove_css_class("highlight")

    def _can_update_meta_in_place(self) -> bool:
        m = self.msg
        if m is None or m.kind in ("status", "call") or m.retracted or self._layout == "compact":
            return False
        failed_now = bool(m.status == STATUS_FAILED and m.outgoing)
        return not failed_now and not self._showing_failed

    def _can_update_reactions_in_place(self) -> bool:
        m = self.msg
        return (m is not None and self._col is not None and m.kind not in ("status", "call") and
                not m.retracted and self._layout != "compact")

    def _can_update_reply_in_place(self) -> bool:
        m = self.msg
        return (m is not None and m.kind not in ("status", "call") and m.retracted == 0 and
                self._layout != "compact" and bool(m.reply_to) and
                self._reply_author_lbl is not None and self._reply_text_lbl is not None)

    def _update_meta_in_place(self) -> None:
        m = self.msg
        if m is None or self._meta_container is None:
            return
        now_edited = bool(m.edited)
        if now_edited != self._showing_edited:
            self.view._layout_changing()
            self._showing_edited = now_edited
        anchor = self._meta_anchor
        child = anchor.get_next_sibling() if anchor is not None else self._meta_container.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._meta_container.remove(child)
            child = nxt
        self._meta_icons(self._meta_container, m)

    def _update_reactions_in_place(self) -> None:
        m = self.msg
        if m is None or self._col is None:
            return
        self.view._layout_changing()
        if self._reactions_widget is not None:
            self._col.remove(self._reactions_widget)
            self._reactions_widget = None
        if m.reactions:
            w = self._reactions(m)
            self._reactions_widget = w
            tail = self._failed_widget or self._bubbles_meta
            if tail is not None:
                self._col.insert_child_after(w, tail.get_prev_sibling())
            else:
                self._col.append(w)

    def _update_reply_in_place(self) -> None:
        m = self.msg
        if m is None or self._reply_author_lbl is None or self._reply_text_lbl is None:
            return
        self._reply_author_lbl.set_text(m.reply_author or _("Reply"))
        self._reply_text_lbl.set_text(m.reply_preview or "…")

    def _changed(self, *args) -> None:
        # Coalesce bursts per frame and update minor property changes in place without rebuilding the row.
        if self.msg is None:
            return
        prop = args[1].name if len(args) >= 2 and args[1] is not None else ""
        if prop == "selected":
            self.update_selection()
            return
        if prop in ("status", "trust", "edited", "error") and self._can_update_meta_in_place():
            self._dirty_meta = True
        elif prop == "reactions-rev" and self._can_update_reactions_in_place():
            self._dirty_reactions = True
        elif prop == "reply-preview" and self._can_update_reply_in_place():
            self._dirty_reply = True
        else:
            self._dirty_full = True
        self._rebuild_queued = True
        if not self._rebuild_idle:
            self._rebuild_idle = GLib.idle_add(self._rebuild)

    def _rebuild(self) -> bool:
        if not hasattr(self, "msg"):
            return False
        self._rebuild_idle = 0
        self._rebuild_queued = False
        m = self.msg
        if m is None:
            self._reset_dirty()
            return False
        if self._dirty_full:
            self._reset_dirty()
            self.view._layout_changing()
            self._build(self.view.previous(m))
            return False
        if self._dirty_meta:
            self._dirty_meta = False
            self._update_meta_in_place()
        if self._dirty_reactions:
            self._dirty_reactions = False
            self._update_reactions_in_place()
        if self._dirty_reply:
            self._dirty_reply = False
            self._update_reply_in_place()
        return False

    def animate_in(self) -> None:
        """Slide this message up into its place when it is sent or received."""
        gtk_settings = Gtk.Settings.get_default()
        enabled = bool(self.view.settings["appearance.message_animations"])
        if gtk_settings is not None and not gtk_settings.get_property("gtk-enable-animations"):
            enabled = False
        if not enabled:
            self.reveal.set_transition_type(Gtk.RevealerTransitionType.NONE)
            self.reveal.set_reveal_child(True)
            return
        # collapse without animating that, then reveal in the next frame so the slide starts at the bottom
        if self._reveal_idle:
            GLib.source_remove(self._reveal_idle)
            self._reveal_idle = 0
        self.reveal.set_transition_type(Gtk.RevealerTransitionType.NONE)
        self.reveal.set_reveal_child(False)
        self.reveal.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self._reveal_idle = GLib.idle_add(self._reveal_content)

    def _reveal_content(self) -> bool:
        self._reveal_idle = 0
        if hasattr(self, "reveal") and self.msg is not None:
            self.reveal.set_reveal_child(True)
        return False

    def _on_revealed(self, *_a) -> None:
        if hasattr(self, "reveal") and self.reveal.get_child_revealed() and getattr(self.view, "_stick", False):
            self.view._queue_settle_bottom()

    # ------------------------------------------------------------------ building
    def _build(self, prev: MessageItem | None) -> None:
        m = self.msg
        # Every build creates new widgets and connects a fresh handler to each attachment. Dropping the old
        # ones here matters: a row is rebuilt on every status or reaction change, and the handlers used to pile
        # up on the attachment (each one pointing at a widget that no longer exists), so one more rebuild ran
        # for every past rebuild.
        for att, handler in self._att_handlers:
            if GObject.signal_handler_is_connected(att, handler):
                att.disconnect(handler)
        self._att_handlers = []
        widgets.clear(self.separator)
        widgets.clear(self.row)
        self._clear_build_refs()
        self.actions.set_opacity(0)
        self.actions.set_sensitive(False)
        settings = self.view.settings
        layout = settings["appearance.layout"]
        self._layout = layout
        for c in ("first", "outgoing", "selected", "highlight", "mention"):
            self.row.remove_css_class(c)
        # date separator and "new messages" line
        if prev is None or not widgets.same_day(prev.timestamp, m.timestamp):
            self.separator.append(Gtk.Label(label=widgets.day_label(m.timestamp), css_classes=["date-separator"]))
        if self.view.first_unread_id and m.id == self.view.first_unread_id:
            line = Gtk.Box(spacing=8, css_classes=["new-messages"], margin_start=12, margin_end=12)
            line.append(Gtk.Separator(hexpand=True, valign=Gtk.Align.CENTER))
            line.append(Gtk.Label(label=_("New messages")))
            line.append(Gtk.Separator(hexpand=True, valign=Gtk.Align.CENTER))
            self.separator.append(line)
        if m.kind == "status":
            self.row.append(Gtk.Label(label=m.body, css_classes=["status-line"], hexpand=True, wrap=True))
            return
        if m.kind == "call":
            self.row.append(self._call_line(m))
            return
        cont = continues(prev, m)
        if not cont:
            self.row.add_css_class("first")
        if m.outgoing:
            self.row.add_css_class("outgoing")
        if m.selected:
            self.row.add_css_class("selected")
        if m.mentions_me:
            self.row.add_css_class("mention")
        if self.view.highlight_id == m.id:
            self.row.add_css_class("highlight")
        dark = style.is_dark()
        if layout == "compact":
            self._build_compact(m, dark)
            return
        chat_type = self.view.chat.type if self.view.chat is not None else "chat"
        show_avatar = settings["appearance.avatars"] == "always" or (
            settings["appearance.avatars"] == "groups" and chat_type == "groupchat")
        if show_avatar or layout == "bubbles":
            if not cont and (show_avatar and not (layout == "bubbles" and m.outgoing)):
                av = widgets.Avatar(36, self.view.display_name(m), self.view.avatar_path(m))
                av.set_valign(Gtk.Align.START)
                av.set_margin_top(2)
                self.row.append(av)
            elif show_avatar and not (layout == "bubbles" and m.outgoing):
                spacer = Gtk.Label(css_classes=["message-time"], width_chars=6, xalign=0.5, valign=Gtk.Align.START)
                if settings["appearance.timestamps"] != "off":
                    spacer.set_text(widgets.clock(m.timestamp, settings))
                    spacer.set_opacity(0 if settings["appearance.timestamps"] == "hover" else 1)
                self.time_hover = spacer
                spacer.set_size_request(36, -1)
                self.row.append(spacer)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                      css_classes=["message-content"] if layout == "bubbles" else [])
        self._col = col
        if layout == "bubbles":
            col.set_hexpand(False)
            col.set_halign(Gtk.Align.END if m.outgoing else Gtk.Align.START)
            self.row.set_halign(Gtk.Align.FILL)
            # A Gtk.Box packs its children along the main axis and ignores their halign there: give the bubble an
            # expanding box that fills the row, so the box can align it left or right.
            wrap = Gtk.Box(hexpand=True, halign=Gtk.Align.END if m.outgoing else Gtk.Align.START)
            wrap.append(col)
            self.row.append(wrap)
        else:
            self.row.append(col)
        bubbles = layout == "bubbles"
        if not cont and (not bubbles or (chat_type == "groupchat" and not m.outgoing)):
            col.append(self._header(m, with_meta=not bubbles, dark=dark))
        if m.reply_to and m.retracted == 0:
            col.append(self._reply_preview(m))
        col.append(self._content(m, dark=dark))
        # A deleted message keeps no media: the attachments (and the image bubble they
        # draw) belong to the message that was there, not to its tombstone.
        if not m.retracted:
            for att in m.attachments:
                col.append(self._attachment(m, att))
        if m.reactions:
            rw = self._reactions(m)
            self._reactions_widget = rw
            col.append(rw)
        if m.status == STATUS_FAILED and m.outgoing:
            fw = self._failed(m)
            self._failed_widget = fw
            self._showing_failed = True
            col.append(fw)
        if layout == "bubbles":
            meta = Gtk.Box(spacing=4, halign=Gtk.Align.END)
            clock_lbl = Gtk.Label(label=widgets.clock(m.timestamp, settings), css_classes=["message-time"])
            meta.append(clock_lbl)
            self._meta_container = meta
            self._meta_anchor = clock_lbl
            self._bubbles_meta = meta
            self._meta_icons(meta, m)
            col.append(meta)
        self._add_hover_bar(m)

    def _call_line(self, m: MessageItem) -> Gtk.Widget:
        """A call: who called, how it ended or how long it lasted, and a button to call back."""
        c = m.extra.get("call") or {}
        video = "video" in (c.get("media") or [])
        missed = c.get("end") in ("missed",) and not c.get("outgoing")
        box = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, hexpand=True, css_classes=["call-line"] + (
            ["missed"] if missed else []))
        icon = "camera-video-symbolic" if video else ("call-missed-symbolic" if missed else (
            "call-outgoing-symbolic" if c.get("outgoing") else "call-incoming-symbolic"))
        box.append(Gtk.Image(icon_name=icon))
        # lines from before calls were rebuilt carry their summary as the body, with no "end"
        text = self.view.core.calls.summary(m.extra) if c.get("end") or not m.body else m.body
        box.append(Gtk.Label(label=text))
        box.append(Gtk.Label(label=widgets.clock(m.timestamp, self.view.settings), css_classes=["message-time"]))
        if self.view.core.calls.available() and self.view.chat is not None and self.view.chat.type == "chat":
            back = Gtk.Button(icon_name="call-start-symbolic" if not video else "camera-video-symbolic",
                              css_classes=["flat", "circular"], tooltip_text=_("Call back"))
            back.connect("clicked", lambda *_: self.view.start_call(video))
            box.append(back)
        return box

    def _build_compact(self, m: MessageItem, dark: bool | None = None) -> None:
        line = Gtk.Box(spacing=6, hexpand=True)
        if self.view.settings["appearance.timestamps"] != "off":
            line.append(Gtk.Label(label=widgets.clock(m.timestamp, self.view.settings), css_classes=["message-time"],
                                  valign=Gtk.Align.START))
        name = Gtk.Label(css_classes=["message-sender"], valign=Gtk.Align.START)
        name.set_markup(self._name_markup(m, dark) + ("" if m.kind == "me" else ":"))
        line.append(name)
        line.append(self._content(m, dark=dark))
        self.row.append(line)
        if not m.retracted:
            for att in m.attachments:
                self.row.append(self._attachment(m, att))
        self._add_hover_bar(m)

    def _name_markup(self, m: MessageItem, dark: bool | None = None) -> str:
        name = self.view.display_name(m)
        if self.view.settings["appearance.nick_colors"]:
            chat_type = self.view.chat.type if self.view.chat is not None else "chat"
            key = m.sender_jid or m.sender if chat_type != "groupchat" else m.sender
            col = colors.hex_color(key or name, style.is_dark() if dark is None else dark)
            return "<span foreground='%s'>%s</span>" % (col, widgets.escape(name))
        return widgets.escape(name)

    def _header(self, m: MessageItem, with_meta: bool = True, dark: bool | None = None) -> Gtk.Widget:
        head = Gtk.Box(spacing=6)
        name = Gtk.Label(css_classes=["message-sender"], xalign=0, ellipsize=Pango.EllipsizeMode.END)
        name.set_markup(self._name_markup(m, dark))
        head.append(name)
        if not with_meta:
            return head
        anchor: Gtk.Widget = name
        if self.view.settings["appearance.timestamps"] != "off":
            t = Gtk.Label(label=widgets.clock(m.timestamp, self.view.settings), css_classes=["message-time"])
            dt = GLib.DateTime.new_from_unix_local(m.timestamp // 1000)
            if dt is not None:
                t.set_tooltip_text(dt.format("%c") or "")
            head.append(t)
            anchor = t
        self._meta_container = head
        self._meta_anchor = anchor
        self._meta_icons(head, m)
        return head

    def _meta_icons(self, box: Gtk.Box, m: MessageItem) -> None:
        self._showing_edited = bool(m.edited)
        if m.encryption:
            icon = Gtk.Image.new_from_icon_name("ward-lock-symbolic")
            icon.add_css_class("message-meta")
            trust = {"verified": _("from a verified device"), "trusted": _("from a trusted device"),
                     "undecided": _("from a device you haven't decided about"),
                     "distrusted": _("from a distrusted device")}.get(m.trust, "")
            icon.set_tooltip_text(("%s %s" % (_("Encrypted with %s") % ENC_NAMES.get(m.encryption, m.encryption),
                                              trust)).strip())
            if m.trust in ("undecided", "distrusted"):
                icon.add_css_class("trust-" + m.trust)
            if m.extra.get("local_only"):
                icon.set_tooltip_text(_("Kept on this device only: you have no other device that can read it"))
            box.append(icon)
        elif m.extra.get("unencrypted"):
            icon = Gtk.Image(icon_name="ward-lock-open-symbolic", css_classes=["lock-off"])
            icon.set_tooltip_text(_("Not encrypted"))
            box.append(icon)
        if m.edited:
            ed = Gtk.Button(label=_("edited"), css_classes=["flat", "message-meta"], valign=Gtk.Align.CENTER)
            ed.connect("clicked", lambda *_: self.view.show_edit_history(m))
            box.append(ed)
        if m.outgoing and m.status in STATUS_ICON:
            icon_name, tip = STATUS_ICON[m.status]
            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.add_css_class("message-meta")
            if m.status == STATUS_READ:
                icon.add_css_class("trust-trusted")
            if m.status == STATUS_FAILED:
                icon.add_css_class("trust-distrusted")
            icon.set_tooltip_text(_(tip))
            box.append(icon)

    def _reply_preview(self, m: MessageItem) -> Gtk.Widget:
        btn = Gtk.Button(css_classes=["flat", "reply-preview"], halign=Gtk.Align.START)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        author_lbl = Gtk.Label(label=m.reply_author or _("Reply"), css_classes=["author"], xalign=0)
        text_lbl = Gtk.Label(label=m.reply_preview or "…", css_classes=["text"], xalign=0,
                             ellipsize=Pango.EllipsizeMode.END, max_width_chars=60)
        self._reply_author_lbl = author_lbl
        self._reply_text_lbl = text_lbl
        box.append(author_lbl)
        box.append(text_lbl)
        btn.set_child(box)
        btn.set_tooltip_text(_("Go to the original message"))
        btn.connect("clicked", lambda *_: self.view.jump_to(m.reply_target or 0, m.reply_to))
        return btn

    def _content(self, m: MessageItem, dark: bool | None = None) -> Gtk.Widget:
        if m.retracted:
            text = {1: _("This message was deleted"), 2: _("You deleted this message"),
                    3: _("Removed by a moderator")}.get(m.retracted, _("Deleted"))
            if m.retracted == 3 and m.extra.get("moderation_reason"):
                text += ": " + m.extra["moderation_reason"]
            return Gtk.Label(label=text, css_classes=["message-deleted"], xalign=0)
        if m.extra.get("decrypt_error"):
            lbl = Gtk.Label(label=m.extra["decrypt_error"], css_classes=["message-error"], xalign=0, wrap=True)
            return lbl
        body = m.body
        if not body.strip():
            return Gtk.Box()
        action = m.kind == "me"
        if action:
            body = "%s %s" % (self.view.display_name(m), body[4:])
        settings = self.view.settings
        # The body is the source of truth: outgoing messages and older rows may have no emoji_only flag in
        # their stored extra, and edits change the body without touching it.
        if emoji_only(m.body) and settings["appearance.large_emoji"]:
            lbl = Gtk.Label(label=body, xalign=0, css_classes=["message-text", "emoji-only"], selectable=True)
            return lbl
        nicks = self.view.mention_names() if "@" in body else ()
        doc = render.parsed(m.id, body, m.is_markdown, nicks, settings["chats.markdown_others"])
        r = render.Renderer(on_link=self.view.on_link, dark=style.is_dark() if dark is None else dark,
                            action=action, on_code_copy=self.view.on_code_copy)
        w = r.build(doc)
        if m.extra.get("unencrypted"):
            w.add_css_class("message-unencrypted")
            w.set_tooltip_text(_("This message was not encrypted, although this chat is"))
        if m.extra.get("spoiler") is not None:
            exp = Gtk.Expander(label=m.extra.get("spoiler") or _("Spoiler"), css_classes=["spoiler-hint"])
            exp.set_child(w)
            return exp
        limit = int(settings["chats.collapse_after"] or 0)
        if limit and body.count("\n") > limit and not getattr(self, "_expanded", False):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            # EXTERNAL, not NEVER: with NEVER the scroller asks for its child's full height and the limit does
            # nothing.  The width follows the content, so code is not squeezed while collapsed.
            sc = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                                    hscrollbar_policy=Gtk.PolicyType.NEVER,
                                    propagate_natural_width=True, propagate_natural_height=True)
            sc.set_child(w)
            sc.set_max_content_height(limit * 22)
            sc.set_min_content_height(limit * 22)   # the message is longer than this, and a list row gets the minimum
            box.append(sc)
            more = Gtk.Button(label=_("Show more"), css_classes=["flat"], halign=Gtk.Align.START)

            def expand(_b):
                self._expanded = True
                self._changed()
            more.connect("clicked", expand)
            box.append(more)
            return box
        if limit and body.count("\n") > limit:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            box.append(w)
            less = Gtk.Button(label=_("Show less"), css_classes=["flat"], halign=Gtk.Align.START)

            def collapse(_b):
                self._expanded = False
                self._changed()
            less.connect("clicked", collapse)
            box.append(less)
            return box
        return w

    def _attachment(self, m: MessageItem, att: AttachmentItem) -> Gtk.Widget:
        self._att_handlers.append((att, att.connect("notify::state", lambda *_: self._changed())))
        thumb = att.thumbnail if att.thumbnail and os.path.exists(att.thumbnail) else ""
        if att.is_image:
            path = "" if thumb else (att.local_path if att.local_path and os.path.exists(att.local_path) else "")
        else:
            path = att.local_path if att.local_path and os.path.exists(att.local_path) else ""
        if att.is_image and (path or thumb):
            img_path = thumb or path
            tex = widgets.cached_texture(img_path, widgets.IMAGE_PIXELS)
            pic = Gtk.Picture.new_for_paintable(tex) if tex is not None else Gtk.Picture()
            pic.set_can_shrink(True)
            pic.set_content_fit(Gtk.ContentFit.COVER if hasattr(Gtk, "ContentFit") else 0)
            w = max(att.width or 320, 1)
            h = max(att.height or 240, 1)
            scale = min(320 / w, 280 / h, 1.0)
            pic.set_size_request(max(80, int(w * scale)), max(60, int(h * scale)))
            pic.add_css_class("attachment-image")
            if tex is None:
                row_ref = weakref.ref(self)
                pic_ref = weakref.ref(pic)

                def _on_tex(loaded) -> None:
                    r = row_ref()
                    p = pic_ref()
                    if r is None or p is None or loaded is None or getattr(r, "msg", None) is not m:
                        return
                    if p.get_parent() is None:
                        return
                    p.set_paintable(loaded)
                    if getattr(r.view, "_stick", False) and (not att.width or not att.height):
                        r.view._queue_settle_bottom()

                widgets.texture_async(img_path, widgets.IMAGE_PIXELS, _on_tex)
            btn = Gtk.Button(css_classes=["flat"], halign=Gtk.Align.START)
            btn.set_child(pic)
            btn.set_tooltip_text(att.name)
            btn.connect("clicked", lambda *_: self.view.open_attachment(m, att))
            return btn
        external_audio = att.is_audio and (att.mime == "audio/mpeg" or path.lower().endswith(".mp3"))
        if path and (att.is_audio or att.is_video) and not external_audio:
            return video.attachment(path, att.is_video, lambda: self.view.save_attachment(m, att))
        card = Gtk.Box(spacing=10, css_classes=["attachment-card"], halign=Gtk.Align.START)
        gicon = _mime_gicon(att.mime or "")
        card.append(Gtk.Image.new_from_gicon(gicon) if gicon else Gtk.Image.new_from_icon_name("text-x-generic"))
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        info.append(Gtk.Label(label=att.name or _("File"), xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE,
                              max_width_chars=36))
        sub = widgets.human(att.size) if att.size else ""
        if att.encrypted:
            sub = (sub + " · " if sub else "") + _("encrypted")
        if att.state == "failed":
            sub = att.error or _("Failed")
        info.append(Gtk.Label(label=sub, xalign=0, css_classes=["message-meta"]))
        card.append(info)
        if att.state in ("downloading", "uploading"):
            bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER, hexpand=True)
            bar.set_size_request(80, -1)
            self._att_handlers.append((att, att.connect("notify::progress",
                                                        lambda a, _p: bar.set_fraction(a.progress))))
            card.append(bar)
            cancel = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat", "circular"],
                                tooltip_text=_("Cancel"))
            cancel.connect("clicked", lambda *_: self.view.core.files.cancel(att))
            card.append(cancel)
        elif path:
            # GTK's Gtk.Video feeds audio through GStreamer's decodebin3. In some GStreamer
            # releases an MP3 without a collection triggers an assertion there and aborts the
            # whole app. Play MP3 in the system player instead; OGG/Opus and video keep the
            # in-app player.
            op = Gtk.Button(
                icon_name="media-playback-start-symbolic" if external_audio else "document-open-symbolic",
                css_classes=["flat", "circular"],
                tooltip_text=_("Play in system player") if external_audio else _("Open"))
            op.connect("clicked", lambda *_: widgets.open_file(self, path, att.mime))
            card.append(op)
            sv = Gtk.Button(icon_name="document-save-symbolic", css_classes=["flat", "circular"],
                            tooltip_text=_("Save as…"))
            sv.connect("clicked", lambda *_: self.view.save_attachment(m, att))
            card.append(sv)
        elif att.url:
            dl = Gtk.Button(icon_name="folder-download-symbolic", css_classes=["flat", "circular"],
                            tooltip_text=_("Download"))
            dl.connect("clicked", lambda *_: self.view.download(m, att))
            card.append(dl)
        return card

    def _reactions(self, m: MessageItem) -> Gtk.Widget:
        from . import compat

        box = compat.wrap_box(4)
        for emoji, who, mine in m.reactions:
            b = Gtk.ToggleButton(label="%s %d" % (emoji, len(who)), active=mine, css_classes=["reaction"] +
                                 (["mine"] if mine else []))
            b.set_tooltip_text(", ".join(who))
            b.connect("clicked", lambda _b, e=emoji: self.view.react(m, e))
            compat.wrap_append(box, b)
        return box

    def _failed(self, m: MessageItem) -> Gtk.Widget:
        box = Gtk.Box(spacing=6)
        box.append(Gtk.Label(label=m.error or _("Not sent"), css_classes=["message-error"], wrap=True, xalign=0,
                             hexpand=True))
        retry = Gtk.Button(label=_("Retry"), css_classes=["flat"])
        retry.connect("clicked", lambda *_: self.view.retry(m))
        box.append(retry)
        return box

    # ------------------------------------------------------------------ hover bar and menu
    def _add_hover_bar(self, m: MessageItem) -> None:
        if m.retracted or m.kind == "status":
            return
        quick = tuple(self.view.settings["chats.quick_reactions"][:3])
        bar = self.actions
        if self._hover_quick != quick:
            widgets.clear(bar)
            for e in quick:
                b = Gtk.Button(label=e, css_classes=["flat"], tooltip_text=_("React with %s") % e)
                b.connect("clicked", lambda _b, e=e: self.msg is not None and self.view.react(self.msg, e))
                bar.append(b)
            more = Gtk.MenuButton(icon_name="ward-react-symbolic", css_classes=["flat"], tooltip_text=_("Add reaction"))
            # keep the bar (and so the picker) while the picker is open: the pointer leaves the row to use it
            widgets.emoji_button(more, self.view.settings,
                                 lambda e: self.msg is not None and self.view.react(self.msg, e),
                                 lambda: self._hover(False))
            self._react_btn = more
            bar.append(more)
            reply = Gtk.Button(icon_name="ward-reply-symbolic", css_classes=["flat"], tooltip_text=_("Reply"))
            reply.connect("clicked", lambda *_: self.msg is not None and self.view.start_reply(self.msg))
            bar.append(reply)
            self._hover_quick = quick
        self.react_button = self._react_btn
        self.hover = bar

    def _hover(self, inside: bool) -> None:
        if self.hover is not None:
            if not inside and getattr(self, "react_button", None) is not None and self.react_button.get_active():
                return
            self.hover.set_opacity(1 if inside else 0)
            self.hover.set_sensitive(inside)
        if self.time_hover is not None and self.view.settings["appearance.timestamps"] == "hover":
            self.time_hover.set_opacity(1 if inside else 0)

    def _on_right_click(self, gesture, n, x, y) -> None:
        if self.msg is not None:
            self.view.message_menu(self, self.msg, x, y)


GObject.type_register(MessageRow)
