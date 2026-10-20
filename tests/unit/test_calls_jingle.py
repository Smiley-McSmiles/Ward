"""Jingle <-> SDP for calls: candidates, directions, Opus, and answering an offer shaped like Conversations' own."""

import asyncio
from xml.etree import ElementTree as ET

import pytest

from ward.core.calls import jingle, media

FP = ":".join(["AB"] * 32)

# a session-initiate as Conversations (libwebrtc on Android) sends it: many codecs, RTX, RED, header extensions
CONVERSATIONS_OFFER = """
<jingle xmlns="urn:xmpp:jingle:1" action="session-initiate" sid="a73sjjvkla37jfea"
        initiator="romeo@montague.example/Conversations.abc">
 <content creator="initiator" name="0" senders="both">
  <description xmlns="urn:xmpp:jingle:apps:rtp:1" media="audio">
   <payload-type id="111" name="opus" clockrate="48000" channels="2">
    <parameter name="minptime" value="10"/><parameter name="useinbandfec" value="1"/>
    <rtcp-fb xmlns="urn:xmpp:jingle:apps:rtp:rtcp-fb:0" type="transport-cc"/>
   </payload-type>
   <payload-type id="63" name="red" clockrate="48000" channels="2"><parameter name="" value="111/111"/></payload-type>
   <payload-type id="9" name="G722" clockrate="8000"/>
   <payload-type id="0" name="PCMU" clockrate="8000"/>
   <payload-type id="8" name="PCMA" clockrate="8000"/>
   <payload-type id="13" name="CN" clockrate="8000"/>
   <payload-type id="110" name="telephone-event" clockrate="48000"/>
   <payload-type id="126" name="telephone-event" clockrate="8000"/>
   <rtp-hdrext xmlns="urn:xmpp:jingle:apps:rtp:rtp-hdrext:0" id="1" uri="urn:ietf:params:rtp-hdrext:ssrc-audio-level"/>
   <rtp-hdrext xmlns="urn:xmpp:jingle:apps:rtp:rtp-hdrext:0" id="3"
               uri="http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01"/>
   <source xmlns="urn:xmpp:jingle:apps:rtp:ssma:0" ssrc="2208853474">
    <parameter name="cname" value="9pOx0r1kFyG1lr2C"/><parameter name="msid" value="my-media-stream my-audio-track"/>
   </source>
   <rtcp-mux/>
  </description>
  <transport xmlns="urn:xmpp:jingle:transports:ice-udp:1" ufrag="h2Lq" pwd="fNQUN7RnR2Pqh0nz6cBxN9Fn">
   <fingerprint xmlns="urn:xmpp:jingle:apps:dtls:0" hash="sha-256" setup="actpass">%(fp)s</fingerprint>
  </transport>
 </content>
 <content creator="initiator" name="1" senders="both">
  <description xmlns="urn:xmpp:jingle:apps:rtp:1" media="video">
   <payload-type id="96" name="VP8" clockrate="90000">
    <rtcp-fb xmlns="urn:xmpp:jingle:apps:rtp:rtcp-fb:0" type="goog-remb"/>
    <rtcp-fb xmlns="urn:xmpp:jingle:apps:rtp:rtcp-fb:0" type="transport-cc"/>
    <rtcp-fb xmlns="urn:xmpp:jingle:apps:rtp:rtcp-fb:0" type="ccm" subtype="fir"/>
    <rtcp-fb xmlns="urn:xmpp:jingle:apps:rtp:rtcp-fb:0" type="nack"/>
    <rtcp-fb xmlns="urn:xmpp:jingle:apps:rtp:rtcp-fb:0" type="nack" subtype="pli"/>
   </payload-type>
   <payload-type id="97" name="rtx" clockrate="90000"><parameter name="apt" value="96"/></payload-type>
   <payload-type id="98" name="VP9" clockrate="90000"><parameter name="profile-id" value="0"/></payload-type>
   <payload-type id="99" name="rtx" clockrate="90000"><parameter name="apt" value="98"/></payload-type>
   <payload-type id="102" name="H264" clockrate="90000">
    <parameter name="level-asymmetry-allowed" value="1"/><parameter name="packetization-mode" value="1"/>
    <parameter name="profile-level-id" value="42e01f"/>
   </payload-type>
   <rtp-hdrext xmlns="urn:xmpp:jingle:apps:rtp:rtp-hdrext:0" id="14" uri="urn:ietf:params:rtp-hdrext:toffset"/>
   <ssrc-group xmlns="urn:xmpp:jingle:apps:rtp:ssma:0" semantics="FID">
    <source ssrc="1823948522"/><source ssrc="2617453018"/>
   </ssrc-group>
   <source xmlns="urn:xmpp:jingle:apps:rtp:ssma:0" ssrc="1823948522">
    <parameter name="cname" value="9pOx0r1kFyG1lr2C"/><parameter name="msid" value="my-media-stream my-video-track"/>
   </source>
   <source xmlns="urn:xmpp:jingle:apps:rtp:ssma:0" ssrc="2617453018">
    <parameter name="cname" value="9pOx0r1kFyG1lr2C"/><parameter name="msid" value="my-media-stream my-video-track"/>
   </source>
   <rtcp-mux/>
  </description>
  <transport xmlns="urn:xmpp:jingle:transports:ice-udp:1" ufrag="h2Lq" pwd="fNQUN7RnR2Pqh0nz6cBxN9Fn">
   <fingerprint xmlns="urn:xmpp:jingle:apps:dtls:0" hash="sha-256" setup="actpass">%(fp)s</fingerprint>
   <candidate component="1" foundation="2785326347" generation="0" id="c1" ip="192.0.2.7" network="1" port="47431"
              priority="2122260223" protocol="udp" type="host"/>
  </transport>
 </content>
 <group xmlns="urn:xmpp:jingle:apps:grouping:0" semantics="BUNDLE"><content name="0"/><content name="1"/></group>
</jingle>
""" % {"fp": FP}


