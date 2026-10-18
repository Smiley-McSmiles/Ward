"""Message stanzas, parsed and built on the raw XML.

Ward does not use slixmpp's per-XEP message plugins: their interfaces changed between slixmpp 1.8 (what
Debian and Ubuntu ship) and 1.17, and decrypted OMEMO 2 content (SCE) must go through the very same code as
plain messages anyway.  parse() turns any <message/> element into a ParsedMessage; the add_* functions append
extension elements to an outgoing one.  Everything here is pure ElementTree and unit tested on its own."""

from __future__ import annotations

import base64
import datetime as _dt
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from .. import NS_MARKDOWN

NS_CLIENT = "jabber:client"
NS_SID = "urn:xmpp:sid:0"
NS_RECEIPTS = "urn:xmpp:receipts"
NS_MARKERS = "urn:xmpp:chat-markers:0"
NS_CHATSTATES = "http://jabber.org/protocol/chatstates"
NS_CORRECT = "urn:xmpp:message-correct:0"
NS_RETRACT = "urn:xmpp:message-retract:1"
NS_RETRACT_0 = "urn:xmpp:message-retract:0"
NS_FASTEN = "urn:xmpp:fasten:0"
NS_MODERATE = "urn:xmpp:message-moderate:1"
NS_MODERATE_0 = "urn:xmpp:message-moderate:0"
NS_REACTIONS = "urn:xmpp:reactions:0"
NS_REPLY = "urn:xmpp:reply:0"
NS_FALLBACK = "urn:xmpp:fallback:0"
NS_HINTS = "urn:xmpp:hints"
NS_CARBONS = "urn:xmpp:carbons:2"
NS_FORWARD = "urn:xmpp:forward:0"
NS_DELAY = "urn:xmpp:delay"
NS_MAM = "urn:xmpp:mam:2"
NS_OOB = "jabber:x:oob"
NS_SFS = "urn:xmpp:sfs:0"
NS_FILE = "urn:xmpp:file:metadata:0"
NS_HASHES = "urn:xmpp:hashes:2"
NS_THUMBS = "urn:xmpp:thumbs:1"
NS_URL_DATA = "http://jabber.org/protocol/url-data"
NS_ESFS = "urn:xmpp:esfs:0"
NS_OCCUPANT_ID = "urn:xmpp:occupant-id:0"
NS_REFERENCE = "urn:xmpp:reference:0"
NS_MUC_USER = "http://jabber.org/protocol/muc#user"
NS_CONFERENCE = "jabber:x:conference"
NS_EME = "urn:xmpp:eme:0"
NS_OMEMO2 = "urn:xmpp:omemo:2"
NS_OMEMO = "eu.siacs.conversations.axolotl"
NS_OPENPGP = "urn:xmpp:openpgp:0"
NS_SPOILER = "urn:xmpp:spoiler:0"
NS_STANZAS = "urn:ietf:params:xml:ns:xmpp-stanzas"
NS_NICK = "http://jabber.org/protocol/nick"

# Elements that are "content" for Stanza Content Encryption (they go inside the OMEMO 2 envelope)
SCE_CONTENT = {
    "{%s}body" % NS_CLIENT, "{%s}subject" % NS_CLIENT, "{%s}thread" % NS_CLIENT,
    "{%s}replace" % NS_CORRECT, "{%s}retract" % NS_RETRACT, "{%s}reactions" % NS_REACTIONS,
    "{%s}reply" % NS_REPLY, "{%s}fallback" % NS_FALLBACK, "{%s}x" % NS_OOB, "{%s}file-sharing" % NS_SFS,
    "{%s}markdown" % NS_MARKDOWN, "{%s}reference" % NS_REFERENCE, "{%s}spoiler" % NS_SPOILER,
    "{%s}active" % NS_CHATSTATES, "{%s}composing" % NS_CHATSTATES, "{%s}paused" % NS_CHATSTATES,
    "{%s}inactive" % NS_CHATSTATES, "{%s}gone" % NS_CHATSTATES,
    "{%s}markable" % NS_MARKERS, "{%s}displayed" % NS_MARKERS, "{%s}received" % NS_MARKERS,
    "{%s}x" % NS_CONFERENCE, "{%s}nick" % NS_NICK,
}

