"""Which messages notify, and what the notification says.

The core decides; the interface only turns a Notification into a Gio.Notification (one per chat, replaced
instead of piling up).  Rules: muted chats stay silent except for mentions; groups follow "all / mentions and
replies / none" (per group overridable); quiet hours silence pop-ups and sounds; text is hidden for encrypted
chats unless the user opts in; strangers' messages say "Message request"."""

from __future__ import annotations

import datetime as _dt
import re
import time
from dataclasses import dataclass

from gi.repository import GObject

from .. import schema, util
from ..i18n import _
from .markup import parse as parse_markup
from .markup import preview as markup_preview


@dataclass
class Notification:
    id: str
    chat_id: int
    account_id: int
    title: str
    body: str
    icon: str = ""
    sound: str = ""          # settings key of the sound, or "" for silence
    priority: str = "normal"  # normal | high
    is_request: bool = False


class Notifier(GObject.Object):
    __gsignals__ = {
        "show-notification": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "withdraw-notification": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    def __init__(self, core):
        super().__init__()
        self.core = core
        self._pending: dict[int, int] = {}       # chat id -> messages folded into its notification
        core.messaging.connect("received", self._on_received)

    @property
    def settings(self):
        return self.core.settings

    def quiet_now(self) -> bool:
        now = _dt.datetime.now()
        return schema.in_time_range(self.settings["notify.quiet_hours"], now.hour * 60 + now.minute)

    def mentions_me(self, chat, body: str, my_nick: str, pm=None) -> bool:
        if chat.type != "groupchat":
            return False
        words = list(self.settings["notify.highlight_words"])
        if my_nick:
            words.append(my_nick)
        for w in words:
            if w and re.search(r"(?<![\w@])@?%s(?!\w)" % re.escape(w), body, re.IGNORECASE):
                return True
        if pm is not None:
            acc = self.core.accounts.get(chat.account_id)
            own = "xmpp:" + (acc.jid if acc else "")
            for uri, _b, _e in pm.mentions:
                if uri == own or (my_nick and uri.endswith("/" + my_nick)):
                    return True
        return False

    def _group_policy(self, chat) -> str:
        if chat.notify in ("all", "mentions", "none"):
            return chat.notify
        return self.settings["notify.groups"]

    def _on_received(self, _svc, chat, msg, mode: str) -> None:
        if mode == "scrollback" or msg.outgoing or msg.kind == "system" or msg.retracted:
            return
        if not self.settings["notify.enabled"]:
            return
        if self.core.is_viewing(chat) and self.core.window_focused:
            return
        now = int(time.time() * 1000)
        replied_to_me = bool(msg.reply_target) and msg.extra.get("reply_to_me", False)
        important = msg.mentions_me or replied_to_me
        if chat.muted(now) and not important:
            return
        if chat.type == "groupchat":
            pol = self._group_policy(chat)
            if pol == "none" or (pol == "mentions" and not important):
                return
        elif chat.notify == "none":
            return
        if self.quiet_now():
            return
        self._pending[chat.id] = self._pending.get(chat.id, 0) + 1
        self.emit("show-notification", self.build(chat, msg, self._pending[chat.id], important))

    def build(self, chat, msg, count: int, important: bool) -> Notification:
        content = self.settings["notify.content"]
        chat_name = chat.name or util.localpart(chat.jid)
        if chat.type == "groupchat":
            title = "%s — %s" % (msg.sender_name or msg.sender, chat_name) if content != "nothing" else _("Ward")
        else:
            title = chat_name if content != "nothing" else _("Ward")
        if chat.is_request:
            title = _("Message request from %s") % chat_name if content != "nothing" else _("Message request")
        if content == "nothing":
            body = _("New message")
        elif content == "sender":
            body = _("New message") if count == 1 else _("%d new messages") % count
        elif msg.encryption and self.settings["notify.hide_encrypted"]:
            body = _("New encrypted message") if count == 1 else _("%d new encrypted messages") % count
        elif msg.attachments and not msg.body:
            body = "📎 " + (msg.attachments[0].name or _("File"))
        else:
            text = msg.body[4:] if msg.kind == "me" else msg.body
            try:
                body = markup_preview(parse_markup(text, msg.is_markdown), 200)
            except Exception:
                body = util.first_line(text, 200)
            if msg.kind == "me":
                body = "* %s %s" % (msg.sender_name, body)
            if count > 1:
                body = "%s (+%d)" % (body, count - 1)
        sound = ""
        if self.settings["notify.sounds"]:
            sound = "notify.sound_request" if chat.is_request else (
                "notify.sound_mention" if important else "notify.sound_message")
        return Notification(id="chat-%d" % chat.id, chat_id=chat.id, account_id=chat.account_id, title=title,
                            body=body, icon=chat.avatar, sound=sound, priority="high" if important else "normal",
                            is_request=chat.is_request)

    def chat_read(self, chat) -> None:
        if self._pending.pop(chat.id, None):
            self.emit("withdraw-notification", "chat-%d" % chat.id)