def test_candidates_both_ways():
    line = "candidate:842163049 1 udp 1677729535 203.0.113.5 46154 typ srflx raddr 10.0.0.2 rport 46154 generation 0"
    c = jingle.Candidate.from_sdp("a=" + line)
    assert (c.type, c.ip, c.port, c.rel_addr, c.rel_port, c.priority) == ("srflx", "203.0.113.5", 46154, "10.0.0.2",
                                                                            46154, 1677729535)
    x = c.to_xml()
    assert x.get("rel-addr") == "10.0.0.2" and x.get("protocol") == "udp"
    back = jingle.Candidate.from_xml(x)
    assert back.to_sdp() == "candidate:842163049 1 UDP 1677729535 203.0.113.5 46154 typ srflx raddr 10.0.0.2 " \
                            "rport 46154 generation 0"
    assert jingle.Candidate.from_sdp("candidate:bad") is None


def test_directions():
    assert jingle.senders_of("sendonly", True) == "initiator"
    assert jingle.senders_of("sendonly", False) == "responder"
    assert jingle.senders_of("recvonly", True) == "responder"
    for senders in ("both", "initiator", "responder", "none"):
        for author in (True, False):
            assert jingle.senders_of(jingle.direction_of(senders, author), author) == senders


def test_conversations_offer_to_sdp():
    j = ET.fromstring(CONVERSATIONS_OFFER)
    sdp = jingle.jingle_to_sdp(j, True)
    assert "a=group:BUNDLE 0 1" in sdp
    assert "m=audio 9 UDP/TLS/RTP/SAVPF 111 63 9 0 8 13 110 126" in sdp
    assert "a=rtpmap:111 opus/48000/2" in sdp and "a=fmtp:111 minptime=10;useinbandfec=1" in sdp
    assert "a=fmtp:63 111/111" in sdp
    assert "a=rtcp-fb:96 nack pli" in sdp and "a=rtcp-fb:96 goog-remb" in sdp
    assert "a=ssrc-group:FID 1823948522 2617453018" in sdp
    assert "a=fingerprint:sha-256 " + FP in sdp and "a=setup:actpass" in sdp
    assert "a=candidate:2785326347 1 UDP 2122260223 192.0.2.7 47431 typ host generation 0" in sdp
    # and back: the same contents
    contents = jingle.sdp_to_jingle(sdp, True)
    names = [c.get("name") for c in contents if c.tag == jingle.q(jingle.NS_JINGLE, "content")]
    assert names == ["0", "1"]
    audio = contents[0].find(jingle.q(jingle.NS_RTP, "description"))
    assert [p.get("id") for p in audio.findall(jingle.q(jingle.NS_RTP, "payload-type"))] == \
        ["111", "63", "9", "0", "8", "13", "110", "126"]


