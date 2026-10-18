"""The message composer: a growing text box with Markdown shortcuts, emoji and nickname completion, reply and
edit banners, attachments (button, paste, drag and drop), typing notifications, drafts and a few /commands."""

from __future__ import annotations

import os
import re
import tempfile

from gi.repository import Gdk, GLib, Gtk, Pango

from .. import deps, paths
from ..core.markup import emoji
from ..core.models import ChatItem, MessageItem
from ..i18n import _
from . import compat, widgets

TYPING_PAUSE_MS = 5000
MAX_HEIGHT = 200
WORD_RE = re.compile(r"(?:^|\s)([:@][^\s:@]{1,40})$")
COMMANDS = {
    "me": "",
    "nick": "groupchat",
    "topic": "groupchat",
    "invite": "groupchat",
    "kick": "groupchat",
    "ban": "groupchat",
    "part": "groupchat",
}


def _spelling(view: Gtk.TextView, languages: list[str]) -> object | None:
    """libspelling, when installed: red underlines and suggestions in the context menu."""
    try:
        import gi

        gi.require_version("Spelling", "1")
        gi.require_version("GtkSource", "5")
        from gi.repository import Spelling
    except (ImportError, ValueError):
        return None
    try:
        checker = Spelling.Checker.get_default()
        if languages:
            checker.set_language(languages[0])
        adapter = Spelling.TextBufferAdapter.new(view.get_buffer(), checker)
        view.set_extra_menu(adapter.get_menu_model())
        view.insert_action_group("spelling", adapter)
        adapter.set_enabled(True)
        return adapter
    except Exception:
        return None


def _make_view() -> Gtk.TextView:
    try:
        import gi

        gi.require_version("GtkSource", "5")
        from gi.repository import GtkSource

        buf = GtkSource.Buffer()
        buf.set_highlight_syntax(False)
        return GtkSource.View(buffer=buf, wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False)
    except (ImportError, ValueError):
        return Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False)


