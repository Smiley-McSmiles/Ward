"""The objects the interface binds to: GObject items with properties (so rows update through notify::) in
Gio.ListStore lists.  No Gtk here: the core stays testable headless."""

from __future__ import annotations

import bisect
import json
from typing import Any

from gi.repository import Gio, GObject

from .. import util
from . import colors

INT64 = GObject.TYPE_INT64


def _prop(type_=str, default=None, **kw):
    if type_ is str:
        return GObject.Property(type=str, default=default or "", **kw)
    if type_ is bool:
        return GObject.Property(type=bool, default=bool(default), **kw)
    if type_ is int:
        return GObject.Property(type=int, default=default or 0, minimum=-(2**31), maximum=2**31 - 1, **kw)
    if type_ == "int64":
        return GObject.Property(type=INT64, default=default or 0, **kw)
    if type_ is float:
        return GObject.Property(type=float, default=default or 0.0, **kw)
    return GObject.Property(type=GObject.TYPE_PYOBJECT, **kw)


class Item(GObject.Object):
    """Base: set several properties at once, only notifying for real changes."""

    def update(self, **values: Any) -> list[str]:
        changed = []
        self.freeze_notify()
        try:
            for k, v in values.items():
                if getattr(self, k) != v:
                    setattr(self, k, v)
                    changed.append(k)
        finally:
            self.thaw_notify()
        return changed


class AccountItem(Item):
    __gtype_name__ = "WardAccount"
    id = _prop(int)
    jid = _prop(str)
    label = _prop(str)
    color = _prop(str)
    enabled = _prop(bool, True)
    state = _prop(str, "offline")         # offline | connecting | online | error | disabled | locked
    step = _prop(str)                     # what connecting is doing right now, for the progress text
    error = _prop(str)
    error_kind = _prop(str)               # auth | certificate | network | password-needed | ...
    show = _prop(str, "available")        # the presence the user chose: available | away | dnd | xa | offline
    status = _prop(str)
    nickname = _prop(str)
    avatar = _prop(str)                   # cached file path
    is_admin = _prop(bool)
    can_upload = _prop(bool)
    upload_limit = _prop("int64")
    has_mam = _prop(bool)
    omemo_ready = _prop(bool)
    unread = _prop(int)

    def __init__(self, **kw):
        super().__init__()
        self.options: dict[str, Any] = {}
        for k, v in kw.items():
            setattr(self, k, v)

    @property
    def display_name(self) -> str:
        return self.label or self.nickname or self.jid

    def option(self, key: str) -> Any:
        from .. import schema

        return self.options.get(key, schema.account_default(key))

    def __repr__(self) -> str:
        return "<Account %d %s %s>" % (self.id, self.jid, self.state)


SHOW_ORDER = {"chat": 0, "available": 0, "away": 2, "xa": 3, "dnd": 1, "offline": 4, "error": 5}


class ContactItem(Item):
    __gtype_name__ = "WardContact"
    id = _prop(int)
    account_id = _prop(int)
    jid = _prop(str)
    roster_name = _prop(str)
    nickname = _prop(str)
    pep_nick = _prop(str)
    groups_text = _prop(str)
    subscription = _prop(str, "none")
    ask = _prop(str)
    in_roster = _prop(bool, True)
    show = _prop(str, "offline")
    status = _prop(str)
    avatar = _prop(str)
    avatar_hash = _prop(str)
    blocked = _prop(bool)
    is_gateway = _prop(bool)
    last_seen = _prop("int64")
    idle_since = _prop("int64")
    name = _prop(str)                    # the display name, kept up to date by refresh_name()

    def __init__(self, **kw):
        super().__init__()
        self.groups: list[str] = []
        self.resources: dict[str, dict] = {}   # resource -> {show, status, priority, caps}
        for k, v in kw.items():
            setattr(self, k, v)
        self.refresh_name()

    def refresh_name(self) -> None:
        n = self.nickname or self.roster_name or self.pep_nick or util.localpart(self.jid) or self.jid
        if self.name != n:
            self.name = n

    def set_groups(self, groups: list[str]) -> None:
        self.groups = sorted(set(groups))
        self.groups_text = ", ".join(self.groups)

    @property
    def color(self) -> str:
        return colors.hex_color(self.jid)

    def best_presence(self) -> tuple[str, str]:
        if not self.resources:
            return "offline", ""
        best = sorted(self.resources.values(), key=lambda r: (-r.get("priority", 0),
                                                              SHOW_ORDER.get(r.get("show", "available"), 9)))[0]
        return best.get("show", "available"), best.get("status", "")

    @property
    def sees_us(self) -> bool:
        return self.subscription in ("from", "both")

    @property
    def we_see(self) -> bool:
        return self.subscription in ("to", "both")


