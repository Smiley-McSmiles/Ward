"""Jingle (XEP-0166) and Jingle Message Initiation (XEP-0353) for Ward's calls.

This module only deals with XML and SDP, not GStreamer: it turns a Jingle session-initiate into an SDP offer the
media engine can consume, and a local SDP answer/offer into the Jingle contents the peer receives.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

NS_JINGLE = "urn:xmpp:jingle:1"
NS_RTP = "urn:xmpp:jingle:apps:rtp:1"
NS_RTP_INFO = "urn:xmpp:jingle:apps:rtp:info:1"
NS_ICE = "urn:xmpp:jingle:transports:ice-udp:1"
NS_DTLS = "urn:xmpp:jingle:apps:dtls:0"
NS_JMI = "urn:xmpp:jingle-message:0"


def q(ns: str, tag: str) -> str:
    return "{%s}%s" % (ns, tag)


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass
class Payload:
    id: int
    name: str
    clockrate: int = 0
    channels: int = 0
    parameters: str = ""
    feedback: list[str] = field(default_factory=list)


@dataclass
class Source:
    ssrc: int
    parameters: dict[str, str] = field(default_factory=dict)


@dataclass
class Description:
    media: str
    payloads: list[Payload] = field(default_factory=list)
    rtcp_mux: bool = True
    extmap: list[tuple[int, str]] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    ssrc_groups: list[tuple[str, list[int]]] = field(default_factory=list)


@dataclass
class Candidate:
    foundation: str
    component: int
    protocol: str
    priority: int
    ip: str
    port: int
    type: str
    generation: int = 0
    network: str = ""
    rel_addr: str = ""
    rel_port: int = 0
    tcp_type: str = ""
    id: str = ""


@dataclass
class Transport:
    ufrag: str = ""
    pwd: str = ""
    fingerprints: list[tuple[str, str, str]] = field(default_factory=list)   # hash, value, setup
    candidates: list[Candidate] = field(default_factory=list)


@dataclass
class Content:
    name: str
    creator: str = "initiator"
    senders: str = "both"
    description: Description | None = None
    transport: Transport | None = None


@dataclass
class Jingle:
    action: str
    sid: str
    initiator: str = ""
    responder: str = ""
    contents: list[Content] = field(default_factory=list)
    reason: str = ""


# ----------------------------------------------------------------------------------------------- JMI
def build_jmi(sid: str, action: str, media: tuple[str, ...] = ()) -> ET.Element:
    el = ET.Element(q(NS_JMI, "jingle-message"), {"id": sid})
    child = ET.SubElement(el, q(NS_JMI, action))
    if action == "propose":
        for kind in media or ("audio",):
            ET.SubElement(child, q(NS_RTP, "description"), {"media": kind})
    return el


def parse_jmi(el) -> tuple[str, str, tuple[str, ...]]:
    """(sid, action, media) from a <jingle-message/>."""
    sid = el.get("id") or ""
    action = ""
    media: list[str] = []
    for child in el:
        name = local(child.tag)
        if name in ("propose", "proceed", "accept", "reject", "retract", "finish"):
            action = name
            for desc in child:
                if local(desc.tag) == "description":
                    kind = desc.get("media") or "audio"
                    if kind not in media:
                        media.append(kind)
            break
    return sid, action, tuple(media)


# ----------------------------------------------------------------------------------------------- Jingle
def _int(value: str | None, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def parse_jingle(el) -> Jingle:
    j = Jingle(el.get("action") or "", el.get("sid") or "", el.get("initiator") or "",
               el.get("responder") or "")
    for content in el.findall(q(NS_JINGLE, "content")):
        c = Content(content.get("name") or "content", content.get("creator") or "initiator",
                    content.get("senders") or "both")
        desc = content.find(q(NS_RTP, "description"))
        if desc is not None:
            d = Description(desc.get("media") or "audio", rtcp_mux=desc.find(q(NS_RTP, "rtcp-mux")) is not None)
            for p in desc.findall(q(NS_RTP, "payload-type")):
                params = ""
                par = p.find(q(NS_RTP, "parameter"))
                if par is not None:
                    params = "%s=%s" % (par.get("name") or "", par.get("value") or "")
                d.payloads.append(Payload(_int(p.get("id")), p.get("name") or "", _int(p.get("clockrate")),
                                          _int(p.get("channels")), params,
                                          [f.get("type") or "" for f in p.findall(q(NS_RTP, "rtcp-fb"))]))
            for ext in desc.findall(q(NS_RTP, "rtp-hdrext")):
                d.extmap.append((_int(ext.get("id")), ext.get("uri") or ""))
            for source in desc.findall(q(NS_RTP, "source")):
                d.sources.append(Source(_int(source.get("ssrc")),
                                        {p.get("name") or "": p.get("value") or ""
                                         for p in source.findall(q(NS_RTP, "parameter"))}))
            for group in desc.findall(q(NS_RTP, "ssrc-group")):
                d.ssrc_groups.append((group.get("semantics") or "", [_int(s.text) for s in group]))
            c.description = d
        trans = content.find(q(NS_ICE, "transport"))
        if trans is not None:
            t = Transport(trans.get("ufrag") or "", trans.get("pwd") or "")
            for fp in trans.findall(q(NS_DTLS, "fingerprint")):
                t.fingerprints.append((fp.get("hash") or "sha-256", (fp.text or "").strip(),
                                       fp.get("setup") or "actpass"))
            for cand in trans.findall(q(NS_ICE, "candidate")):
                t.candidates.append(Candidate(
                    cand.get("foundation") or "", _int(cand.get("component"), 1),
                    cand.get("protocol") or "udp", _int(cand.get("priority")), cand.get("ip") or "",
                    _int(cand.get("port")), cand.get("type") or "host", _int(cand.get("generation")),
                    cand.get("network") or "", cand.get("rel-addr") or "", _int(cand.get("rel-port")),
                    cand.get("tcptype") or "", cand.get("id") or ""))
            c.transport = t
        j.contents.append(c)
    reason = el.find("%s/%s" % (q(NS_JINGLE, "reason"), q(NS_JINGLE, "text")))
    j.reason = (reason.text or "") if reason is not None else ""
    return j


def _description_el(desc: Description) -> ET.Element:
    el = ET.Element(q(NS_RTP, "description"), {"media": desc.media})
    for p in desc.payloads:
        attrs = {"id": str(p.id), "name": p.name}
        if p.clockrate:
            attrs["clockrate"] = str(p.clockrate)
        if p.channels:
            attrs["channels"] = str(p.channels)
        pe = ET.SubElement(el, q(NS_RTP, "payload-type"), attrs)
        if "=" in p.parameters:
            name, value = p.parameters.split("=", 1)
            ET.SubElement(pe, q(NS_RTP, "parameter"), {"name": name, "value": value})
        for feedback in p.feedback:
            ET.SubElement(pe, q(NS_RTP, "rtcp-fb"), {"type": feedback})
    for ident, uri in desc.extmap:
        ET.SubElement(el, q(NS_RTP, "rtp-hdrext"), {"id": str(ident), "uri": uri})
    for source in desc.sources:
        se = ET.SubElement(el, q(NS_RTP, "source"), {"ssrc": str(source.ssrc)})
        for name, value in source.parameters.items():
            ET.SubElement(se, q(NS_RTP, "parameter"), {"name": name, "value": value})
    for semantics, ssrcs in desc.ssrc_groups:
        ge = ET.SubElement(el, q(NS_RTP, "ssrc-group"), {"semantics": semantics})
        for ssrc in ssrcs:
            ET.SubElement(ge, q(NS_RTP, "ssrc")).text = str(ssrc)
    if desc.rtcp_mux:
        ET.SubElement(el, q(NS_RTP, "rtcp-mux"))
    return el


def _transport_el(trans: Transport) -> ET.Element:
    el = ET.Element(q(NS_ICE, "transport"), {"ufrag": trans.ufrag, "pwd": trans.pwd})
    for hash_, value, setup in trans.fingerprints:
        fp = ET.SubElement(el, q(NS_DTLS, "fingerprint"), {"hash": hash_, "setup": setup})
        fp.text = value
    for c in trans.candidates:
        attrs = {"foundation": c.foundation, "component": str(c.component), "protocol": c.protocol,
                 "priority": str(c.priority), "ip": c.ip, "port": str(c.port), "type": c.type,
                 "generation": str(c.generation)}
        if c.network:
            attrs["network"] = c.network
        if c.rel_addr:
            attrs["rel-addr"] = c.rel_addr
        if c.rel_port:
            attrs["rel-port"] = str(c.rel_port)
        if c.tcp_type:
            attrs["tcptype"] = c.tcp_type
        if c.id:
            attrs["id"] = c.id
        ET.SubElement(el, q(NS_ICE, "candidate"), attrs)
    return el


def build_jingle(action: str, sid: str, initiator: str, contents: list[Content], responder: str = "",
                 reason: str = "") -> ET.Element:
    attrs = {"action": action, "sid": sid}
    if initiator:
        attrs["initiator"] = initiator
    if responder:
        attrs["responder"] = responder
    el = ET.Element(q(NS_JINGLE, "jingle"), attrs)
    for content in contents:
        ce = ET.SubElement(el, q(NS_JINGLE, "content"),
                           {"creator": content.creator, "name": content.name, "senders": content.senders})
        if content.description is not None:
            ce.append(_description_el(content.description))
        if content.transport is not None:
            ce.append(_transport_el(content.transport))
    if reason:
        re = ET.SubElement(el, q(NS_JINGLE, "reason"))
        ET.SubElement(re, q(NS_JINGLE, "text")).text = reason
    return el


def new_sid() -> str:
    return secrets.token_hex(8)
