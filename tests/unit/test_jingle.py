"""Jingle XML and the SDP translation used by WebRTC calls."""

from __future__ import annotations

from xml.etree import ElementTree as ET

from ward.core import jingle, sdp

SAMPLE = """v=0
o=- 123 2 IN IP4 127.0.0.1
s=-
t=0 0
a=group:BUNDLE audio video
m=audio 9 UDP/TLS/RTP/SAVPF 111
c=IN IP4 0.0.0.0
a=rtcp:9 IN IP4 0.0.0.0
a=ice-ufrag:ufrag
a=ice-pwd:pwd
a=fingerprint:sha-256 AA:BB:CC
a=setup:actpass
a=mid:audio
a=sendrecv
a=rtcp-mux
a=rtpmap:111 opus/48000/2
a=fmtp:111 minptime=10;useinbandfec=1
a=rtcp-fb:111 transport-cc
a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level
a=ssrc:1234 cname:abc
a=candidate:1 1 UDP 2130706431 192.0.2.1 50000 typ host
m=video 9 UDP/TLS/RTP/SAVPF 96
c=IN IP4 0.0.0.0
a=mid:video
a=sendrecv
a=rtpmap:96 VP8/90000
a=candidate:2 1 UDP 2130706431 192.0.2.1 50001 typ host
"""


def test_sdp_to_contents() -> None:
    contents = sdp.contents_from_sdp(SAMPLE)
    assert [c.name for c in contents] == ["audio", "video"]
    audio = contents[0]
    assert audio.description.media == "audio"
    assert [(p.id, p.name, p.clockrate, p.channels) for p in audio.description.payloads] == [
        (111, "opus", 48000, 2)]
    assert audio.description.payloads[0].parameters == "minptime=10;useinbandfec=1"
    assert audio.description.payloads[0].feedback == ["transport-cc"]
    assert audio.description.rtcp_mux
    assert audio.transport.ufrag == "ufrag" and audio.transport.pwd == "pwd"
    assert audio.transport.fingerprints == [("sha-256", "AA:BB:CC", "actpass")]
    assert audio.transport.candidates[0].ip == "192.0.2.1"
    assert audio.transport.candidates[0].port == 50000
    assert audio.description.sources[0].parameters["cname"] == "abc"


def test_contents_to_sdp() -> None:
    contents = sdp.contents_from_sdp(SAMPLE)
    again = sdp.contents_from_sdp(sdp.sdp_from_contents(contents, role="answer"))
    assert [c.name for c in again] == ["audio", "video"]
    assert again[0].description.payloads[0].name == "opus"
    assert again[0].transport.ufrag == "ufrag"
    assert again[1].description.payloads[0].name == "VP8"
    assert again[1].transport.candidates[0].port == 50001


def test_jingle_round_trip() -> None:
    contents = sdp.contents_from_sdp(SAMPLE, creator="initiator")
    xml = jingle.build_jingle("session-initiate", "sid1", "alice@example.org/ward", contents)
    parsed = jingle.parse_jingle(xml)
    assert parsed.action == "session-initiate" and parsed.sid == "sid1"
    assert parsed.initiator == "alice@example.org/ward"
    assert [c.name for c in parsed.contents] == ["audio", "video"]
    assert parsed.contents[0].description.payloads[0].name == "opus"
    assert parsed.contents[0].transport.ufrag == "ufrag"
    assert parsed.contents[1].transport.candidates[0].port == 50001


def test_jingle_message_initiation() -> None:
    xml = jingle.build_jmi("sid2", "propose", ("audio", "video"))
    sid, action, media = jingle.parse_jmi(xml)
    assert (sid, action, media) == ("sid2", "propose", ("audio", "video"))
    accept = jingle.build_jmi("sid2", "accept")
    assert jingle.parse_jmi(accept) == ("sid2", "accept", ())


def test_reason() -> None:
    el = ET.fromstring(
        "<jingle xmlns='urn:xmpp:jingle:1' action='session-terminate' sid='s'>"
        "<reason><text>bye</text></reason></jingle>")
    assert jingle.parse_jingle(el).reason == "bye"