class ChatItem(Item):
    __gtype_name__ = "WardChat"
    id = _prop(int)
    account_id = _prop(int)
    jid = _prop(str)
    type = _prop(str, "chat")            # chat | groupchat | pm
    name = _prop(str)
    pinned = _prop(bool)
    muted_until = _prop("int64")
    archived = _prop(bool)
    open = _prop(bool, True)
    is_request = _prop(bool)
    draft = _prop(str)
    unread = _prop(int)
    mentions = _prop(int)
    last_activity = _prop("int64")
    last_read_id = _prop("int64")
    preview = _prop(str)
    preview_sender = _prop(str)
    typing = _prop(str)                  # "Alex" or "Alex, Sam" while typing
    encryption = _prop(str, "default")   # the chat's override: default | omemo | pgp | off
    notify = _prop(str, "default")
    folder = _prop(str)
    avatar = _prop(str)
    show = _prop(str, "offline")         # for 1:1 chats: the contact's presence
    subject = _prop(str)                 # group topic
    member_count = _prop(int)
    joined = _prop(bool)                 # groups: currently in the room
    join_error = _prop(str)
    our_nick = _prop(str)
    our_role = _prop(str)
    our_affiliation = _prop(str)
    encryptable = _prop(bool)            # groups: members-only and non-anonymous
    encryption_state = _prop(str)        # what will actually happen: omemo | omemo-legacy | pgp | plain | blocked
    encryption_reason = _prop(str)       # why, in plain words, when it is not the expected encryption
    new_devices = _prop(int)             # undecided OMEMO devices of this contact (banner)

    def __init__(self, **kw):
        super().__init__()
        self.options: dict[str, Any] = {}
        for k, v in kw.items():
            setattr(self, k, v)

    @property
    def is_group(self) -> bool:
        return self.type == "groupchat"

    @property
    def color(self) -> str:
        return colors.hex_color(self.jid)

    def muted(self, now_ms: int) -> bool:
        return self.muted_until == -1 or self.muted_until > now_ms

    def options_json(self) -> str:
        return json.dumps(self.options, sort_keys=True)

    def __repr__(self) -> str:
        return "<Chat %d %s %s>" % (self.id, self.type, self.jid)


class AttachmentItem(Item):
    __gtype_name__ = "WardAttachment"
    id = _prop(int)
    url = _prop(str)
    name = _prop(str)
    mime = _prop(str)
    size = _prop("int64")
    width = _prop(int)
    height = _prop(int)
    duration = _prop(int)
    local_path = _prop(str)
    thumbnail = _prop(str)
    state = _prop(str, "remote")         # remote | downloading | done | failed | uploading
    progress = _prop(float)
    encrypted = _prop(bool)
    error = _prop(str)

    def __init__(self, **kw):
        super().__init__()
        self.aes_key = ""
        self.aes_iv = ""
        self.sha256 = ""
        for k, v in kw.items():
            setattr(self, k, v)

    @property
    def is_image(self) -> bool:
        return self.mime.startswith("image/")

    @property
    def is_audio(self) -> bool:
        return self.mime.startswith("audio/")

    @property
    def is_video(self) -> bool:
        return self.mime.startswith("video/")