def test_opus_from_webrtcbin_gets_two_channels():
    sdp = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=mid:audio0\r\na=rtpmap:111 OPUS/48000\r\n" \
          "a=ice-ufrag:u\r\na=ice-pwd:p\r\na=fingerprint:sha-256 %s\r\na=setup:actpass\r\n" % FP
    content = jingle.sdp_to_jingle(sdp, True)[0]
    pt = content.find(jingle.q(jingle.NS_RTP, "description")).find(jingle.q(jingle.NS_RTP, "payload-type"))
    assert (pt.get("name"), pt.get("clockrate"), pt.get("channels")) == ("opus", "48000", "2")


def test_candidates_can_ride_in_the_description():
    """A session-initiate/accept can carry the candidates gathered so far, before trickling the rest."""
    sdp = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=mid:0\r\na=rtpmap:111 opus/48000/2\r\n" \
          "a=ice-ufrag:u\r\na=ice-pwd:p\r\na=fingerprint:sha-256 %s\r\na=setup:actpass\r\n" % FP
    contents = jingle.sdp_to_jingle(sdp, True, with_candidates=False)
    candidate = jingle.Candidate.from_sdp("candidate:1 1 UDP 2122260223 192.0.2.7 47431 typ host")
    assert jingle.add_candidates(contents, {"0": [candidate]}) == 1
    transport = contents[0].find(jingle.q(jingle.NS_ICE_UDP, "transport"))
    assert [c.get("port") for c in transport.findall(jingle.q(jingle.NS_ICE_UDP, "candidate"))] == ["47431"]
    assert jingle.add_candidates(contents, {"0": [candidate]}) == 0       # not twice


def test_answer_content_names_follow_the_offer():
    """Some webrtcbin versions rename the mids in an answer; Conversations finds candidates by the offer's names."""
    from ward.core.calls.service import CallService

    sdp = "v=0\r\na=group:BUNDLE audio0 video1\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=mid:audio0\r\n" \
          "a=rtpmap:111 opus/48000/2\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=mid:video1\r\na=rtpmap:96 VP8/90000\r\n"
    contents = jingle.sdp_to_jingle(sdp, False, with_candidates=False)
    names = CallService._match_names(contents, ["0", "1"])
    assert names == ["0", "1"]
    assert [c.get("name") for c in contents if c.tag == jingle.q(jingle.NS_JINGLE, "content")] == ["0", "1"]
    group = next(c for c in contents if c.tag == jingle.q(jingle.NS_GROUPING, "group"))
    assert [c.get("name") for c in group.findall(jingle.q(jingle.NS_GROUPING, "content"))] == ["0", "1"]


def test_jmi_and_reasons():
    p = jingle.jmi("propose", "s1", {"audio", "video"})
    assert jingle.media_of(p) == {"audio", "video"} and p.get("id") == "s1"
    r = jingle.jmi("reject", "s1", reason="busy")
    assert jingle.reason_of(r) == "busy"
    t = jingle.transport_info("s1", "0", "u", "p", jingle.Candidate.from_sdp(
        "candidate:1 1 udp 2122260223 192.0.2.1 5000 typ host"))
    assert t.get("action") == "transport-info" and t.find(".//{%s}candidate" % jingle.NS_ICE_UDP) is not None


