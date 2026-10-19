"""One message in the conversation.  Rows are recycled by the list view: bind() fills a row for a message (and
the one before it, for grouping and date separators), unbind() lets it go.  While bound, the row follows the
message's properties (edits, receipts, reactions, downloads) through notify signals."""

from __future__ import annotations

import os

from gi.repository import Gio, GLib, GObject, Gtk, Pango

from ..core import colors
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
        # the content is wrapped so a newly sent or received message can slide up into its place
        self.reveal = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.NONE, transition_duration=200,
                                   overflow=Gtk.Overflow.HIDDEN)
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

    def unbind(self) -> None:
        if self.msg is not None:
            for h in self._handlers:
                self.msg.disconnect(h)
        for att, h in self._att_handlers:
            att.disconnect(h)
        self._handlers = []
        self._att_handlers = []
        self.msg = None

    def _changed(self, *_a) -> None:
        if self.msg is not None:
            self._build(self.view.previous(self.msg))

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
        self.reveal.set_transition_type(Gtk.RevealerTransitionType.NONE)
        self.reveal.set_reveal_child(False)
        self.reveal.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        GLib.idle_add(self._reveal_content)

    def _reveal_content(self) -> bool:
        self.reveal.set_reveal_child(True)
        return False

    # ------------------------------------------------------------------ building
    def _build(self, prev: MessageItem | None) -> None:
        m = self.msg
        self.view._layout_changing()
        widgets.clear(self.separator)
        widgets.clear(self.row)
        widgets.clear(self.actions)
        self.actions.set_opacity(0)
        self.actions.set_sensitive(False)
        self.hover = None
        settings = self.view.settings
        layout = settings["appearance.layout"]
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
        if layout == "compact":
            self._build_compact(m)
            return
        show_avatar = settings["appearance.avatars"] == "always" or (
            settings["appearance.avatars"] == "groups" and self.view.chat.type == "groupchat")
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
        if not cont and (not bubbles or (self.view.chat.type == "groupchat" and not m.outgoing)):
            col.append(self._header(m, with_meta=not bubbles))
        if m.reply_to and m.retracted == 0:
            col.append(self._reply_preview(m))
        col.append(self._content(m))
        for att in m.attachments:
            col.append(self._attachment(m, att))
        if m.reactions:
            col.append(self._reactions(m))
        if m.status == STATUS_FAILED and m.outgoing:
            col.append(self._failed(m))
        if layout == "bubbles":
            meta = Gtk.Box(spacing=4, halign=Gtk.Align.END)
            meta.append(Gtk.Label(label=widgets.clock(m.timestamp, settings), css_classes=["message-time"]))
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
        box.append(Gtk.Label(label=self.view.core.calls.summary(m.extra)))
        box.append(Gtk.Label(label=widgets.clock(m.timestamp, self.view.settings), css_classes=["message-time"]))
        if self.view.core.calls.available() and self.view.chat is not None and self.view.chat.type == "chat":
            back = Gtk.Button(icon_name="call-start-symbolic" if not video else "camera-video-symbolic",
                              css_classes=["flat", "circular"], tooltip_text=_("Call back"))
            back.connect("clicked", lambda *_: self.view.start_call(video))
            box.append(back)
        return box

    def _build_compact(self, m: MessageItem) -> None:
        line = Gtk.Box(spacing=6, hexpand=True)
        if self.view.settings["appearance.timestamps"] != "off":
            line.append(Gtk.Label(label=widgets.clock(m.timestamp, self.view.settings), css_classes=["message-time"],
                                  valign=Gtk.Align.START))
        name = Gtk.Label(css_classes=["message-sender"], valign=Gtk.Align.START)
        name.set_markup(self._name_markup(m) + ("" if m.kind == "me" else ":"))
        line.append(name)
        line.append(self._content(m))
        self.row.append(line)
        for att in m.attachments:
            self.row.append(self._attachment(m, att))
        self._add_hover_bar(m)

    def _name_markup(self, m: MessageItem) -> str:
        name = self.view.display_name(m)
        if self.view.settings["appearance.nick_colors"]:
            key = m.sender_jid or m.sender if self.view.chat.type != "groupchat" else m.sender
            col = colors.hex_color(key or name, style.is_dark())
            return "<span foreground='%s'>%s</span>" % (col, widgets.escape(name))
        return widgets.escape(name)

    def _header(self, m: MessageItem, with_meta: bool = True) -> Gtk.Widget:
        head = Gtk.Box(spacing=6)
        name = Gtk.Label(css_classes=["message-sender"], xalign=0, ellipsize=Pango.EllipsizeMode.END)
        name.set_markup(self._name_markup(m))
        head.append(name)
        if not with_meta:
            return head
        if self.view.settings["appearance.timestamps"] != "off":
            t = Gtk.Label(label=widgets.clock(m.timestamp, self.view.settings), css_classes=["message-time"])
            t.set_tooltip_text(GLib.DateTime.new_from_unix_local(m.timestamp // 1000).format("%c") or "")
            head.append(t)
        self._meta_icons(head, m)
        return head

    def _meta_icons(self, box: Gtk.Box, m: MessageItem) -> None:
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
        box.append(Gtk.Label(label=m.reply_author or _("Reply"), css_classes=["author"], xalign=0))
        box.append(Gtk.Label(label=m.reply_preview or "…", css_classes=["text"], xalign=0,
                             ellipsize=Pango.EllipsizeMode.END, max_width_chars=60))
        btn.set_child(box)
        btn.set_tooltip_text(_("Go to the original message"))
        btn.connect("clicked", lambda *_: self.view.jump_to(m.reply_target or 0, m.reply_to))
        return btn

    def _content(self, m: MessageItem) -> Gtk.Widget:
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
        if m.extra.get("emoji_only") and settings["appearance.large_emoji"]:
            lbl = Gtk.Label(label=body, xalign=0, css_classes=["message-text", "emoji-only"], selectable=True)
            return lbl
        nicks = self.view.mention_names()
        doc = render.parsed(m.id, body, m.is_markdown, nicks, settings["chats.markdown_others"])
        r = render.Renderer(on_link=self.view.on_link, dark=style.is_dark(), action=action,
                            on_code_copy=self.view.on_code_copy)
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
        path = att.local_path if att.local_path and os.path.exists(att.local_path) else ""
        thumb = att.thumbnail if att.thumbnail and os.path.exists(att.thumbnail) else ""
        if att.is_image and (path or thumb):
            pic = Gtk.Picture.new_for_paintable(widgets.texture(thumb or path, widgets.IMAGE_PIXELS))
            pic.set_can_shrink(True)
            pic.set_content_fit(Gtk.ContentFit.COVER if hasattr(Gtk, "ContentFit") else 0)
            w = max(att.width or 320, 1)
            h = max(att.height or 240, 1)
            scale = min(320 / w, 280 / h, 1.0)
            pic.set_size_request(max(80, int(w * scale)), max(60, int(h * scale)))
            pic.add_css_class("attachment-image")
            btn = Gtk.Button(css_classes=["flat"], halign=Gtk.Align.START)
            btn.set_child(pic)
            btn.set_tooltip_text(att.name)
            btn.connect("clicked", lambda *_: self.view.open_attachment(m, att))
            return btn
        if path and (att.is_audio or att.is_video):
            return video.attachment(path, att.is_video, lambda: self.view.save_attachment(m, att))
        card = Gtk.Box(spacing=10, css_classes=["attachment-card"], halign=Gtk.Align.START)
        gicon = Gio.content_type_get_symbolic_icon(Gio.content_type_from_mime_type(att.mime or "") or "")
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
            op = Gtk.Button(icon_name="document-open-symbolic", css_classes=["flat", "circular"],
                            tooltip_text=_("Open"))
            op.connect("clicked", lambda *_: widgets.open_file(self, path))
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
        bar = self.actions
        for e in self.view.settings["chats.quick_reactions"][:3]:
            b = Gtk.Button(label=e, css_classes=["flat"], tooltip_text=_("React with %s") % e)
            b.connect("clicked", lambda _b, e=e: self.view.react(m, e))
            bar.append(b)
        more = Gtk.MenuButton(icon_name="ward-react-symbolic", css_classes=["flat"], tooltip_text=_("Add reaction"))
        # keep the bar (and so the picker) while the picker is open: the pointer leaves the row to use it
        widgets.emoji_button(more, self.view.settings, lambda e: self.view.react(m, e), lambda: self._hover(False))
        self.react_button = more
        bar.append(more)
        reply = Gtk.Button(icon_name="ward-reply-symbolic", css_classes=["flat"], tooltip_text=_("Reply"))
        reply.connect("clicked", lambda *_: self.view.start_reply(m))
        bar.append(reply)
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