STATUS_FAILED, STATUS_PENDING, STATUS_SENT, STATUS_DELIVERED, STATUS_READ = -1, 0, 1, 2, 3


class MessageItem(Item):
    __gtype_name__ = "WardMessage"
    id = _prop("int64")
    chat_id = _prop(int)
    stanza_id = _prop(str)
    origin_id = _prop(str)
    msg_id = _prop(str)
    sender = _prop(str)
    sender_jid = _prop(str)
    sender_name = _prop(str)
    occupant_id = _prop(str)
    direction = _prop(int)               # 0 received, 1 sent
    timestamp = _prop("int64")
    body = _prop(str)
    is_markdown = _prop(bool)
    kind = _prop(str, "text")            # text | me | file | status | system
    encryption = _prop(str)              # '' | omemo | omemo2 | pgp
    sender_device = _prop("int64")
    trust = _prop(str)                   # verified | trusted | undecided | distrusted | '' (at receipt)
    edited = _prop(bool)
    retracted = _prop(int)               # 0 no, 1 for everyone, 2 for me, 3 by a moderator
    reply_to = _prop(str)
    reply_preview = _prop(str)
    reply_author = _prop(str)
    reply_target = _prop("int64")        # local id of the replied-to message when we have it
    status = _prop(int)
    error = _prop(str)
    reactions_rev = _prop(int)           # bumped when reactions change, so rows redraw
    attachments_rev = _prop(int)
    mentions_me = _prop(bool)
    selected = _prop(bool)

    def __init__(self, **kw):
        super().__init__()
        self.reactions: list[tuple[str, list[str], bool]] = []   # (emoji, sender names, includes me)
        self.attachments: list[AttachmentItem] = []
        self.extra: dict[str, Any] = {}
        for k, v in kw.items():
            setattr(self, k, v)

    @property
    def outgoing(self) -> bool:
        return self.direction == 1

    def ref_ids(self) -> set[str]:
        return {x for x in (self.stanza_id, self.origin_id, self.msg_id) if x}

    def sort_key(self) -> tuple[int, int]:
        return (self.timestamp, self.id)

    def __repr__(self) -> str:
        return "<Message %d chat=%d %s %r>" % (self.id, self.chat_id, "out" if self.outgoing else "in",
                                               self.body[:30])


class DeviceItem(Item):
    """An OMEMO device (ours or a contact's) or an OpenPGP key, for the trust pages."""
    __gtype_name__ = "WardDevice"
    jid = _prop(str)
    device_id = _prop("int64")
    fingerprint = _prop(str)             # hex
    label = _prop(str)
    trust = _prop(str)                   # verified | trusted | undecided | distrusted
    active = _prop(bool, True)
    inactive = _prop(bool)
    own = _prop(bool)
    this_device = _prop(bool)
    versions = _prop(str)                # "OMEMO 2, legacy"
    first_seen = _prop("int64")
    last_used = _prop("int64")
    kind = _prop(str, "omemo")           # omemo | pgp

    def __init__(self, **kw):
        super().__init__()
        for k, v in kw.items():
            setattr(self, k, v)


class OccupantItem(Item):
    __gtype_name__ = "WardOccupant"
    nick = _prop(str)
    real_jid = _prop(str)
    occupant_id = _prop(str)
    role = _prop(str, "participant")
    affiliation = _prop(str, "none")
    show = _prop(str, "available")
    status = _prop(str)
    avatar = _prop(str)
    hats = _prop(str)
    is_self = _prop(bool)

    def __init__(self, **kw):
        super().__init__()
        for k, v in kw.items():
            setattr(self, k, v)

    @property
    def color(self) -> str:
        return colors.hex_color(self.nick)