class Composer(Gtk.Box):
    __gtype_name__ = "WardComposer"

    def __init__(self, view):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["composer"])
        self.view = view
        self.core = view.core
        self.settings = view.settings
        self.chat: ChatItem | None = None
        self.reply_to: MessageItem | None = None
        self.editing: MessageItem | None = None
        self._loading = False
        self._pause_timer = 0
        self._completions: list[tuple[str, str]] = []
        self._completion_word = ""

        # reply / edit banner
        self.banner = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        bbox = Gtk.Box(spacing=8, css_classes=["composer-banner"])
        self.banner_icon = Gtk.Image()
        bbox.append(self.banner_icon)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.banner_title = Gtk.Label(xalign=0, css_classes=["heading"], ellipsize=Pango.EllipsizeMode.END)
        self.banner_text = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["dim-label"])
        texts.append(self.banner_title)
        texts.append(self.banner_text)
        bbox.append(texts)
        close = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat", "circular"],
                           valign=Gtk.Align.CENTER, tooltip_text=_("Cancel (Escape)"))
        close.connect("clicked", lambda *_: self.cancel())
        bbox.append(close)
        self.banner.set_child(bbox)
        self.append(self.banner)

        # formatting toolbar (optional)
        self.toolbar = Gtk.Box(spacing=2, margin_start=8, visible=self.settings["chats.toolbar"])
        for icon, tip, before, after in (("format-text-bold-symbolic", _("Bold (Ctrl+B)"), "**", "**"),
                                         ("format-text-italic-symbolic", _("Italic (Ctrl+I)"), "_", "_"),
                                         ("format-text-strikethrough-symbolic", _("Strikethrough"), "~~", "~~"),
                                         ("ward-markdown-symbolic", _("Code"), "`", "`"),
                                         ("format-indent-more-symbolic", _("Quote"), "> ", "")):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, css_classes=["flat"])
            b.connect("clicked", lambda _b, x=before, y=after: self.wrap_selection(x, y))
            self.toolbar.append(b)
        self.append(self.toolbar)

        row = Gtk.Box(spacing=6)
        self.append(row)
        self.attach_button = Gtk.Button(icon_name="ward-attach-symbolic", css_classes=["flat", "circular"],
                                        valign=Gtk.Align.END, tooltip_text=_("Send a file"))
        self.attach_button.connect("clicked", lambda *_: self.choose_files())
        row.append(self.attach_button)

        box = Gtk.Box(css_classes=["composer-box"], hexpand=True)
        row.append(box)
        overlay = Gtk.Overlay(hexpand=True)
        self.text = _make_view()
        self.text.set_top_margin(6)
        self.text.set_bottom_margin(6)
        self.text.set_left_margin(4)
        self.text.set_right_margin(4)
        self.text.set_hexpand(True)
        self.buffer = self.text.get_buffer()
        if hasattr(self.buffer, "set_enable_undo"):
            self.buffer.set_enable_undo(True)
        # a scrolled window's vertical scrollbar would make the box two lines high while empty: grow with the
        # text instead (_fit), and only show a scrollbar beyond MAX_HEIGHT
        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                           vscrollbar_policy=Gtk.PolicyType.EXTERNAL)
        self.scroller.set_child(self.text)
        overlay.set_child(self.scroller)
        self.placeholder = Gtk.Label(xalign=0, css_classes=["dim-label"], can_target=False, margin_start=4,
                                     margin_top=6, valign=Gtk.Align.START, ellipsize=Pango.EllipsizeMode.END)
        overlay.add_overlay(self.placeholder)
        box.append(overlay)
        self.emoji_button = Gtk.MenuButton(icon_name="ward-emoji-symbolic", css_classes=["flat", "circular"],
                                           valign=Gtk.Align.END, tooltip_text=_("Emoji"))
        chooser = Gtk.EmojiChooser()
        chooser.connect("emoji-picked", lambda _c, e: self._insert(e))
        self.emoji_button.set_popover(chooser)
        box.append(self.emoji_button)
        self.send_button = Gtk.Button(icon_name="ward-send-symbolic", css_classes=["suggested-action", "circular"],
                                      valign=Gtk.Align.END, sensitive=False, tooltip_text=_("Send"))
        self.send_button.connect("clicked", lambda *_: self.send())
        row.append(self.send_button)

        self.buffer.connect("changed", self._on_changed)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.text.add_controller(keys)
        self.text.connect("paste-clipboard", self._on_paste)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: self._set_state("paused") if self._text() else None)
        self.text.add_controller(focus)

        self.popover = Gtk.Popover(autohide=False, has_arrow=False, position=Gtk.PositionType.TOP)
        self.popover.set_parent(self.text)
        self.popover_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=["navigation-sidebar"])
        self.popover_list.connect("row-activated", lambda _l, r: self._complete(r.get_index()))
        self.popover.set_child(self.popover_list)

        self._spell = _spelling(self.text, self.settings["chats.spell_languages"]) \
            if self.settings["chats.spellcheck"] and deps.have("spelling") else None
        self.settings.connect("changed::chats.toolbar", lambda *_: self.toolbar.set_visible(
            self.settings["chats.toolbar"]))
        self._update_placeholder()
        self.scroller.connect("notify::width", lambda *_: GLib.idle_add(self._fit))
        self.text.connect("map", lambda *_: GLib.idle_add(self._fit))

    # ================================================================== chat switching
    def set_chat(self, chat: ChatItem | None) -> None:
        self.cancel(clear=False)
        self.chat = chat
        self._loading = True
        self.buffer.set_text(chat.draft if chat is not None else "")
        self._loading = False
        self._update_placeholder()
        self.set_sensitive(chat is not None)
        if chat is not None:
            GLib.idle_add(self.grab_focus_text)

    def save_draft(self) -> None:
        if self.chat is not None and self.editing is None:
            self.core.chats.set_draft(self.chat, self._text())

    def grab_focus_text(self) -> bool:
        self.text.grab_focus()
        return False

    def _text(self) -> str:
        return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), False)

    def _update_placeholder(self) -> None:
        chat = self.chat
        empty = not self._text()
        self.placeholder.set_visible(empty)
        self.send_button.set_sensitive(not empty)
        if chat is None:
            return
        state = chat.encryption_state
        if self.editing is not None:
            text = _("Edit message")
        elif state in ("omemo", "omemo-legacy", "pgp"):
            text = _("Encrypted message")
        elif chat.type == "groupchat":
            text = _("Message %s") % (chat.name or chat.jid)
        else:
            text = _("Message")
        self.placeholder.set_text(text)

    # ================================================================== typing
    def _fit(self) -> bool:
        width = self.scroller.get_width()
        if width <= 0:
            return False
        natural = self.text.measure(Gtk.Orientation.VERTICAL, width)[1]
        height = max(1, min(natural, MAX_HEIGHT))
        self.scroller.set_min_content_height(height)
        self.scroller.set_max_content_height(MAX_HEIGHT)
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC if natural > MAX_HEIGHT
                                 else Gtk.PolicyType.EXTERNAL)
        return False

    def _on_changed(self, _buf) -> None:
        GLib.idle_add(self._fit)
        self._update_placeholder()
        if self._loading or self.chat is None:
            return
        text = self._text()
        if self.editing is None:
            self.core.chats.set_draft(self.chat, text)
        self._set_state("composing" if text else "active")
        if self._pause_timer:
            GLib.source_remove(self._pause_timer)
            self._pause_timer = 0
        if text:
            self._pause_timer = GLib.timeout_add(TYPING_PAUSE_MS, self._paused)
        self._update_completion()

    def _paused(self) -> bool:
        self._pause_timer = 0
        self._set_state("paused")
        return False

    def _set_state(self, state: str) -> None:
        if self.chat is not None:
            try:
                self.core.messaging.set_chat_state(self.chat, state)
            except Exception:
                pass

    # ================================================================== keys
    def _on_key(self, _ctrl, keyval: int, _code: int, state: Gdk.ModifierType) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if self.popover.get_visible():
            if keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
                self._move_completion(1 if keyval == Gdk.KEY_Down else -1)
                return True
            if keyval in (Gdk.KEY_Tab, Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                row = self.popover_list.get_selected_row()
                self._complete(row.get_index() if row else 0)
                return True
            if keyval == Gdk.KEY_Escape:
                self.popover.popdown()
                return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            send_with_ctrl = self.settings["chats.send_with"] == "ctrl_enter"
            if shift or (ctrl != send_with_ctrl):
                if send_with_ctrl and not ctrl:
                    return False
                self.buffer.insert_at_cursor("\n")
                return True
            self.send()
            return True
        if keyval == Gdk.KEY_Escape:
            if self.reply_to is not None or self.editing is not None:
                self.cancel()
                return True
            return False
        if keyval == Gdk.KEY_Up and not self._text() and not ctrl:
            last = self._last_own_message()
            if last is not None:
                self.start_edit(last)
                return True
        if ctrl and keyval in (Gdk.KEY_b, Gdk.KEY_B):
            self.wrap_selection("**", "**")
            return True
        if ctrl and keyval in (Gdk.KEY_i, Gdk.KEY_I):
            self.wrap_selection("_", "_")
            return True
        if keyval == Gdk.KEY_Tab and not ctrl and self.chat is not None and self.chat.type == "groupchat":
            return self._tab_nick()
        return False

    def _last_own_message(self) -> MessageItem | None:
        ml = self.view.ml
        if ml is None:
            return None
        for m in reversed(ml.items()):
            if m.outgoing and not m.retracted and m.kind in ("text", "me") and m.body:
                return m
        return None

    def wrap_selection(self, before: str, after: str) -> None:
        b = self.buffer
        if b.get_has_selection():
            start, end = b.get_selection_bounds()
            text = b.get_text(start, end, False)
            b.begin_user_action()
            b.delete(start, end)
            b.insert(start, before + text + after)
            b.end_user_action()
        else:
            b.insert_at_cursor(before + after)
            it = b.get_iter_at_mark(b.get_insert())
            it.backward_chars(len(after))
            b.place_cursor(it)
        self.text.grab_focus()

    def _insert(self, text: str) -> None:
        self.buffer.insert_at_cursor(text)
        self.text.grab_focus()

    # ================================================================== completion
    def _word_before_cursor(self) -> str:
        it = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        start = it.copy()
        start.set_line_offset(0)
        m = WORD_RE.search(self.buffer.get_text(start, it, False))
        return m.group(1) if m else ""

    def _update_completion(self) -> None:
        word = self._word_before_cursor()
        items: list[tuple[str, str]] = []
        if word.startswith(":") and len(word) >= 3 and self.settings["chats.shortcodes"]:
            items = [(e + "  :" + name + ":", e) for name, e in emoji.complete(word)]
        elif word.startswith("@") and self.chat is not None and self.chat.type == "groupchat":
            prefix = word[1:].lower()
            items = [(n, n) for n in sorted(self.view.mention_names(), key=str.lower)
                     if n.lower().startswith(prefix) and n != self.chat.our_nick][:8]
        self._completions = items
        self._completion_word = word
        if not items:
            self.popover.popdown()
            return
        widgets.clear(self.popover_list)
        for label, _v in items:
            self.popover_list.append(Gtk.Label(label=label, xalign=0, margin_start=6, margin_end=6))
        self.popover_list.select_row(self.popover_list.get_row_at_index(0))
        it = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        rect = self.text.get_iter_location(it)
        x, y = self.text.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, rect.x, rect.y)
        r = Gdk.Rectangle()
        r.x, r.y, r.width, r.height = x, y, 1, rect.height
        self.popover.set_pointing_to(r)
        self.popover.popup()

    def _move_completion(self, delta: int) -> None:
        row = self.popover_list.get_selected_row()
        i = (row.get_index() if row else -1) + delta
        i = max(0, min(len(self._completions) - 1, i))
        self.popover_list.select_row(self.popover_list.get_row_at_index(i))

    def _complete(self, index: int) -> None:
        if not (0 <= index < len(self._completions)):
            return
        value = self._completions[index][1]
        word = self._completion_word
        it = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        start = it.copy()
        start.backward_chars(len(word))
        if word.startswith("@"):
            at_start = start.get_offset() == 0
            value = value + (": " if at_start else " ")
        self.buffer.begin_user_action()
        self.buffer.delete(start, it)
        self.buffer.insert(start, value)
        self.buffer.end_user_action()
        self.popover.popdown()

    def _tab_nick(self) -> bool:
        """IRC habit: Tab after the start of a nickname completes it."""
        it = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        start = it.copy()
        start.set_line_offset(0)
        before = self.buffer.get_text(start, it, False)
        m = re.search(r"(\S+)$", before)
        if not m:
            return False
        prefix = m.group(1).lower()
        names = [n for n in self.view.mention_names() if n.lower().startswith(prefix)]
        if not names:
            return False
        name = sorted(names, key=str.lower)[0]
        s = it.copy()
        s.backward_chars(len(m.group(1)))
        self.buffer.delete(s, it)
        self.buffer.insert(s, name + (": " if s.get_offset() == 0 else " "))
        return True

    # ================================================================== reply and edit
    def start_reply(self, m: MessageItem) -> None:
        self.editing = None
        self.reply_to = m
        self.banner_icon.set_from_icon_name("ward-reply-symbolic")
        self.banner_title.set_text(_("Reply to %s") % self.view.display_name(m))
        self.banner_text.set_text((m.body or (m.attachments[0].name if m.attachments else "")).splitlines()[0]
                                  if (m.body or m.attachments) else "")
        self.banner.get_child().remove_css_class("editing")
        self.banner.set_reveal_child(True)
        self.text.grab_focus()

    def start_edit(self, m: MessageItem) -> None:
        self.save_draft()
        self.reply_to = None
        self.editing = m
        self.banner_icon.set_from_icon_name("document-edit-symbolic")
        self.banner_title.set_text(_("Edit message"))
        self.banner_text.set_text(m.body.splitlines()[0] if m.body else "")
        self.banner.get_child().add_css_class("editing")
        self.banner.set_reveal_child(True)
        self._loading = True
        self.buffer.set_text(m.body)
        self._loading = False
        self._update_placeholder()
        self.text.grab_focus()

    def quote(self, m: MessageItem) -> None:
        quoted = "\n".join("> " + line for line in m.body.splitlines()) + "\n"
        self.buffer.insert(self.buffer.get_start_iter(), quoted)
        self.text.grab_focus()

    def cancel(self, clear: bool = True) -> None:
        was_editing = self.editing is not None
        self.reply_to = None
        self.editing = None
        self.banner.set_reveal_child(False)
        if was_editing and clear:
            self._loading = True
            self.buffer.set_text(self.chat.draft if self.chat is not None else "")
            self._loading = False
        self._update_placeholder()

    # ================================================================== sending
    def send(self) -> None:
        chat = self.chat
        text = self._text()
        if chat is None or not text.strip():
            return
        if self.settings["chats.shortcodes"]:
            text = emoji.replace_shortcodes(text)
        if self.settings["chats.ascii_smileys"]:
            text = emoji.replace_smileys(text)
        if self.editing is not None:
            msg = self.editing
            self.cancel(clear=False)
            self._clear()
            widgets.run(self, self.core.messaging.correct(chat, msg, text), error=_("Could not edit"))
            return
        if text.startswith("/") and not text.startswith("//") and self._command(chat, text):
            self._clear()
            return
        if text.startswith("//"):
            text = text[1:]
        reply = self.reply_to
        self.cancel(clear=False)
        self._clear()
        widgets.run(self, self.core.messaging.send_text(chat, text, reply), error=_("Could not send"))
        self.view.scroll_to_bottom()

    def _clear(self) -> None:
        self._loading = True
        self.buffer.set_text("")
        self._loading = False
        self._update_placeholder()
        self.popover.popdown()
        if self._pause_timer:
            GLib.source_remove(self._pause_timer)
            self._pause_timer = 0
        if self.chat is not None:
            self.core.chats.set_draft(self.chat, "")

    def _command(self, chat: ChatItem, text: str) -> bool:
        """Group commands; returns False to send the text as a message (/me, unknown commands)."""
        name, _sp, arg = text[1:].partition(" ")
        name = name.lower()
        arg = arg.strip()
        if name not in COMMANDS or not COMMANDS[name]:
            return False
        if COMMANDS[name] == "groupchat" and chat.type != "groupchat":
            compat.toast(self, _("/%s only works in groups") % name)
            return True
        muc = self.core.muc
        if name == "nick" and arg:
            widgets.run(self, muc.change_nick(chat, arg))
        elif name == "topic":
            widgets.run(self, muc.set_subject(chat, arg), error=_("Could not change the topic"))
        elif name == "invite" and arg:
            widgets.run(self, muc.invite(chat, arg.split()[0], " ".join(arg.split()[1:])),
                        done=lambda _r: compat.toast(self, _("Invited %s") % arg.split()[0]))
        elif name == "kick" and arg:
            nick, _s, reason = arg.partition(" ")
            widgets.run(self, muc.kick(chat, nick, reason), error=_("Could not remove %s") % nick)
        elif name == "ban" and arg:
            jid, _s, reason = arg.partition(" ")
            widgets.run(self, muc.ban(chat, jid, reason), error=_("Could not ban %s") % jid)
        elif name == "part":
            widgets.run(self, muc.leave(chat, arg))
            self.view.window.show_chat(None)
        else:
            compat.toast(self, _("Usage: /%s …") % name)
        return True

    # ================================================================== files
    def choose_files(self) -> None:
        dialog = Gtk.FileDialog(title=_("Send files"), modal=True)

        def done(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            paths_ = [files.get_item(i).get_path() for i in range(files.get_n_items())]
            self.send_files([p for p in paths_ if p])
        dialog.open_multiple(self.get_root(), None, done)

    def send_files(self, file_paths: list[str]) -> None:
        chat = self.chat
        if chat is None:
            return
        caption = ""
        for p in file_paths:
            widgets.run(self, self.core.files.send_file(chat, p, caption), error=_("Could not send %s") %
                        os.path.basename(p))
        self.view.scroll_to_bottom()

    def _on_paste(self, view) -> None:
        clip = view.get_clipboard()
        formats = clip.get_formats()
        if formats.contain_gtype(Gdk.Texture) and not formats.contain_mime_type("text/plain"):
            view.stop_emission_by_name("paste-clipboard")

            def got(c, res):
                try:
                    tex = c.read_texture_finish(res)
                except GLib.Error:
                    return
                if tex is None:
                    return
                folder = paths.ensure(os.path.join(paths.files_cache(), "pasted"))
                fd, path = tempfile.mkstemp(prefix="pasted-", suffix=".png", dir=folder)
                os.close(fd)
                tex.save_to_png(path)
                self._confirm_image(path)
            clip.read_texture_async(None, got)

    def _confirm_image(self, path: str) -> None:
        pic = Gtk.Picture.new_for_filename(path)
        pic.set_size_request(260, 200)
        pic.set_can_shrink(True)
        compat.alert(self, _("Send this image?"), "", [("cancel", _("Cancel"), ""), ("send", _("Send"), "suggested")],
                     lambda r: self.send_files([path]) if r == "send" else os.unlink(path), default="send", extra=pic)