@pytest.mark.skipif(not media.available(), reason="GStreamer WebRTC is missing")
def test_audio_gain_and_device_list(monkeypatch):
    """The microphone volume is the volume element before Opus; devices come from GStreamer's monitor."""
    monkeypatch.setenv("WARD_CALL_SOURCES", "test")
    ms = media.MediaSession(True, False, mic_gain=1.5, speaker_gain=0.5)
    try:
        ms.build()
        assert ms._mute.get_property("volume") == pytest.approx(1.5)
        ms.set_mic_gain(0.25)
        assert ms._mute.get_property("volume") == pytest.approx(0.25)
        assert all(isinstance(d.key, str) and isinstance(d.label, str) for d in media.audio_devices("input"))
        assert all(isinstance(d.key, str) and isinstance(d.label, str) for d in media.audio_devices("output"))
    finally:
        ms.close()
    asyncio.set_event_loop(None)


@pytest.mark.skipif(not media.available(), reason="GStreamer WebRTC is missing")
def test_answering_a_conversations_offer(monkeypatch):
    """webrtcbin picks Opus and VP8 from everything libwebrtc offers, with the offer's payload types."""
    from ward import aio

    monkeypatch.setenv("WARD_CALL_SOURCES", "test")
    monkeypatch.setenv("WARD_CALL_FAKESINK", "1")
    offer = jingle.jingle_to_sdp(ET.fromstring(CONVERSATIONS_OFFER), True)

    async def answer():
        ms = media.MediaSession(True, True)
        try:
            return await ms.accept_offer(offer)
        finally:
            ms.close()

    loop = aio.install()
    sdp = loop.run_until_complete(answer())
    contents = jingle.sdp_to_jingle(sdp, False)
    by_media = {c.find(jingle.q(jingle.NS_RTP, "description")).get("media"): c for c in contents
                if c.tag == jingle.q(jingle.NS_JINGLE, "content")}
    audio = by_media["audio"].find(jingle.q(jingle.NS_RTP, "description"))
    video = by_media["video"].find(jingle.q(jingle.NS_RTP, "description"))
    first = audio.find(jingle.q(jingle.NS_RTP, "payload-type"))
    assert (first.get("id"), first.get("name").lower()) == ("111", "opus")
    assert ("96", "vp8") in [(p.get("id"), p.get("name").lower())
                             for p in video.findall(jingle.q(jingle.NS_RTP, "payload-type"))]
    assert {c.get("name") for c in by_media.values()} == {"0", "1"}       # the offer's content names
    tr = by_media["audio"].find(jingle.q(jingle.NS_ICE_UDP, "transport"))
    fp = tr.find(jingle.q(jingle.NS_DTLS, "fingerprint"))
    assert fp.get("setup") in ("active", "passive") and fp.text
    assert all(c.get("senders") == "both" for c in by_media.values())
    asyncio.set_event_loop(None)


@pytest.mark.skipif(not media.available() or not (media._have("webrtcdsp") and media._have("webrtcechoprobe")),
                    reason="needs GStreamer's webrtcdsp")
def test_echo_cancellation_starts_with_its_probe(monkeypatch):
    """webrtcdsp looks its webrtcechoprobe up by name when it starts: without one the pipeline failed ("No echo
    probe with name webrtcechoprobe0 found") and calls never connected.  Two calls at once each get their own."""
    from ward import aio

    monkeypatch.setenv("WARD_CALL_SOURCES", "test")
    gst = media.gst()[0]

    async def start_two():
        sessions = [media.MediaSession(True, False), media.MediaSession(True, False)]
        errors = []
        try:
            for ms in sessions:
                ms.connect("error", lambda _m, text: errors.append(text))
                ms.build()
                ms.start()
            for _ in range(40):
                if all(ms.pipe.get_state(0)[1] == gst.State.PLAYING for ms in sessions) or errors:
                    break
                await asyncio.sleep(0.05)
            probes = [ms._echo_probe.get_name() if getattr(ms, "_echo_probe", None) else "" for ms in sessions]
            return errors, probes, [ms.pipe.get_state(0)[1] for ms in sessions]
        finally:
            for ms in sessions:
                ms.close()

    loop = aio.install()
    errors, probes, states = loop.run_until_complete(start_two())
    assert not errors
    assert states == [gst.State.PLAYING, gst.State.PLAYING]
    assert len(set(probes)) == 2
    asyncio.set_event_loop(None)