ROLE_ORDER = {"moderator": 0, "participant": 1, "visitor": 2, "none": 3}
AFFILIATION_ORDER = {"owner": 0, "admin": 1, "member": 2, "none": 3, "outcast": 4}


def new_store(item_type) -> Gio.ListStore:
    return Gio.ListStore.new(item_type)


def store_items(store: Gio.ListStore) -> list:
    return [store.get_item(i) for i in range(store.get_n_items())]


def store_find(store: Gio.ListStore, pred) -> tuple[int, Any] | tuple[None, None]:
    for i in range(store.get_n_items()):
        it = store.get_item(i)
        if pred(it):
            return i, it
    return None, None


class MessageList(GObject.Object):
    """The messages of one open chat, oldest first, loaded page by page from the database (and the server
    archive when we reach the start of local history).  Lives as long as the chat view needs it."""
    __gtype_name__ = "WardMessageList"
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_LAST, None, ())}
    loading = _prop(bool)
    has_older = _prop(bool, True)

    def __init__(self, chat: ChatItem):
        super().__init__()
        self.chat = chat
        self.store = Gio.ListStore.new(MessageItem)
        self.by_id: dict[int, MessageItem] = {}
        self._keys: list[tuple[int, int]] = []

    def __len__(self) -> int:
        return self.store.get_n_items()

    def items(self) -> list[MessageItem]:
        return store_items(self.store)

    def add(self, msg: MessageItem) -> int:
        if msg.id in self.by_id:
            return -1
        key = msg.sort_key()
        pos = bisect.bisect_right(self._keys, key)
        self._keys.insert(pos, key)
        self.store.insert(pos, msg)
        self.by_id[msg.id] = msg
        return pos

    def add_many(self, msgs: list[MessageItem]) -> None:
        fresh = [m for m in msgs if m.id not in self.by_id]
        if not fresh:
            return
        fresh.sort(key=MessageItem.sort_key)
        if not self._keys or fresh[-1].sort_key() <= self._keys[0]:
            # the common case: a page of older history goes in front in one splice
            self.store.splice(0, 0, fresh)
            self._keys[0:0] = [m.sort_key() for m in fresh]
            for m in fresh:
                self.by_id[m.id] = m
        else:
            for m in fresh:
                self.add(m)

    def remove(self, msg_id: int) -> None:
        m = self.by_id.pop(msg_id, None)
        if m is None:
            return
        pos = bisect.bisect_left(self._keys, m.sort_key())
        if pos < len(self._keys) and self.store.get_item(pos) is m:
            self.store.remove(pos)
            del self._keys[pos]

    def trim(self, keep: int) -> None:
        """Forget all but the newest messages (the chat is no longer shown; older ones load again when scrolled
        to), so the history someone scrolled back through does not stay in memory."""
        extra = len(self) - keep
        if extra <= 0:
            return
        for m in self.items()[:extra]:
            self.by_id.pop(m.id, None)
        self.store.splice(0, extra, [])
        del self._keys[:extra]
        self.has_older = True

    def get(self, msg_id: int) -> MessageItem | None:
        return self.by_id.get(msg_id)

    def index(self, msg: MessageItem) -> int:
        pos = bisect.bisect_left(self._keys, msg.sort_key())
        if pos < len(self._keys) and self.store.get_item(pos) is msg:
            return pos
        for i in range(len(self)):
            if self.store.get_item(i) is msg:
                return i
        return -1

    def previous(self, msg: MessageItem) -> MessageItem | None:
        i = self.index(msg)
        return self.store.get_item(i - 1) if i > 0 else None

    def find_ref(self, ref: str) -> MessageItem | None:
        for m in reversed(self.items()):
            if ref in m.ref_ids():
                return m
        return None

    @property
    def oldest(self) -> MessageItem | None:
        return self.store.get_item(0) if len(self) else None

    @property
    def newest(self) -> MessageItem | None:
        n = len(self)
        return self.store.get_item(n - 1) if n else None
