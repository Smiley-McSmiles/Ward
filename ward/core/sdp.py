"""The SDP <-> Jingle translation used by the WebRTC call engine.

GStreamer's webrtcbin speaks SDP.  Jingle peers speak XEP-0167 descriptions and XEP-0176 ICE transports.  This
module builds the SDP a peer's Jingle offer asks for, and turns a local SDP offer/answer back into Jingle contents.
It is deliberately independent of GStreamer so the wire format can be tested on its own.
"""

from __future__ import annotations

import secrets

from .jingle import Candidate, Content, Description, Payload, Source, Transport


def _payload_from_rtpmap(pt: int, value: str) -> Payload:
    parts = value.split("/")
    name = parts[0] if parts else ""
    clock = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    channels = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    return Payload(pt, name, clock, channels)


def _candidate_from_sdp(value: str) -> Candidate | None:
    parts = value.split()
    if len(parts) < 8:
        return None
    try:
        cand = Candidate(parts[0], int(parts[1]), parts[2].lower(), int(parts[3]), parts[4], int(parts[5]),
                         parts[7], id="")
    except (ValueError, IndexError):
        return None
    rest = parts[8:]
    i = 0
    while i + 1 < len(rest):
        key, val = rest[i], rest[i + 1]
        if key == "raddr":
            cand.rel_addr = val
        elif key == "rport":
            try:
                cand.rel_port = int(val)
            except ValueError:
                pass
        elif key == "generation":
            try:
                cand.generation = int(val)
            except ValueError:
                pass
        elif key == "tcptype":
            cand.tcp_type = val
        i += 2
    return cand


def contents_from_sdp(sdp: str, creator: str = "initiator") -> list[Content]:
    """Turn a local offer/answer into Jingle contents."""
    contents: list[Content] = []
    current: Content | None = None
    desc: Description | None = None
    trans: Transport | None = None
    pending_rtpmap: dict[int, Payload] = {}
    setup = "actpass"
    in_media = False
    for raw in sdp.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("m="):
            if current is not None and current.description is not None and desc is not None:
                desc.payloads = [pending_rtpmap[k] for k in sorted(pending_rtpmap)]
            parts = line[2:].split()
            media = parts[0] if parts else "audio"
            current = Content(media if not any(c.name == media for c in contents) else "%s%d" % (media, len(contents)),
                              creator=creator)
            desc = Description(media)
            trans = Transport()
            current.description = desc
            current.transport = trans
            pending_rtpmap = {}
            setup = "actpass"
            in_media = True
            contents.append(current)
            continue
        if not in_media or current is None or desc is None or trans is None:
            continue
        if line.startswith("a=mid:"):
            current.name = line[6:].strip() or current.name
        elif line.startswith("a=sendrecv"):
            current.senders = "both"
        elif line.startswith("a=sendonly"):
            current.senders = "initiator"
        elif line.startswith("a=recvonly"):
            current.senders = "responder"
        elif line.startswith("a=inactive"):
            current.senders = "none"
        elif line.startswith("a=rtcp-mux"):
            desc.rtcp_mux = True
        elif line.startswith("a=rtpmap:"):
            rest = line[9:]
            try:
                pt_text, value = rest.split(" ", 1)
                pt = int(pt_text)
            except ValueError:
                continue
            pending_rtpmap[pt] = _payload_from_rtpmap(pt, value)
        elif line.startswith("a=fmtp:"):
            rest = line[7:]
            try:
                pt_text, value = rest.split(" ", 1)
                pt = int(pt_text)
            except ValueError:
                continue
            if pt in pending_rtpmap:
                pending_rtpmap[pt].parameters = value
        elif line.startswith("a=rtcp-fb:"):
            rest = line[10:]
            try:
                pt_text, value = rest.split(" ", 1)
                pt = int(pt_text)
            except ValueError:
                continue
            if pt in pending_rtpmap:
                pending_rtpmap[pt].feedback.append(value)
        elif line.startswith("a=extmap:"):
            rest = line[9:]
            try:
                ident, uri = rest.split(" ", 1)
                desc.extmap.append((int(ident.split("/")[0]), uri))
            except (ValueError, IndexError):
                pass
        elif line.startswith("a=ice-ufrag:"):
            trans.ufrag = line[12:].strip()
        elif line.startswith("a=ice-pwd:"):
            trans.pwd = line[10:].strip()
        elif line.startswith("a=fingerprint:"):
            rest = line[14:].strip()
            try:
                hash_, value = rest.split(" ", 1)
            except ValueError:
                continue
            trans.fingerprints.append((hash_, value, setup))
        elif line.startswith("a=setup:"):
            setup = line[8:].strip() or "actpass"
            if trans.fingerprints:
                hash_, value, _old = trans.fingerprints[-1]
                trans.fingerprints[-1] = (hash_, value, setup)
        elif line.startswith("a=candidate:"):
            cand = _candidate_from_sdp(line[12:].strip())
            if cand is not None:
                trans.candidates.append(cand)
        elif line.startswith("a=ssrc-group:"):
            parts = line[13:].strip().split()
            if parts:
                try:
                    desc.ssrc_groups.append((parts[0], [int(x) for x in parts[1:]]))
                except ValueError:
                    pass
        elif line.startswith("a=ssrc:"):
            rest = line[7:].strip()
            try:
                ssrc_text, values = rest.split(" ", 1)
                ssrc = int(ssrc_text)
            except ValueError:
                continue
            source = next((s for s in desc.sources if s.ssrc == ssrc), None)
            if source is None:
                source = Source(ssrc)
                desc.sources.append(source)
            if ":" in values:
                name, value = values.split(":", 1)
                source.parameters[name] = value
    if desc is not None:
        desc.payloads = [pending_rtpmap[k] for k in sorted(pending_rtpmap)]
    return contents


