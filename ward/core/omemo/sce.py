"""Stanza Content Encryption (XEP-0420), the envelope OMEMO 2 encrypts.

The whole content of a message goes inside (body, replies, reactions, corrections, markers, file metadata...),
with random padding to hide its length, a timestamp, and to/from affixes that the receiver checks so a message
cannot be replayed into another conversation."""

from __future__ import annotations

import secrets
import time
from xml.etree import ElementTree as ET

from ..stanzas import format_stamp, parse_stamp

NS = "urn:xmpp:sce:1"
MAX_SKEW = 7 * 24 * 3600   # a timestamp further in the future than this is suspicious


class EnvelopeError(Exception):
    pass


def build(content: list[ET.Element], to_jid: str, from_jid: str, now: float | None = None) -> bytes:
    env = ET.Element("{%s}envelope" % NS)
    c = ET.SubElement(env, "{%s}content" % NS)
    for el in content:
        c.append(el)
    # 0..199 random characters, as recommended for OMEMO
    ET.SubElement(env, "{%s}rpad" % NS).text = secrets.token_urlsafe(150)[:secrets.randbelow(200)]
    ET.SubElement(env, "{%s}time" % NS, {"stamp": format_stamp(now if now is not None else time.time())})
    ET.SubElement(env, "{%s}to" % NS, {"jid": to_jid})
    ET.SubElement(env, "{%s}from" % NS, {"jid": from_jid})
    return ET.tostring(env, encoding="utf-8")


def parse(data: bytes, expect_from: str, expect_to: str | set[str]) -> tuple[list[ET.Element], float | None]:
    """Content elements and the timestamp; raises EnvelopeError when the envelope does not belong here."""
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise EnvelopeError("refusing XML with a document type")
    try:
        env = ET.fromstring(data)
    except ET.ParseError as e:
        raise EnvelopeError("not XML: %s" % e) from None
    if env.tag != "{%s}envelope" % NS:
        raise EnvelopeError("not an SCE envelope")
    frm = env.find("{%s}from" % NS)
    to = env.find("{%s}to" % NS)
    if frm is None or frm.get("jid", "").split("/")[0].lower() != expect_from.lower():
        raise EnvelopeError("the envelope was sent by someone else")
    targets = {expect_to.lower()} if isinstance(expect_to, str) else {t.lower() for t in expect_to}
    if to is None or to.get("jid", "").split("/")[0].lower() not in targets:
        raise EnvelopeError("the envelope was meant for another conversation")
    stamp_el = env.find("{%s}time" % NS)
    stamp = parse_stamp(stamp_el.get("stamp")) if stamp_el is not None else None
    if stamp is not None and stamp - time.time() > MAX_SKEW:
        raise EnvelopeError("the envelope is dated in the future")
    content = env.find("{%s}content" % NS)
    return (list(content) if content is not None else []), stamp