CHAT_STATES = ("active", "composing", "paused", "inactive", "gone")


def q(ns: str, tag: str) -> str:
    return "{%s}%s" % (ns, tag)


def _text(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    return el.text or ""


def parse_stamp(stamp: str | None) -> float | None:
    """XEP-0082 date-time -> Unix seconds."""
    if not stamp:
        return None
    s = stamp.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        # fractional seconds with more than 6 digits
        try:
            head, _, frac = s.partition(".")
            tz = ""
            for sign in ("+", "-"):
                if sign in frac:
                    frac, _, tzrest = frac.partition(sign)
                    tz = sign + tzrest
            d = _dt.datetime.fromisoformat(head + ("." + frac[:6] if frac else "") + tz)
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.UTC)
    return d.timestamp()


def format_stamp(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class FileInfo:
    url: str = ""
    name: str = ""
    mime: str = ""
    size: int | None = None
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    sha256: str = ""               # hex
    desc: str = ""
    thumbnail: bytes | None = None
    thumbnail_mime: str = ""
    encrypted: bool = False

    def to_json(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if v not in (None, "", False) and k != "thumbnail"}
        return d


@dataclass
class MucUser:
    status: set[int] = field(default_factory=set)
    invite_from: str = ""
    invite_reason: str = ""
    password: str = ""
    declined_from: str = ""
    jid: str = ""                  # real JID in presence items
    affiliation: str = ""
    role: str = ""
    nick: str = ""                 # new nick on 303
    destroyed: bool = False
    destroy_reason: str = ""
    alternate: str = ""


@dataclass
class Moderation:
    target: str
    by: str = ""
    reason: str = ""
    occupant_id: str = ""


@dataclass
class Wrapper:
    kind: str                      # carbon-received | carbon-sent | mam
    delay: float | None = None
    queryid: str = ""
    archive_id: str = ""
    from_jid: str = ""             # who sent the wrapper (must be our bare JID / the room; checked by callers)


@dataclass
class ParsedMessage:
    type: str = "normal"
    from_jid: str = ""
    to_jid: str = ""
    id: str = ""
    body: str | None = None
    subject: str | None = None
    thread: str = ""
    origin_id: str = ""
    stanza_ids: dict[str, str] = field(default_factory=dict)
    delay: float | None = None
    delay_from: str = ""
    receipt_request: bool = False
    receipt_for: str = ""
    markable: bool = False
    marker: tuple[str, str] | None = None
    chat_state: str = ""
    correction_of: str = ""
    retraction_of: str = ""
    moderation: Moderation | None = None
    reactions: tuple[str, list[str]] | None = None
    reply_to: tuple[str, str] | None = None        # (jid, id)
    fallbacks: list[tuple[str, list[tuple[int, int]]]] = field(default_factory=list)
    oob: list[tuple[str, str]] = field(default_factory=list)
    files: list[FileInfo] = field(default_factory=list)
    occupant_id: str = ""
    mentions: list[tuple[str, int, int]] = field(default_factory=list)
    muc_user: MucUser | None = None
    direct_invite: tuple[str, str, str] | None = None   # (room, password, reason)
    eme: str = ""
    omemo2: ET.Element | None = None
    omemo: ET.Element | None = None
    openpgp: ET.Element | None = None
    markdown: bool = False
    hints: set[str] = field(default_factory=set)
    error: tuple[str, str] | None = None
    spoiler: str | None = None
    nick: str = ""
    wrapper: Wrapper | None = None

    @property
    def encrypted(self) -> bool:
        return self.omemo2 is not None or self.omemo is not None or self.openpgp is not None

    @property
    def is_me(self) -> bool:
        return (self.body or "").startswith("/me ")

    def display_body(self, supported_fallbacks: tuple[str, ...] = (NS_REPLY, NS_RETRACT, NS_RETRACT_0,
                                                                   NS_REACTIONS, NS_OOB, NS_SFS)) -> str:
        """The body with fallback parts for features we understand removed (a reply's quote, a retraction's
        "this message was deleted" text...)."""
        body = self.body or ""
        ranges: list[tuple[int, int]] = []
        for for_ns, rs in self.fallbacks:
            if for_ns not in supported_fallbacks:
                continue
            if not rs:
                return ""
            ranges.extend(rs)
        if not ranges:
            return body
        out, pos = [], 0
        for start, end in sorted(ranges):
            start, end = max(start, pos), min(end, len(body))
            if start > pos:
                out.append(body[pos:start])
            pos = max(pos, end)
        out.append(body[pos:])
        return "".join(out).strip("\n") if ranges else body

    @property
    def has_content(self) -> bool:
        return bool((self.body or "").strip()) or bool(self.files) or bool(self.oob)


def unwrap(el: ET.Element) -> tuple[ET.Element, Wrapper | None]:
    """Carbons (XEP-0280) and archive results (XEP-0313) carry the real message in <forwarded/>."""
    for kind in ("received", "sent"):
        c = el.find(q(NS_CARBONS, kind))
        if c is not None:
            fwd = c.find(q(NS_FORWARD, "forwarded"))
            inner = fwd.find(q(NS_CLIENT, "message")) if fwd is not None else None
            if inner is not None:
                d = fwd.find(q(NS_DELAY, "delay")) if fwd is not None else None
                return inner, Wrapper("carbon-" + kind, parse_stamp(d.get("stamp")) if d is not None else None,
                                      from_jid=el.get("from", ""))
    r = el.find(q(NS_MAM, "result"))
    if r is not None:
        fwd = r.find(q(NS_FORWARD, "forwarded"))
        inner = fwd.find(q(NS_CLIENT, "message")) if fwd is not None else None
        if inner is not None:
            d = fwd.find(q(NS_DELAY, "delay")) if fwd is not None else None
            return inner, Wrapper("mam", parse_stamp(d.get("stamp")) if d is not None else None,
                                  queryid=r.get("queryid", ""), archive_id=r.get("id", ""),
                                  from_jid=el.get("from", ""))
    return el, None


def _int(v: str | None) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


def parse_file(fs: ET.Element) -> FileInfo:
    """XEP-0447 <file-sharing/> with XEP-0446 metadata."""
    info = FileInfo()
    f = fs.find(q(NS_FILE, "file"))
    if f is not None:
        info.name = _text(f.find(q(NS_FILE, "name"))) or ""
        info.mime = _text(f.find(q(NS_FILE, "media-type"))) or ""
        info.size = _int(_text(f.find(q(NS_FILE, "size"))))
        info.width = _int(_text(f.find(q(NS_FILE, "width"))))
        info.height = _int(_text(f.find(q(NS_FILE, "height"))))
        info.duration = _int(_text(f.find(q(NS_FILE, "length"))))
        info.desc = _text(f.find(q(NS_FILE, "desc"))) or ""
        for h in f.findall(q(NS_HASHES, "hash")):
            if h.get("algo") == "sha-256" and h.text:
                try:
                    info.sha256 = base64.b64decode(h.text.strip()).hex()
                except ValueError:
                    pass
        th = f.find(q(NS_THUMBS, "thumbnail"))
        if th is not None:
            uri = th.get("uri", "")
            info.thumbnail_mime = th.get("media-type", "")
            if uri.startswith("data:") and "," in uri:
                meta, _, data = uri[5:].partition(",")
                try:
                    info.thumbnail = base64.b64decode(data) if meta.endswith(";base64") else None
                    info.thumbnail_mime = info.thumbnail_mime or meta.split(";")[0]
                except ValueError:
                    pass
    sources = fs.find(q(NS_SFS, "sources"))
    if sources is not None:
        u = sources.find(q(NS_URL_DATA, "url-data"))
        if u is not None:
            info.url = u.get("target", "")
        enc = sources.find(q(NS_ESFS, "encrypted"))
        if enc is not None:
            info.encrypted = True
            u = enc.find(".//" + q(NS_URL_DATA, "url-data"))
            if u is not None and not info.url:
                info.url = u.get("target", "")
    return info


def parse(el: ET.Element) -> ParsedMessage:
    inner, wrapper = unwrap(el)
    m = ParsedMessage(type=inner.get("type") or "normal", from_jid=inner.get("from", ""), to_jid=inner.get("to", ""),
                      id=inner.get("id", ""), wrapper=wrapper)
    m.body = _text(inner.find(q(NS_CLIENT, "body")))
    m.subject = _text(inner.find(q(NS_CLIENT, "subject")))
    m.thread = _text(inner.find(q(NS_CLIENT, "thread"))) or ""
    for child in inner:
        tag = child.tag
        if tag == q(NS_SID, "stanza-id"):
            by = child.get("by", "")
            if by and child.get("id"):
                m.stanza_ids[by.lower()] = child.get("id", "")
        elif tag == q(NS_SID, "origin-id"):
            m.origin_id = child.get("id", "")
        elif tag == q(NS_DELAY, "delay"):
            m.delay = parse_stamp(child.get("stamp"))
            m.delay_from = child.get("from", "")
        elif tag == q(NS_RECEIPTS, "request"):
            m.receipt_request = True
        elif tag == q(NS_RECEIPTS, "received"):
            m.receipt_for = child.get("id", "")
        elif tag == q(NS_MARKERS, "markable"):
            m.markable = True
        elif tag in (q(NS_MARKERS, "received"), q(NS_MARKERS, "displayed"), q(NS_MARKERS, "acknowledged")):
            m.marker = (tag.split("}")[1], child.get("id", ""))
        elif tag.startswith("{%s}" % NS_CHATSTATES):
            st = tag.split("}")[1]
            if st in CHAT_STATES:
                m.chat_state = st
        elif tag == q(NS_CORRECT, "replace"):
            m.correction_of = child.get("id", "")
        elif tag == q(NS_RETRACT, "retract"):
            mod = child.find(q(NS_MODERATE, "moderated"))
            if mod is not None:
                occ = mod.find(q(NS_OCCUPANT_ID, "occupant-id"))
                m.moderation = Moderation(child.get("id", ""), mod.get("by", ""),
                                          _text(child.find(q(NS_RETRACT, "reason"))) or "",
                                          occ.get("id", "") if occ is not None else "")
            else:
                m.retraction_of = child.get("id", "")
        elif tag == q(NS_FASTEN, "apply-to"):
            target = child.get("id", "")
            if child.find(q(NS_RETRACT_0, "retract")) is not None:
                m.retraction_of = target
            mod = child.find(q(NS_MODERATE_0, "moderated"))
            if mod is not None and mod.find(q(NS_RETRACT_0, "retract")) is not None:
                occ = mod.find(q(NS_OCCUPANT_ID, "occupant-id"))
                m.moderation = Moderation(target, mod.get("by", ""), _text(mod.find(q(NS_MODERATE_0, "reason")))
                                          or "", occ.get("id", "") if occ is not None else "")
        elif tag == q(NS_REACTIONS, "reactions"):
            emojis = [(r.text or "").strip() for r in child.findall(q(NS_REACTIONS, "reaction"))]
            m.reactions = (child.get("id", ""), [e for e in emojis if e])
        elif tag == q(NS_REPLY, "reply"):
            m.reply_to = (child.get("to", ""), child.get("id", ""))
        elif tag == q(NS_FALLBACK, "fallback"):
            ranges = []
            for b in child.findall(q(NS_FALLBACK, "body")):
                s, e = _int(b.get("start")), _int(b.get("end"))
                if s is not None and e is not None and 0 <= s <= e:
                    ranges.append((s, e))
            m.fallbacks.append((child.get("for", ""), ranges))
        elif tag == q(NS_HINTS, "store") or tag == q(NS_HINTS, "no-store") or tag == q(NS_HINTS, "no-copy") \
                or tag == q(NS_HINTS, "no-permanent-store"):
            m.hints.add(tag.split("}")[1])
        elif tag == q(NS_OOB, "x"):
            url = _text(child.find(q(NS_OOB, "url"))) or ""
            if url.strip():
                m.oob.append((url.strip(), _text(child.find(q(NS_OOB, "desc"))) or ""))
        elif tag == q(NS_SFS, "file-sharing"):
            m.files.append(parse_file(child))
        elif tag == q(NS_OCCUPANT_ID, "occupant-id"):
            m.occupant_id = child.get("id", "")
        elif tag == q(NS_REFERENCE, "reference"):
            if child.get("type") == "mention":
                begin, end = _int(child.get("begin")), _int(child.get("end"))
                m.mentions.append((child.get("uri", ""), begin or 0, end or 0))
        elif tag == q(NS_MUC_USER, "x"):
            m.muc_user = parse_muc_user(child)
        elif tag == q(NS_CONFERENCE, "x"):
            m.direct_invite = (child.get("jid", ""), child.get("password", ""), child.get("reason", ""))
        elif tag == q(NS_EME, "encryption"):
            m.eme = child.get("namespace", "")
        elif tag == q(NS_OMEMO2, "encrypted"):
            m.omemo2 = child
        elif tag == q(NS_OMEMO, "encrypted"):
            m.omemo = child
        elif tag == q(NS_OPENPGP, "openpgp"):
            m.openpgp = child
        elif tag == q(NS_MARKDOWN, "markdown"):
            m.markdown = True
        elif tag == q(NS_SPOILER, "spoiler"):
            m.spoiler = child.text or ""
        elif tag == q(NS_NICK, "nick"):
            m.nick = child.text or ""
        elif tag == q(NS_CLIENT, "error"):
            m.error = parse_error(child)
    if m.type == "error" and m.error is None:
        m.error = ("undefined-condition", "")
    return m


def parse_error(err: ET.Element) -> tuple[str, str]:
    cond = ""
    text = ""
    for c in err:
        if c.tag.startswith("{%s}" % NS_STANZAS):
            name = c.tag.split("}")[1]
            if name == "text":
                text = c.text or ""
            else:
                cond = name
    return cond or "undefined-condition", text


def parse_muc_user(x: ET.Element) -> MucUser:
    mu = MucUser()
    for s in x.findall(q(NS_MUC_USER, "status")):
        code = _int(s.get("code"))
        if code is not None:
            mu.status.add(code)
    inv = x.find(q(NS_MUC_USER, "invite"))
    if inv is not None:
        mu.invite_from = inv.get("from", "")
        mu.invite_reason = _text(inv.find(q(NS_MUC_USER, "reason"))) or ""
    pw = x.find(q(NS_MUC_USER, "password"))
    if pw is not None:
        mu.password = pw.text or ""
    dec = x.find(q(NS_MUC_USER, "decline"))
    if dec is not None:
        mu.declined_from = dec.get("from", "")
    item = x.find(q(NS_MUC_USER, "item"))
    if item is not None:
        mu.jid = item.get("jid", "")
        mu.affiliation = item.get("affiliation", "")
        mu.role = item.get("role", "")
        mu.nick = item.get("nick", "")
    d = x.find(q(NS_MUC_USER, "destroy"))
    if d is not None:
        mu.destroyed = True
        mu.destroy_reason = _text(d.find(q(NS_MUC_USER, "reason"))) or ""
        mu.alternate = d.get("jid", "")
    return mu


# ================================================================================================ building
def _sub(el: ET.Element, ns: str, tag: str, attrs: dict | None = None, text: str | None = None) -> ET.Element:
    child = ET.SubElement(el, q(ns, tag), attrs or {})
    if text is not None:
        child.text = text
    return child


def set_body(el: ET.Element, text: str) -> None:
    for b in el.findall(q(NS_CLIENT, "body")):
        el.remove(b)
    _sub(el, NS_CLIENT, "body", text=text)


def add_origin_id(el: ET.Element, oid: str) -> None:
    _sub(el, NS_SID, "origin-id", {"id": oid})


def add_receipt_request(el: ET.Element) -> None:
    _sub(el, NS_RECEIPTS, "request")


def add_receipt(el: ET.Element, msg_id: str) -> None:
    _sub(el, NS_RECEIPTS, "received", {"id": msg_id})


def add_markable(el: ET.Element) -> None:
    _sub(el, NS_MARKERS, "markable")


def add_marker(el: ET.Element, kind: str, msg_id: str) -> None:
    _sub(el, NS_MARKERS, kind, {"id": msg_id})


def add_chat_state(el: ET.Element, state: str) -> None:
    if state in CHAT_STATES:
        _sub(el, NS_CHATSTATES, state)


def add_correction(el: ET.Element, msg_id: str) -> None:
    _sub(el, NS_CORRECT, "replace", {"id": msg_id})


def add_hint(el: ET.Element, hint: str) -> None:
    _sub(el, NS_HINTS, hint)


def add_markdown_flag(el: ET.Element) -> None:
    _sub(el, NS_MARKDOWN, "markdown")


def add_eme(el: ET.Element, namespace: str, name: str) -> None:
    _sub(el, NS_EME, "encryption", {"namespace": namespace, "name": name})


def add_retraction(el: ET.Element, msg_id: str) -> None:
    """XEP-0424 version 1, plus the older fastening form and a fallback body for clients without either."""
    _sub(el, NS_RETRACT, "retract", {"id": msg_id})
    apply = _sub(el, NS_FASTEN, "apply-to", {"id": msg_id})
    _sub(apply, NS_RETRACT_0, "retract")
    _sub(el, NS_FALLBACK, "fallback", {"for": NS_RETRACT})
    set_body(el, "This person attempted to retract a previous message, but it's unsupported by your client.")
    add_hint(el, "store")


def add_reactions(el: ET.Element, msg_id: str, emojis: list[str]) -> None:
    r = _sub(el, NS_REACTIONS, "reactions", {"id": msg_id})
    for e in emojis:
        _sub(r, NS_REACTIONS, "reaction", text=e)
    add_hint(el, "store")


def reply_fallback(quoted_text: str, author: str = "", max_lines: int = 3) -> str:
    """The quote other clients show above a reply, ending with a newline."""
    lines = [x for x in (quoted_text or "").split("\n") if not x.startswith(">")]
    lines = [x for x in lines if x.strip()][:max_lines] or ["…"]
    lines = [x if len(x) <= 120 else x[:119] + "…" for x in lines]
    head = ["> %s:" % author] if author else []
    return "\n".join("> " + x if not x.startswith("> ") else x for x in head + lines) + "\n"


def add_reply(el: ET.Element, to_jid: str, msg_id: str, fallback: str = "") -> None:
    """Call after set_body with body = fallback + text; the fallback range covers the quote."""
    _sub(el, NS_REPLY, "reply", {"to": to_jid, "id": msg_id} if to_jid else {"id": msg_id})
    if fallback:
        fb = _sub(el, NS_FALLBACK, "fallback", {"for": NS_REPLY})
        _sub(fb, NS_FALLBACK, "body", {"start": "0", "end": str(len(fallback))})


def add_oob(el: ET.Element, url: str, desc: str = "") -> None:
    x = _sub(el, NS_OOB, "x")
    _sub(x, NS_OOB, "url", text=url)
    if desc:
        _sub(x, NS_OOB, "desc", text=desc)


def add_file_sharing(el: ET.Element, info: FileInfo, disposition: str = "inline") -> None:
    fs = _sub(el, NS_SFS, "file-sharing", {"disposition": disposition})
    f = _sub(fs, NS_FILE, "file")
    if info.mime:
        _sub(f, NS_FILE, "media-type", text=info.mime)
    if info.name:
        _sub(f, NS_FILE, "name", text=info.name)
    if info.size is not None:
        _sub(f, NS_FILE, "size", text=str(info.size))
    if info.width:
        _sub(f, NS_FILE, "width", text=str(info.width))
    if info.height:
        _sub(f, NS_FILE, "height", text=str(info.height))
    if info.duration:
        _sub(f, NS_FILE, "length", text=str(info.duration))
    if info.desc:
        _sub(f, NS_FILE, "desc", text=info.desc)
    if info.sha256:
        _sub(f, NS_HASHES, "hash", {"algo": "sha-256"}, base64.b64encode(bytes.fromhex(info.sha256)).decode())
    if info.thumbnail:
        mime = info.thumbnail_mime or "image/jpeg"
        attrs = {"uri": "data:%s;base64,%s" % (mime, base64.b64encode(info.thumbnail).decode()), "media-type": mime}
        _sub(f, NS_THUMBS, "thumbnail", attrs)
    src = _sub(fs, NS_SFS, "sources")
    _sub(src, NS_URL_DATA, "url-data", {"target": info.url})


def add_direct_invite(el: ET.Element, room: str, reason: str = "", password: str = "") -> None:
    attrs = {"jid": room}
    if reason:
        attrs["reason"] = reason
    if password:
        attrs["password"] = password
    _sub(el, NS_CONFERENCE, "x", attrs)


def moderation_request(msg_stanza_id: str, reason: str = "") -> list[ET.Element]:
    """Payloads for an <iq type='set' to='room'/> asking the room to retract a message (XEP-0425).  Both the
    current (v1, sent first) and the older fastening form are built; send the second if the first fails."""
    mod = ET.Element(q(NS_MODERATE, "moderate"), {"id": msg_stanza_id})
    ET.SubElement(mod, q(NS_RETRACT, "retract"))
    if reason:
        ET.SubElement(mod, q(NS_MODERATE, "reason")).text = reason
    old = ET.Element(q(NS_FASTEN, "apply-to"), {"id": msg_stanza_id})
    om = ET.SubElement(old, q(NS_MODERATE_0, "moderate"))
    ET.SubElement(om, q(NS_RETRACT_0, "retract"))
    if reason:
        ET.SubElement(om, q(NS_MODERATE_0, "reason")).text = reason
    return [mod, old]


def split_content(el: ET.Element) -> tuple[list[ET.Element], list[ET.Element]]:
    """(content elements to encrypt with SCE, elements that must stay outside: hints, receipts, ids...)."""
    inside: list[ET.Element] = []
    outside: list[ET.Element] = []
    for child in list(el):
        (inside if child.tag in SCE_CONTENT else outside).append(child)
    return inside, outside


def to_xml(el: ET.Element, parent_ns: str = NS_CLIENT) -> str:
    """Serialise with plain xmlns declarations (no ns0: prefixes), the way XMPP servers like to read it."""
    from xml.sax.saxutils import escape, quoteattr

    tag = el.tag
    ns = parent_ns
    if tag.startswith("{"):
        ns, tag = tag[1:].split("}", 1)
    attrs = []
    if ns != parent_ns:
        attrs.append("xmlns=%s" % quoteattr(ns))
    for k, v in el.attrib.items():
        if k.startswith("{"):
            kns, kname = k[1:].split("}", 1)
            k = "xml:" + kname if kns == "http://www.w3.org/XML/1998/namespace" else kname
        attrs.append("%s=%s" % (k, quoteattr(v)))
    head = "<%s%s" % (tag, (" " + " ".join(attrs)) if attrs else "")
    inner = escape(el.text or "") + "".join(to_xml(c, ns) + escape(c.tail or "") for c in el)
    return head + ("/>" if not inner else ">%s</%s>" % (inner, tag))