def sdp_from_contents(contents: list[Content], role: str = "answer") -> str:
    """Build the SDP a Jingle peer's contents describe."""
    session_id = str(int.from_bytes(secrets.token_bytes(8), "big"))
    lines = ["v=0", "o=- %s 2 IN IP4 127.0.0.1" % session_id, "s=-", "t=0 0"]
    mids = [c.name for c in contents if c.description is not None]
    if mids:
        lines.append("a=group:BUNDLE " + " ".join(mids))
    for content in contents:
        desc = content.description
        if desc is None:
            continue
        trans = content.transport or Transport()
        payloads = desc.payloads or [Payload(111, "opus", 48000, 2)]
        pt_list = " ".join(str(p.id) for p in payloads)
        lines += ["m=%s 9 UDP/TLS/RTP/SAVPF %s" % (desc.media, pt_list),
                  "c=IN IP4 0.0.0.0", "a=rtcp:9 IN IP4 0.0.0.0"]
        if trans.ufrag:
            lines.append("a=ice-ufrag:" + trans.ufrag)
        if trans.pwd:
            lines.append("a=ice-pwd:" + trans.pwd)
        for hash_, value, setup in trans.fingerprints:
            lines.append("a=fingerprint:%s %s" % (hash_, value))
            lines.append("a=setup:" + (setup or ("active" if role == "answer" else "actpass")))
            break
        lines += ["a=mid:" + content.name, "a=ice-options:trickle"]
        senders = content.senders or "both"
        if senders == "both":
            lines.append("a=sendrecv")
        elif senders == "initiator":
            lines.append("a=sendonly" if role == "initiator" else "a=recvonly")
        elif senders == "responder":
            lines.append("a=recvonly" if role == "initiator" else "a=sendonly")
        else:
            lines.append("a=inactive")
        if desc.rtcp_mux:
            lines.append("a=rtcp-mux")
        for payload in payloads:
            rtpmap = "%d %s/%d" % (payload.id, payload.name, payload.clockrate or 48000)
            if payload.channels > 1:
                rtpmap += "/%d" % payload.channels
            lines.append("a=rtpmap:" + rtpmap)
            if payload.parameters:
                lines.append("a=fmtp:%d %s" % (payload.id, payload.parameters))
            for feedback in payload.feedback:
                lines.append("a=rtcp-fb:%d %s" % (payload.id, feedback))
        for ident, uri in desc.extmap:
            lines.append("a=extmap:%d %s" % (ident, uri))
        for source in desc.sources:
            for name, value in source.parameters.items():
                lines.append("a=ssrc:%d %s:%s" % (source.ssrc, name, value))
        for semantics, ssrcs in desc.ssrc_groups:
            lines.append("a=ssrc-group:%s %s" % (semantics, " ".join(str(s) for s in ssrcs)))
        for cand in trans.candidates:
            line = "a=candidate:%s %d %s %d %s %d typ %s" % (
                cand.foundation or "1", cand.component, cand.protocol or "udp", cand.priority,
                cand.ip, cand.port, cand.type or "host")
            if cand.rel_addr:
                line += " raddr " + cand.rel_addr
            if cand.rel_port:
                line += " rport %d" % cand.rel_port
            if cand.generation:
                line += " generation %d" % cand.generation
            if cand.tcp_type:
                line += " tcptype " + cand.tcp_type
            lines.append(line)
    return "\r\n".join(lines) + "\r\n"
