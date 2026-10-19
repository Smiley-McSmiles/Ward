"""Jingle RTP sessions as SDP, and back: the same translation Conversations makes for WebRTC.

A call is negotiated in Jingle (XEP-0166) with RTP descriptions (XEP-0167, with XEP-0293 feedback, XEP-0294 header
extensions, XEP-0339 sources and XEP-0338 bundle groups) and ICE-UDP transports (XEP-0176) carrying DTLS-SRTP
fingerprints (XEP-0320).  GStreamer's webrtcbin speaks SDP, so each Jingle content becomes one SDP m-section, its
name the mid.  Candidates trickle separately in transport-info."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

NS_JINGLE = "urn:xmpp:jingle:1"
NS_RTP = "urn:xmpp:jingle:apps:rtp:1"
NS_RTP_AUDIO = "urn:xmpp:jingle:apps:rtp:audio"
NS_RTP_VIDEO = "urn:xmpp:jingle:apps:rtp:video"
NS_RTCP_FB = "urn:xmpp:jingle:apps:rtp:rtcp-fb:0"
NS_HDREXT = "urn:xmpp:jingle:apps:rtp:rtp-hdrext:0"
NS_SSMA = "urn:xmpp:jingle:apps:rtp:ssma:0"
NS_GROUPING = "urn:xmpp:jingle:apps:grouping:0"
NS_ICE_UDP = "urn:xmpp:jingle:transports:ice-udp:1"
NS_DTLS = "urn:xmpp:jingle:apps:dtls:0"
NS_JMI = "urn:xmpp:jingle-message:0"
NS_ERRORS = "urn:xmpp:jingle:errors:1"
NS_SDP_OA = "urn:ietf:rfc:3264"

# what a client must advertise for others to call it (Conversations checks these).  SDP offer/answer tells peers
# such as Conversations to put their candidates in the session-initiate/accept as well as trickling them, which
# survives a lost transport-info.
FEATURES = (NS_JINGLE, NS_RTP, NS_RTP_AUDIO, NS_RTP_VIDEO, NS_ICE_UDP, NS_DTLS, NS_JMI, NS_SDP_OA)

PROTOCOL = "UDP/TLS/RTP/SAVPF"


def q(ns: str, tag: str) -> str:
    return "{%s}%s" % (ns, tag)


@dataclass
class Candidate:
    """An ICE candidate, as in SDP (candidate:...) and Jingle (<candidate/>)."""
    foundation: str
    component: int
    protocol: str
    priority: int
    ip: str
    port: int
    type: str
    rel_addr: str = ""
    rel_port: int = 0
    generation: int = 0
    id: str = ""

    @classmethod
    def from_sdp(cls, line: str) -> Candidate | None:
        """'candidate:1 1 UDP 2015363327 192.168.1.2 41234 typ host [raddr .. rport ..] [generation 0]'."""
        line = line.strip()
        if line.startswith("a="):
            line = line[2:]
        if not line.startswith("candidate:"):
            return None
        parts = line[len("candidate:"):].split()
        if len(parts) < 8 or parts[6] != "typ":
            return None
        extra = dict(zip(parts[8::2], parts[9::2], strict=False))
        try:
            return cls(foundation=parts[0], component=int(parts[1]), protocol=parts[2].lower(),
                       priority=int(parts[3]), ip=parts[4], port=int(parts[5]), type=parts[7],
                       rel_addr=extra.get("raddr", ""), rel_port=int(extra.get("rport", 0) or 0),
                       generation=int(extra.get("generation", 0) or 0), id=_random_id())
        except ValueError:
            return None

    def to_sdp(self) -> str:
        s = "candidate:%s %d %s %d %s %d typ %s" % (self.foundation, self.component, self.protocol.upper(),
                                                     self.priority, self.ip, self.port, self.type)
        if self.rel_addr:
            s += " raddr %s rport %d" % (self.rel_addr, self.rel_port)
        return s + " generation %d" % self.generation

    @classmethod
    def from_xml(cls, el: ET.Element) -> Candidate | None:
        try:
            return cls(foundation=el.get("foundation", ""), component=int(el.get("component", 1)),
                       protocol=(el.get("protocol") or "udp").lower(), priority=int(el.get("priority", 0)),
                       ip=el.get("ip", ""), port=int(el.get("port", 0)), type=el.get("type", "host"),
                       rel_addr=el.get("rel-addr", ""), rel_port=int(el.get("rel-port", 0) or 0),
                       generation=int(el.get("generation", 0) or 0), id=el.get("id") or _random_id())
        except ValueError:
            return None

    def to_xml(self) -> ET.Element:
        a = {"component": str(self.component), "foundation": self.foundation, "generation": str(self.generation),
             "id": self.id or _random_id(), "ip": self.ip, "network": "0", "port": str(self.port),
             "priority": str(self.priority), "protocol": self.protocol, "type": self.type}
        if self.rel_addr:
            a["rel-addr"] = self.rel_addr
            a["rel-port"] = str(self.rel_port)
        return ET.Element(q(NS_ICE_UDP, "candidate"), a)


def _random_id() -> str:
    return "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(10))


# ---------------------------------------------------------------------------------------------------- SDP model
@dataclass
class Media:
    kind: str                                   # audio | video
    formats: list[str] = field(default_factory=list)
    attrs: list[tuple[str, str]] = field(default_factory=list)     # (name, value); value "" for flags

    def get(self, name: str) -> str | None:
        return next((v for n, v in self.attrs if n == name), None)

    def all(self, name: str) -> list[str]:
        return [v for n, v in self.attrs if n == name]

    @property
    def mid(self) -> str:
        return self.get("mid") or self.kind


@dataclass
class Sdp:
    attrs: list[tuple[str, str]] = field(default_factory=list)
    media: list[Media] = field(default_factory=list)

    def get(self, name: str) -> str | None:
        return next((v for n, v in self.attrs if n == name), None)


def parse_sdp(text: str) -> Sdp:
    sdp = Sdp()
    cur: Media | None = None
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if len(line) < 2 or line[1] != "=":
            continue
        kind, value = line[0], line[2:]
        if kind == "m":
            parts = value.split()
            cur = Media(kind=parts[0], formats=parts[3:])
            sdp.media.append(cur)
        elif kind == "a":
            name, _, val = value.partition(":")
            (cur.attrs if cur is not None else sdp.attrs).append((name, val))
    return sdp


DIRECTIONS = ("sendrecv", "sendonly", "recvonly", "inactive")


def senders_of(direction: str, author_is_initiator: bool) -> str:
    """SDP direction (from its author's view) -> Jingle senders."""
    me, other = ("initiator", "responder") if author_is_initiator else ("responder", "initiator")
    return {"sendrecv": "both", "sendonly": me, "recvonly": other, "inactive": "none"}.get(direction, "both")


def direction_of(senders: str, author_is_initiator: bool) -> str:
    me = "initiator" if author_is_initiator else "responder"
    if senders in ("", "both"):
        return "sendrecv"
    if senders == "none":
        return "inactive"
    return "sendonly" if senders == me else "recvonly"


# ---------------------------------------------------------------------------------------------------- SDP -> Jingle
def sdp_to_jingle(text: str, author_is_initiator: bool, with_candidates: bool = True) -> list[ET.Element]:
    """The <content/> elements (and a bundle <group/> when there is one) for a <jingle/>."""
    sdp = parse_sdp(text)
    out: list[ET.Element] = []
    session_ufrag, session_pwd = sdp.get("ice-ufrag"), sdp.get("ice-pwd")
    session_fp, session_setup = sdp.get("fingerprint"), sdp.get("setup")
    for m in sdp.media:
        if m.kind not in ("audio", "video"):
            continue
        direction = next((n for n, _v in m.attrs if n in DIRECTIONS), "sendrecv")
        content = ET.Element(q(NS_JINGLE, "content"), {"creator": "initiator", "name": m.mid,
                                                         "senders": senders_of(direction, author_is_initiator)})
        desc = ET.SubElement(content, q(NS_RTP, "description"), {"media": m.kind})
        fmtp = {}
        for v in m.all("fmtp"):
            pt, _, params = v.partition(" ")
            fmtp[pt] = params
        fbs: dict[str, list[str]] = {}
        for v in m.all("rtcp-fb"):
            pt, _, rest = v.partition(" ")
            fbs.setdefault(pt, []).append(rest)
        rtpmap = {}
        for v in m.all("rtpmap"):
            pt, _, rest = v.partition(" ")
            rtpmap[pt] = rest
        for pt in m.formats:
            if pt not in rtpmap:
                continue
            enc = rtpmap[pt].split("/")
            a = {"id": pt, "name": enc[0], "clockrate": enc[1] if len(enc) > 1 else ""}
            if len(enc) > 2:
                a["channels"] = enc[2]
            if enc[0].lower() == "opus":
                # Opus is always 'opus/48000/2' in SDP (RFC 7587); libwebrtc ignores it otherwise, and webrtcbin
                # can leave the channels out
                a.update(name="opus", clockrate="48000", channels="2")
            pel = ET.SubElement(desc, q(NS_RTP, "payload-type"), {k: v for k, v in a.items() if v})
            for p in (fmtp.get(pt) or "").split(";"):
                name, eq, value = p.strip().partition("=")
                if name and eq:
                    ET.SubElement(pel, q(NS_RTP, "parameter"), {"name": name, "value": value})
                elif name:      # a bare value (RED's '111/111')
                    ET.SubElement(pel, q(NS_RTP, "parameter"), {"name": "", "value": name})
            for fb in fbs.get(pt, []):
                _feedback(pel, fb)
        for fb in fbs.get("*", []):
            _feedback(desc, fb)
        for v in m.all("extmap"):
            ident, _, uri = v.partition(" ")
            ident = ident.split("/")[0]
            uri = uri.split()[0] if uri else ""
            if ident and uri:
                ET.SubElement(desc, q(NS_HDREXT, "rtp-hdrext"), {"id": ident, "uri": uri})
        if m.get("extmap-allow-mixed") is not None:
            ET.SubElement(desc, q(NS_HDREXT, "extmap-allow-mixed"))
        for v in m.all("ssrc-group"):
            parts = v.split()
            if len(parts) > 1:
                g = ET.SubElement(desc, q(NS_SSMA, "ssrc-group"), {"semantics": parts[0]})
                for s in parts[1:]:
                    ET.SubElement(g, q(NS_SSMA, "source"), {"ssrc": s})
        sources: dict[str, ET.Element] = {}
        for v in m.all("ssrc"):
            ssrc, _, rest = v.partition(" ")
            name, _, value = rest.partition(":")
            if not ssrc or not name:
                continue
            src = sources.get(ssrc)
            if src is None:
                src = sources[ssrc] = ET.SubElement(desc, q(NS_SSMA, "source"), {"ssrc": ssrc})
            if value:
                ET.SubElement(src, q(NS_SSMA, "parameter"), {"name": name, "value": value})
        if m.get("rtcp-mux") is not None:
            ET.SubElement(desc, q(NS_RTP, "rtcp-mux"))
        tr = ET.SubElement(content, q(NS_ICE_UDP, "transport"),
                           {"ufrag": m.get("ice-ufrag") or session_ufrag or "",
                            "pwd": m.get("ice-pwd") or session_pwd or ""})
        fp = m.get("fingerprint") or session_fp
        if fp:
            hash_, _, value = fp.partition(" ")
            ET.SubElement(tr, q(NS_DTLS, "fingerprint"),
                          {"hash": hash_, "setup": m.get("setup") or session_setup or "actpass"}).text = value
        if with_candidates:
            for v in m.all("candidate"):
                c = Candidate.from_sdp("candidate:" + v)
                if c is not None and c.protocol == "udp":
                    tr.append(c.to_xml())
        out.append(content)
    group = sdp.get("group")
    if group:
        parts = group.split()
        g = ET.Element(q(NS_GROUPING, "group"), {"semantics": parts[0]})
        for name in parts[1:]:
            ET.SubElement(g, q(NS_GROUPING, "content"), {"name": name})
        out.append(g)
    return out


def _feedback(parent: ET.Element, fb: str) -> None:
    parts = fb.split()
    if not parts:
        return
    if parts[0] == "trr-int":
        ET.SubElement(parent, q(NS_RTCP_FB, "rtcp-fb-trr-int"), {"value": parts[1] if len(parts) > 1 else "0"})
        return
    a = {"type": parts[0]}
    if len(parts) > 1:
        a["subtype"] = parts[1]
    ET.SubElement(parent, q(NS_RTCP_FB, "rtcp-fb"), a)


# ---------------------------------------------------------------------------------------------------- Jingle -> SDP
def jingle_to_sdp(jingle: ET.Element, author_is_initiator: bool) -> str:
    """The SDP for a session-initiate (offer) or session-accept (answer)."""
    lines = ["v=0", "o=- %d 2 IN IP4 0.0.0.0" % random.randint(10**15, 10**16), "s=-", "t=0 0"]
    group = jingle.find(q(NS_GROUPING, "group"))
    if group is not None:
        names = [c.get("name", "") for c in group.findall(q(NS_GROUPING, "content"))]
        lines.append("a=group:%s %s" % (group.get("semantics", "BUNDLE"), " ".join(names)))
    lines.append("a=msid-semantic: WMS *")
    for content in jingle.findall(q(NS_JINGLE, "content")):
        desc = content.find(q(NS_RTP, "description"))
        tr = content.find(q(NS_ICE_UDP, "transport"))
        if desc is None or tr is None:
            continue
        pts = desc.findall(q(NS_RTP, "payload-type"))
        lines.append("m=%s 9 %s %s" % (desc.get("media", "audio"), PROTOCOL, " ".join(p.get("id", "") for p in pts)))
        lines += ["c=IN IP4 0.0.0.0", "a=rtcp:9 IN IP4 0.0.0.0",
                  "a=ice-ufrag:%s" % tr.get("ufrag", ""), "a=ice-pwd:%s" % tr.get("pwd", ""),
                  "a=ice-options:trickle"]
        fp = tr.find(q(NS_DTLS, "fingerprint"))
        if fp is not None:
            lines.append("a=fingerprint:%s %s" % (fp.get("hash", "sha-256"), (fp.text or "").strip()))
            lines.append("a=setup:%s" % (fp.get("setup") or "actpass"))
        lines.append("a=mid:%s" % content.get("name", ""))
        lines.append("a=%s" % direction_of(content.get("senders", "both"), author_is_initiator))
        if desc.find(q(NS_RTP, "rtcp-mux")) is not None or group is not None:
            lines.append("a=rtcp-mux")
        for ext in desc.findall(q(NS_HDREXT, "rtp-hdrext")):
            lines.append("a=extmap:%s %s" % (ext.get("id", ""), ext.get("uri", "")))
        if desc.find(q(NS_HDREXT, "extmap-allow-mixed")) is not None:
            lines.append("a=extmap-allow-mixed")
        for p in pts:
            pid = p.get("id", "")
            enc = "%s/%s" % (p.get("name", ""), p.get("clockrate", "90000"))
            if (p.get("name") or "").lower() == "opus":
                enc = "opus/48000/2"
            elif p.get("channels") and p.get("channels") != "1":
                enc += "/" + p.get("channels", "")
            lines.append("a=rtpmap:%s %s" % (pid, enc))
            # name=value; a bare value without a name (RED's '111/111') or a bare name (a flag) on its own
            params = ["%s=%s" % (x.get("name"), x.get("value")) if x.get("name") and x.get("value")
                      else (x.get("name") or x.get("value") or "") for x in p.findall(q(NS_RTP, "parameter"))]
            params = [x for x in params if x]
            if params:
                lines.append("a=fmtp:%s %s" % (pid, ";".join(params)))
            lines += _feedback_lines(p, pid)
        lines += _feedback_lines(desc, "*")
        for g in desc.findall(q(NS_SSMA, "ssrc-group")):
            lines.append("a=ssrc-group:%s %s" % (g.get("semantics", ""), " ".join(
                s.get("ssrc", "") for s in g.findall(q(NS_SSMA, "source")))))
        for s in desc.findall(q(NS_SSMA, "source")):
            for prm in s.findall(q(NS_SSMA, "parameter")):
                lines.append("a=ssrc:%s %s:%s" % (s.get("ssrc", ""), prm.get("name", ""), prm.get("value", "")))
        for c in tr.findall(q(NS_ICE_UDP, "candidate")):
            cand = Candidate.from_xml(c)
            if cand is not None:
                lines.append("a=" + cand.to_sdp())
    return "\r\n".join(lines) + "\r\n"


def _feedback_lines(el: ET.Element, pt: str) -> list[str]:
    out = []
    for fb in el.findall(q(NS_RTCP_FB, "rtcp-fb")):
        sub = fb.get("subtype") or ""
        out.append("a=rtcp-fb:%s %s%s" % (pt, fb.get("type", ""), " " + sub if sub else ""))
    for t in el.findall(q(NS_RTCP_FB, "rtcp-fb-trr-int")):
        out.append("a=rtcp-fb:%s trr-int %s" % (pt, t.get("value", "0")))
    return out


# ---------------------------------------------------------------------------------------------------- stanzas
def jingle_element(action: str, sid: str, initiator: str = "", responder: str = "") -> ET.Element:
    a = {"action": action, "sid": sid}
    if initiator:
        a["initiator"] = initiator
    if responder:
        a["responder"] = responder
    return ET.Element(q(NS_JINGLE, "jingle"), a)


def transport_info(sid: str, content_name: str, ufrag: str, pwd: str, candidate: Candidate) -> ET.Element:
    j = jingle_element("transport-info", sid)
    c = ET.SubElement(j, q(NS_JINGLE, "content"), {"creator": "initiator", "name": content_name})
    tr = ET.SubElement(c, q(NS_ICE_UDP, "transport"), {"ufrag": ufrag, "pwd": pwd})
    tr.append(candidate.to_xml())
    return j


def reason_element(condition: str, text: str = "") -> ET.Element:
    r = ET.Element(q(NS_JINGLE, "reason"))
    ET.SubElement(r, q(NS_JINGLE, condition))
    if text:
        ET.SubElement(r, q(NS_JINGLE, "text")).text = text
    return r


def reason_of(el: ET.Element | None) -> str:
    """The condition of a <reason/> (success, decline, busy, cancel, connectivity-error...), '' without one."""
    if el is None:
        return ""
    r = el.find(q(NS_JINGLE, "reason")) if el.tag != q(NS_JINGLE, "reason") else el
    if r is None:
        return ""
    for child in r:
        if child.tag != q(NS_JINGLE, "text"):
            return child.tag.split("}")[-1]
    return ""


def text_of(el: ET.Element | None) -> str:
    """The free text in a <reason/> (what a peer says went wrong), '' without one."""
    if el is None:
        return ""
    r = el.find(q(NS_JINGLE, "reason")) if el.tag != q(NS_JINGLE, "reason") else el
    if r is None:
        return ""
    text = r.find(q(NS_JINGLE, "text"))
    return (text.text or "").strip() if text is not None else ""


def add_candidates(contents: list[ET.Element], candidates: dict[str, list[Candidate]]) -> int:
    """Put candidates for a content name into its <transport>; returns how many were added."""
    added = 0
    for content in contents:
        if content.tag != q(NS_JINGLE, "content"):
            continue
        transport = content.find(q(NS_ICE_UDP, "transport"))
        if transport is None:
            continue
        have = {(c.get("ip"), c.get("port")) for c in transport.findall(q(NS_ICE_UDP, "candidate"))}
        for candidate in candidates.get(content.get("name", ""), []):
            if (candidate.ip, str(candidate.port)) in have:
                continue
            transport.append(candidate.to_xml())
            added += 1
    return added


def media_of(jingle_or_propose: ET.Element) -> set[str]:
    """{'audio', 'video'} from a <propose/>'s descriptions or a <jingle/>'s contents."""
    out = set()
    for d in jingle_or_propose.iter(q(NS_RTP, "description")):
        if d.get("media") in ("audio", "video"):
            out.add(d.get("media", ""))
    return out


def jmi(kind: str, sid: str, media: set[str] | None = None, reason: str = "") -> ET.Element:
    """A Jingle Message Initiation element (XEP-0353): propose, ringing, proceed, accept, reject, retract, finish."""
    el = ET.Element(q(NS_JMI, kind), {"id": sid})
    for m in sorted(media or ()):
        ET.SubElement(el, q(NS_RTP, "description"), {"media": m})
    if reason:
        el.append(reason_element(reason))
    return el
