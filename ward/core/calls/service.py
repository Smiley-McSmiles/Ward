"""One-to-one audio and video calls, compatible with Conversations, Dino and Gajim.

Calls start with Jingle Message Initiation (XEP-0353): the caller proposes to the contact's bare JID so all their
devices ring; the one that answers sends proceed (and accept to its own account, so the others stop ringing); the
caller then sends the Jingle session-initiate (XEP-0166/0167) to that device and it answers with session-accept.
Candidates trickle in transport-info.  STUN and TURN servers come from core.turn (the server's, XEP-0215, and
those set in Preferences).  A call leaves a line
in the chat: missed, declined, or how long it lasted."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any
from xml.etree import ElementTree as ET

from gi.repository import GObject

from ... import aio, paths, util
from ...i18n import _
from ..chats import find_message_job, insert_message_job
from ..stanzas import NS_HINTS, NS_RECEIPTS
from . import jingle
from .jingle import NS_JINGLE, NS_JMI, q

log = logging.getLogger(__name__)

RING_TIMEOUT = 60           # seconds an unanswered call rings
CONNECT_TIMEOUT = 40        # seconds from answering to media flowing

# a call's end, as kept in its chat line
MISSED, DECLINED, BUSY, CANCELLED, FAILED, ENDED, ELSEWHERE = (
    "missed", "declined", "busy", "cancelled", "failed", "ended", "elsewhere")


class Call(GObject.Object):
    """One call.  state: proposing | ringing (theirs) | incoming (ours) | connecting | active | ended."""

    __gtype_name__ = "WardCall"
    __gsignals__ = {"media": (GObject.SignalFlags.RUN_LAST, None, ())}    # call.ms is there: its video to show
    state = GObject.Property(type=str, default="")
    muted = GObject.Property(type=bool, default=False)
    camera = GObject.Property(type=bool, default=True)
    screen = GObject.Property(type=bool, default=False)

    def __init__(self, account_id: int, sid: str, peer: str, outgoing: bool, media: set[str]):
        super().__init__()
        self.account_id = account_id
        self.sid = sid
        self.peer = util.bare(peer)          # the contact
        self.remote = peer if util.resource(peer) else ""   # their device, once known
        self.outgoing = outgoing
        self.media = set(media) or {"audio"}
        self.video = "video" in self.media
        self.started_at = 0.0                # when media started flowing
        self.created_at = time.time()
        self.end = ""                        # missed, declined... (see above)
        self.reason = ""                     # the Jingle reason the call ended with
        self.error = ""                      # what failed, for the log and the call line
        self.ms: Any = None                  # media.MediaSession
        self.offer: ET.Element | None = None     # a received session-initiate we have not answered yet
        self.local_names: list[str] = []     # content names in our SDP order (= m-line indexes)
        self.remote_names: list[str] = []
        self.local_ice: dict[str, tuple[str, str]] = {}      # content -> (ufrag, pwd)
        self.pending_local: list[tuple[int, str]] = []       # our candidates before the peer knows the session
        self.pending_remote: list[tuple[str, str]] = []      # theirs before we have their description
        self.signalled = False               # session-initiate / session-accept sent: candidates can go
        self.described = False               # their description set: their candidates can go in
        self.timer: asyncio.TimerHandle | None = None

    @property
    def duration(self) -> int:
        return int(time.time() - self.started_at) if self.started_at else 0

    @property
    def active(self) -> bool:
        return self.state not in ("", "ended")


class CallService(GObject.Object):
    __gsignals__ = {
        "incoming": (GObject.SignalFlags.RUN_LAST, None, (object,)),     # a call ringing here
        "added": (GObject.SignalFlags.RUN_LAST, None, (object,)),        # any new call (ours or theirs)
        "ended": (GObject.SignalFlags.RUN_LAST, None, (object,)),
    }
    iq_handlers = {(NS_JINGLE, "jingle"): "_on_jingle"}

    def __init__(self, core):
        super().__init__()
        self.core = core
        self.calls: dict[tuple[int, str], Call] = {}
        self._available: bool | None = None
        settings = getattr(core, "settings", None)
        if settings is not None and hasattr(settings, "connect"):
            settings.connect("changed::calls.mic_gain", lambda *_: self._update_gains())
            settings.connect("changed::calls.speaker_gain", lambda *_: self._update_gains())

    def _update_gains(self) -> None:
        """Preferences apply to a call already in progress."""
        try:
            mic = max(0, int(self.core.settings["calls.mic_gain"])) / 100.0
            speaker = max(0, int(self.core.settings["calls.speaker_gain"])) / 100.0
        except (KeyError, TypeError, ValueError):
            return
        for call in self.calls.values():
            if call.ms is not None:
                call.ms.set_mic_gain(mic)
                call.ms.set_speaker_gain(speaker)

    # ================================================================== availability
    def available(self) -> bool:
        if self._available is None:
            from . import media

            self._available = media.available()
        return self._available

    def attach(self, session) -> None:
        if self.available() and self.core.settings["calls.enabled"]:
            for ns in jingle.FEATURES:
                session.add_feature(ns)

    def account_offline(self, account_id: int) -> None:
        """The account lost its connection: its calls cannot be signalled any more."""
        for call in [c for c in self.calls.values() if c.account_id == account_id]:
            end = FAILED if call.state in ("connecting", "active") else (CANCELLED if call.outgoing else MISSED)
            aio.spawn(self._end(call, end))

    def detach(self, account_id: int) -> None:
        self.account_offline(account_id)

    async def can_call(self, account_id: int, jid: str) -> set[str]:
        """What the contact's devices online can take: {'audio', 'video'}, empty when none can."""
        session = self.core.sessions.get(account_id)
        contact = self.core.roster.contacts.get(account_id, util.bare(jid))
        if session is None or not session.online or contact is None or not self.available():
            return set()
        out: set[str] = set()
        for res, info in list(contact.resources.items()):
            node = info.get("caps") or ""
            full = "%s/%s" % (contact.jid, res)
            try:
                di = await self.core.disco.caps_info(session, full, node) if node else \
                    await self.core.disco.info(session, full)
            except Exception:
                continue
            f = di.features if di is not None else set()
            if {jingle.NS_JINGLE, jingle.NS_RTP, jingle.NS_ICE_UDP, jingle.NS_DTLS} <= f:
                if jingle.NS_RTP_AUDIO in f:
                    out.add("audio")
                if jingle.NS_RTP_VIDEO in f and jingle.NS_RTP_AUDIO in f:
                    out.add("video")
        return out

    def active_call(self) -> Call | None:
        return next((c for c in self.calls.values() if c.state in ("connecting", "active")), None)

    def find(self, account_id: int, sid: str) -> Call | None:
        return self.calls.get((account_id, sid))

    def _trace(self, call: Call, text: str) -> None:
        """Always keep a short call trace: diagnosing a call between different clients needs more than "failed"."""
        try:
            directory = paths.ensure(paths.state_dir())
            path = os.path.join(directory, "calls.log")
            if os.path.exists(path) and os.path.getsize(path) > 512 * 1024:
                os.replace(path, path + ".1")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("%s %s %s [%s]: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), call.account_id,
                                                   call.sid, call.state or "new", text))
        except OSError:
            pass

    # ================================================================== starting and answering
    async def start(self, acc, jid: str, video: bool) -> Call:
        """Call a contact: all their devices ring until one answers."""
        session = self.core.sessions[acc.id]
        sid = util.random_id(16)
        call = Call(acc.id, sid, util.bare(jid), True, {"audio", "video"} if video else {"audio"})
        self.calls[(acc.id, sid)] = call
        msg = session.new_message(call.peer, "chat", "jm-propose-" + sid)
        msg.append(jingle.jmi("propose", sid, call.media))
        ET.SubElement(msg, q(NS_RECEIPTS, "request"))
        ET.SubElement(msg, q(NS_HINTS, "store"))
        session.send(msg)
        self._set(call, "proposing")
        self.emit("added", call)
        self._timeout(call, RING_TIMEOUT, lambda: self.hang_up(call))
        await self._record(call)
        return call

    async def accept(self, call: Call, video: bool | None = None) -> None:
        """Answer a ringing call (with our camera when video is True and they proposed video)."""
        if call.state != "incoming":
            return
        session = self.core.sessions.get(call.account_id)
        if session is None:
            return
        if video is not None:
            call.video = video and "video" in call.media
        other = self.active_call()
        if other is not None and other is not call:
            await self.hang_up(other)
        self._set(call, "connecting")
        self._timeout(call, CONNECT_TIMEOUT, lambda: self._fail(call, "connectivity-error"))
        if call.offer is None:
            # Jingle Message Initiation: tell our other devices, then the caller's device
            own = session.new_message(util.bare(session.jid), "chat")
            own.append(jingle.jmi("accept", call.sid))
            ET.SubElement(own, q(NS_HINTS, "store"))
            session.send(own)
            msg = session.new_message(call.remote or call.peer, "chat", "jm-proceed-" + call.sid)
            msg.append(jingle.jmi("proceed", call.sid))
            ET.SubElement(msg, q(NS_HINTS, "store"))
            session.send(msg)
        else:
            await self._answer(session, call)

    async def decline(self, call: Call) -> None:
        session = self.core.sessions.get(call.account_id)
        if session is not None and call.state == "incoming":
            if call.offer is not None:
                self._terminate(session, call, "decline")
            else:
                msg = session.new_message(call.remote or call.peer, "chat")
                msg.append(jingle.jmi("reject", call.sid, reason="decline"))
                ET.SubElement(msg, q(NS_HINTS, "store"))
                session.send(msg)
        await self._end(call, DECLINED)

    async def hang_up(self, call: Call) -> None:
        if not call.active:
            return
        session = self.core.sessions.get(call.account_id)
        if call.state == "incoming":
            await self.decline(call)
            return
        if session is not None and session.online:
            if call.state in ("proposing", "ringing"):
                msg = session.new_message(call.peer, "chat")
                msg.append(jingle.jmi("retract", call.sid, reason="cancel"))
                ET.SubElement(msg, q(NS_HINTS, "store"))
                session.send(msg)
            else:
                self._terminate(session, call, "success" if call.state == "active" else "cancel")
                if call.remote:
                    fin = session.new_message(call.remote, "chat")
                    fin.append(jingle.jmi("finish", call.sid,
                                          reason="success" if call.state == "active" else "cancel"))
                    ET.SubElement(fin, q(NS_HINTS, "store"))
                    session.send(fin)
        await self._end(call, ENDED if call.state == "active" else CANCELLED)

    def set_muted(self, call: Call, muted: bool) -> None:
        call.muted = muted
        if call.ms is not None:
            call.ms.set_muted(muted)

    def set_camera(self, call: Call, on: bool) -> None:
        call.camera = on
        if call.ms is not None:
            call.ms.set_camera(on)

    def set_screen(self, call: Call, on: bool) -> None:
        """Share the desktop; raises when the desktop's screen chooser is unavailable or cancelled."""
        if call.ms is None:
            raise RuntimeError(_("The call is not connected"))
        call.ms.set_screen(on)
        call.screen = on

    # ================================================================== Jingle Message Initiation
    async def handle_message(self, session, pm, inner: ET.Element, archived: bool, outgoing: bool) -> bool:
        """A message carrying a JMI element: handled here (True), or not one (False)."""
        el = next((c for c in inner if c.tag.startswith("{%s}" % NS_JMI)), None)
        if el is None:
            return False
        kind = el.tag.split("}")[1]
        sid = el.get("id", "")
        if not sid or pm.type == "groupchat":
            return True
        acc = session.account
        frm = pm.from_jid or session.jid
        own = util.bare(session.jid)
        if not archived and pm.delay and time.time() - pm.delay > RING_TIMEOUT:
            archived = True         # stored while we were offline: too late to ring
        if archived:
            await self._archived(session, kind, sid, pm, outgoing, el)
            return True
        call = self.find(acc.id, sid)
        from_self = util.bare(frm) == own
        log.info("call %s: JMI %s from %s (outgoing=%s, archived=%s)", sid, kind, frm, outgoing, archived)
        if call is not None:
            self._trace(call, "JMI %s from %s (outgoing=%s, archived=%s)" % (kind, frm, outgoing, archived))
        if kind == "propose":
            if from_self or outgoing:
                return True             # we called from another device
            if call is not None:
                return True
            media = jingle.media_of(el)
            if not self.available() or not self.core.settings["calls.enabled"] or not media:
                self._jmi(session, frm, "reject", sid, "unsupported-applications" if media else "")
                return True
            contact = self.core.roster.contacts.get(acc.id, util.bare(frm))
            if contact is not None and contact.blocked:
                return True
            busy = self.active_call()
            call = Call(acc.id, sid, frm, False, media)
            if busy is not None:
                self._jmi(session, frm, "reject", sid, "busy")
                call.end = BUSY
                await self._record(call)
                return True
            self.calls[(acc.id, sid)] = call
            self._jmi(session, frm, "ringing", sid)
            self._set(call, "incoming")
            self._timeout(call, RING_TIMEOUT, lambda: self._end(call, MISSED))
            await self._record(call)
            self.emit("added", call)
            self.emit("incoming", call)
            return True
        if call is None:
            return True
        if kind == "retract" and not call.outgoing and util.bare(frm) == call.peer:
            await self._end(call, MISSED)
        elif kind == "ringing" and call.outgoing and util.bare(frm) == call.peer and call.state == "proposing":
            self._set(call, "ringing")
        elif kind in ("proceed", "accept") and call.outgoing and util.bare(frm) == call.peer and not outgoing:
            # XEP-0353: the answering device sends proceed to the caller.  Some older Conversations/Snikket
            # builds send accept to the caller instead; take that as proceed too.
            if call.state in ("proposing", "ringing"):
                call.remote = frm
                self._set(call, "connecting")
                self._timeout(call, CONNECT_TIMEOUT, lambda: self._fail(call, "connectivity-error"))
                self._trace(call, "answered with JMI %s from %s" % (kind, frm))
                aio.spawn(self._initiate(session, call))
        elif kind in ("proceed", "accept") and not call.outgoing and (from_self or outgoing):
            if call.state == "incoming":
                await self._end(call, ELSEWHERE)            # answered on another of our devices
        elif kind == "reject":
            if call.outgoing and util.bare(frm) == call.peer and not outgoing:
                await self._end(call, BUSY if jingle.reason_of(el) == "busy" else DECLINED)
            elif not call.outgoing and (from_self or outgoing) and call.state == "incoming":
                await self._end(call, DECLINED)            # declined on another of our devices
        elif kind == "finish" and util.bare(frm) == call.peer and call.active:
            await self._end(call, ENDED if call.started_at else CANCELLED)
        return True

    async def _archived(self, session, kind: str, sid: str, pm, outgoing: bool, el: ET.Element) -> None:
        """Calls we were offline for: a line in the chat."""
        acc = session.account
        if kind != "propose" or self.find(acc.id, sid) is not None:
            return
        peer = pm.to_jid if outgoing else pm.from_jid
        if not peer:
            return
        call = Call(acc.id, sid, peer, outgoing, jingle.media_of(el))
        call.end = CANCELLED if outgoing else MISSED
        call.created_at = pm.delay or time.time()
        await self._record(call, only_new=True)

    def _jmi(self, session, to: str, kind: str, sid: str, reason: str = "") -> None:
        msg = session.new_message(to, "chat")
        msg.append(jingle.jmi(kind, sid, reason=reason))
        ET.SubElement(msg, q(NS_HINTS, "store"))
        session.send(msg)

    # ================================================================== Jingle
    def _on_jingle(self, session, stanza) -> None:
        el = stanza.xml
        j = el.find(q(NS_JINGLE, "jingle"))
        frm = el.get("from", "")
        if j is None:
            session.reply_iq(stanza, error="bad-request")
            return
        action, sid = j.get("action", ""), j.get("sid", "")
        call = self.find(session.account.id, sid)
        log.info("call %s: jingle %s from %s", sid, action, frm)
        if call is not None:
            self._trace(call, "Jingle %s from %s" % (action, frm))
        if action == "session-initiate":
            if call is None:
                # a call without Jingle Message Initiation (some clients start with Jingle directly)
                media = jingle.media_of(j)
                if not media or not self.available() or not self.core.settings["calls.enabled"]:
                    session.reply_iq(stanza, error="feature-not-implemented")
                    return
                if self.active_call() is not None:
                    session.reply_iq(stanza)
                    call = Call(session.account.id, sid, frm, False, media)
                    self._terminate(session, call, "busy")
                    return
                call = Call(session.account.id, sid, frm, False, media)
                call.offer = j
                self.calls[(session.account.id, sid)] = call
                session.reply_iq(stanza)
                self._set(call, "incoming")
                self._timeout(call, RING_TIMEOUT, lambda: self._end(call, MISSED))
                aio.spawn(self._record(call))
                self.emit("added", call)
                self.emit("incoming", call)
                return
            if call.outgoing or util.bare(frm) != call.peer:
                session.reply_iq(stanza, error="item-not-found")
                return
            if util.resource(frm):
                call.remote = frm       # the resource that sends the session is the one to answer, even after a rebind
            session.reply_iq(stanza)
            call.offer = j
            if call.state == "connecting":
                aio.spawn(self._answer(session, call))
            return
        if call is None or (frm and util.bare(frm) != call.peer):
            session.reply_iq(stanza, error="item-not-found")
            return
        if util.resource(frm):
            call.remote = frm
        session.reply_iq(stanza)
        if action == "session-accept" and call.outgoing:
            aio.spawn(self._accepted(call, j))
        elif action == "transport-info":
            self._remote_candidates(call, j)
        elif action == "session-terminate":
            reason = jingle.reason_of(j)
            call.reason = reason
            call.error = jingle.text_of(j) or reason
            log.info("call %s: %s ended it: %s%s", call.sid, call.remote or call.peer, reason or "no reason",
                     (" (%s)" % jingle.text_of(j)) if jingle.text_of(j) else "")
            self._trace(call, "session-terminate from %s: %s%s" % (
                call.remote or call.peer, reason or "no reason",
                (" (%s)" % jingle.text_of(j)) if jingle.text_of(j) else ""))
            end = {"decline": DECLINED, "busy": BUSY, "success": ENDED, "cancel": CANCELLED}.get(reason, FAILED)
            if end == CANCELLED and call.state == "incoming":
                end = MISSED
            aio.spawn(self._end(call, ENDED if call.started_at and end in (CANCELLED, ENDED) else end))
        # session-info (ringing, mute...) and anything else: acknowledged, nothing to do

    def _terminate(self, session, call: Call, reason: str) -> None:
        if not (call.remote or call.peer):
            return
        j = jingle.jingle_element("session-terminate", call.sid)
        j.append(jingle.reason_element(reason))
        aio.spawn(self._send_jingle(session, call, j, quiet=True))

    async def _send_jingle(self, session, call: Call, j: ET.Element, quiet: bool = False) -> bool:
        to = call.remote or call.peer
        try:
            await session.iq("set", to, j, timeout=20)
            return True
        except Exception as e:
            if not quiet:
                log.info("call %s: jingle %s to %s failed: %s", call.sid, j.get("action"), to, e)
            return False

    # ------------------------------------------------------------------ negotiation
    async def _media(self, session, call: Call, send_video: bool | None = None,
                     receive_video: bool | None = None):
        from . import media

        stun, turns = await self.core.turn.uris(session)
        video = call.video if send_video is None else send_video
        settings = self.core.settings
        try:
            mic_gain = max(0, int(settings.get("calls.mic_gain", 100))) / 100.0
            speaker_gain = max(0, int(settings.get("calls.speaker_gain", 100))) / 100.0
        except (TypeError, ValueError):
            mic_gain = speaker_gain = 1.0
        self._trace(call, "ICE servers: %s, %d TURN; microphone %r, speaker %r" % (
            stun or "no STUN", len(turns), settings.get("calls.mic_device", ""),
            settings.get("calls.speaker_device", "")))
        ms = media.MediaSession(True, video, stun, turns, bool(settings.get("calls.relay_only", False)),
                                receive_video=video if receive_video is None else receive_video,
                                mic_device=str(settings.get("calls.mic_device", "") or ""),
                                speaker_device=str(settings.get("calls.speaker_device", "") or ""),
                                mic_gain=mic_gain, speaker_gain=speaker_gain)
        ms.set_muted(call.muted)
        ms.set_camera(call.camera)
        ms.connect("local-candidate", lambda _m, mline, cand: self._local_candidate(session, call, mline, cand))
        ms.connect("state-changed", lambda _m, state: self._media_state(call, state))
        ms.connect("error", lambda _m, text: self._media_error(call, text))
        call.ms = ms
        call.emit("media")
        return ms

    async def _initiate(self, session, call: Call) -> None:
        """We called and they answered: the session-initiate, to the device that answered."""
        try:
            ms = await self._media(session, call)
            offer = await ms.create_offer()
        except Exception as e:
            log.warning("call %s: cannot start the media: %s", call.sid, e)
            await self._fail(call, "failed-application", error=str(e))
            return
        if not call.active:
            return
        self._learn_local(call, offer)
        await self._gather(call)
        contents = jingle.sdp_to_jingle(offer, True, with_candidates=False)
        self._embed_candidates(contents, call)
        j = jingle.jingle_element("session-initiate", call.sid, initiator=session.bound_jid or session.jid)
        for el in contents:
            j.append(el)
        log.info("call %s: session-initiate to %s: media %s, %d contents, %d local candidates",
                 call.sid, call.remote, sorted(call.media), len(call.local_names), self._candidate_count(j))
        self._trace(call, "session-initiate to %s: media %s, contents %s, local candidates %d" % (
            call.remote, sorted(call.media), call.local_names, self._candidate_count(j)))
        if not await self._send_jingle(session, call, j):
            await self._fail(call, "connectivity-error", send=False, error="the session-initiate was not accepted")
            return
        self._flush_local(session, call)

    async def _accepted(self, call: Call, j: ET.Element) -> None:
        if call.ms is None:
            return
        call.remote_names = [c.get("name", "") for c in j.findall(q(NS_JINGLE, "content"))]
        try:
            await call.ms.set_answer(jingle.jingle_to_sdp(j, False))
        except Exception as e:
            log.warning("call %s: cannot use the answer: %s", call.sid, e)
            await self._fail(call, "failed-application", error=str(e))
            return
        call.described = True
        log.info("call %s: session-accept from %s: %d contents, %d remote candidates",
                 call.sid, call.remote, len(call.remote_names), self._candidate_count(j))
        embedded = [c for c in (jingle.Candidate.from_xml(e)
                               for e in j.iter(q(jingle.NS_ICE_UDP, "candidate"))) if c is not None]
        self._trace(call, "session-accept from %s: contents %s, remote candidates %s" % (
            call.remote, call.remote_names,
            ", ".join("%s/%s:%d" % (c.type, c.ip, c.port) for c in embedded) or "none"))
        self._flush_remote(call)

    async def _answer(self, session, call: Call) -> None:
        """They sent the session-initiate: our session-accept."""
        j = call.offer
        if j is None or call.ms is not None:
            return
        call.remote_names = [c.get("name", "") for c in j.findall(q(NS_JINGLE, "content"))]
        offered = jingle.media_of(j)
        try:
            ms = await self._media(session, call, send_video=call.video and "video" in offered,
                                   receive_video="video" in offered)
            answer = await ms.accept_offer(jingle.jingle_to_sdp(j, True))
        except Exception as e:
            log.warning("call %s: cannot answer the media: %s", call.sid, e)
            await self._fail(call, "failed-application", error=str(e))
            return
        if not call.active:
            return
        call.described = True
        await self._gather(call)
        contents = jingle.sdp_to_jingle(answer, False, with_candidates=False)
        names = self._match_names(contents, call.remote_names)
        self._learn_local(call, answer, names)
        self._embed_candidates(contents, call)
        acc = jingle.jingle_element("session-accept", call.sid, responder=session.bound_jid or session.jid)
        for el in contents:
            acc.append(el)
        log.info("call %s: session-accept to %s: media %s, %d contents, %d local candidates",
                 call.sid, call.remote, sorted(offered), len(call.local_names), self._candidate_count(acc))
        self._trace(call, "session-accept to %s: media %s, contents %s, local candidates %d" % (
            call.remote, sorted(offered), call.local_names, self._candidate_count(acc)))
        if not await self._send_jingle(session, call, acc):
            await self._fail(call, "connectivity-error", send=False, error="the session-accept was not accepted")
            return
        self._flush_local(session, call)
        self._flush_remote(call)

    def _learn_local(self, call: Call, sdp_text: str, names: list[str] | None = None) -> None:
        sdp = jingle.parse_sdp(sdp_text)
        mids = [m.mid for m in sdp.media]
        names = names if names and len(names) == len(mids) else mids
        call.local_names = list(names)
        call.local_ice = {}
        for m, name in zip(sdp.media, names, strict=False):
            call.local_ice[name] = (m.get("ice-ufrag") or sdp.get("ice-ufrag") or "",
                                    m.get("ice-pwd") or sdp.get("ice-pwd") or "")

    @staticmethod
    def _match_names(contents: list[ET.Element], offered: list[str]) -> list[str]:
        """The offer's content names for our contents, by position: some webrtcbin versions rename the mids."""
        own = [c.get("name", "") for c in contents if c.tag == q(NS_JINGLE, "content")]
        if len(own) != len(offered) or not offered:
            return own
        old_new = dict(zip(own, offered, strict=False))
        for content in contents:
            if content.tag == q(NS_JINGLE, "content"):
                content.set("name", old_new.get(content.get("name", ""), content.get("name", "")))
            elif content.tag == q(jingle.NS_GROUPING, "group"):
                for ref in content.findall(q(jingle.NS_GROUPING, "content")):
                    ref.set("name", old_new.get(ref.get("name", ""), ref.get("name", "")))
        return list(offered)

    async def _gather(self, call: Call, wait: float = 0.75) -> None:
        """Give ICE a moment to gather host candidates, so the session-initiate/accept can carry some too."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while loop.time() < deadline:
            if any((c := jingle.Candidate.from_sdp(raw)) is not None and c.protocol == "udp"
                   for _ml, raw in call.pending_local):
                return
            await asyncio.sleep(0.05)

    def _embed_candidates(self, contents: list[ET.Element], call: Call) -> None:
        """Move the candidates gathered so far into the session-initiate/accept (and still trickle later ones)."""
        pending, call.pending_local = call.pending_local, []
        by_name: dict[str, list[jingle.Candidate]] = {}
        for mline, raw in pending:
            c = jingle.Candidate.from_sdp(raw)
            if c is None or c.protocol != "udp" or not (0 <= mline < len(call.local_names)):
                continue
            by_name.setdefault(call.local_names[mline], []).append(c)
        if not by_name:
            return
        added = jingle.add_candidates(contents, by_name)
        if added:
            log.info("call %s: %d local candidates in the session description", call.sid, added)
            self._trace(call, "local candidates: %s" % ", ".join(
                "%s/%s:%d" % (c.type, c.ip, c.port) for group in by_name.values() for c in group))

    @staticmethod
    def _candidate_count(j: ET.Element) -> int:
        return len(j.findall(".//{%s}candidate" % jingle.NS_ICE_UDP))

    def _local_candidate(self, session, call: Call, mline: int, cand: str) -> None:
        call.pending_local.append((mline, cand))
        if call.signalled:
            self._flush_local(session, call)

    def _flush_local(self, session, call: Call) -> None:
        call.signalled = True
        pending, call.pending_local = call.pending_local, []
        for mline, cand in pending:
            c = jingle.Candidate.from_sdp(cand)
            if c is None or c.protocol != "udp" or not (0 <= mline < len(call.local_names)):
                continue
            name = call.local_names[mline]
            ufrag, pwd = call.local_ice.get(name, ("", ""))
            self._trace(call, "local candidate %s/%s:%d in %s" % (c.type, c.ip, c.port, name))
            aio.spawn(self._send_jingle(session, call, jingle.transport_info(call.sid, name, ufrag, pwd, c),
                                        quiet=True))

    def _remote_candidates(self, call: Call, j: ET.Element) -> None:
        added = []
        for content in j.findall(q(NS_JINGLE, "content")):
            tr = content.find(q(jingle.NS_ICE_UDP, "transport"))
            if tr is None:
                continue
            for c in tr.findall(q(jingle.NS_ICE_UDP, "candidate")):
                cand = jingle.Candidate.from_xml(c)
                if cand is not None:
                    call.pending_remote.append((content.get("name", ""), cand.to_sdp()))
                    added.append(cand)
        if added:
            self._trace(call, "remote candidates: %s" % ", ".join(
                "%s/%s:%d" % (c.type, c.ip, c.port) for c in added))
        if call.described:
            self._flush_remote(call)

    def _flush_remote(self, call: Call) -> None:
        pending, call.pending_remote = call.pending_remote, []
        names = call.local_names or call.remote_names
        for name, cand in pending:
            if call.ms is None:
                continue
            # our names are the m-line order; the peer's answer may use different names (some webrtcbin versions)
            if name in names:
                index = names.index(name)
            elif name in call.remote_names:
                index = call.remote_names.index(name)
            else:
                log.info("call %s: dropping a candidate for unknown content %r (known: %s)",
                         call.sid, name, ", ".join(names))
                continue
            call.ms.add_remote_candidate(index, cand)

    def _media_state(self, call: Call, state: str) -> None:
        log.info("call %s: media %s -> %s", call.sid, call.ms.state if call.ms is not None else "?", state)
        if state == "connected" and call.state == "connecting":
            call.started_at = time.time()
            self._cancel_timer(call)
            self._set(call, "active")
        elif state == "failed" and call.active:
            aio.spawn(self._fail(call, "connectivity-error", error="the media connection failed"))

    def _media_error(self, call: Call, text: str) -> None:
        """A GStreamer error (no microphone, no camera, a codec): end the call instead of waiting for the timeout."""
        log.warning("call %s: media error: %s", call.sid, text)
        self._trace(call, "media error: %s" % text)
        if call.active:
            aio.spawn(self._fail(call, "failed-application", error=text))

    # ================================================================== ending
    async def _fail(self, call: Call, reason: str, send: bool = True, error: str = "") -> None:
        if not call.active:
            return
        call.reason = reason
        call.error = error or reason
        log.info("call %s failed (%s): %s", call.sid, reason, call.error)
        self._trace(call, "failed (%s): %s" % (reason, call.error))
        session = self.core.sessions.get(call.account_id)
        if send and session is not None and call.state in ("connecting", "active"):
            self._terminate(session, call, reason)
        await self._end(call, FAILED)

    async def _end(self, call: Call, end: str) -> None:
        if call.state == "ended":
            return
        self._cancel_timer(call)
        if call.ms is not None:
            with contextlib.suppress(Exception):
                call.ms.close()
            call.ms = None
        call.end = end
        self._set(call, "ended")
        self.calls.pop((call.account_id, call.sid), None)
        await self._record(call)
        self.emit("ended", call)

    def _set(self, call: Call, state: str) -> None:
        if call.state != state:
            log.info("call %s: %s -> %s", call.sid, call.state or "new", state)
            call.state = state
            self._trace(call, "state -> %s" % state)

    def _timeout(self, call: Call, seconds: float, action) -> None:
        self._cancel_timer(call)

        def fire():
            call.timer = None
            if call.active:
                aio.spawn(action())
        call.timer = asyncio.get_event_loop().call_later(seconds, fire)

    def _cancel_timer(self, call: Call) -> None:
        if call.timer is not None:
            call.timer.cancel()
            call.timer = None

    # ================================================================== the line in the chat
    async def _record(self, call: Call, only_new: bool = False) -> None:
        acc = self.core.accounts.get(call.account_id)
        if acc is None:
            return
        chat = await self.core.open_chat(acc, call.peer)
        extra = {"call": {"sid": call.sid, "media": sorted(call.media), "end": call.end,
                          "duration": call.duration if call.end == ENDED else 0, "outgoing": call.outgoing,
                          "reason": call.reason, "error": call.error}}
        values = {"chat_id": chat.id, "msg_id": call.sid, "sender": acc.jid if call.outgoing else call.peer,
                  "sender_jid": "", "direction": 1 if call.outgoing else 0,
                  "timestamp": int(call.created_at * 1000), "body": "", "kind": "call", "status": 0,
                  "extra": extra}

        def job(conn):
            row = find_message_job(conn, chat.id, call.sid)
            if row is not None and row["kind"] == "call":
                if only_new:
                    return None, False
                import json

                conn.execute("UPDATE messages SET extra = ? WHERE id = ?",
                             (json.dumps(extra, ensure_ascii=False), row["id"]))
                return row["id"], False
            return insert_message_job(conn, values), True

        mid, new = await self.core.db.run(job)
        if mid is None:
            return
        msg = await self.core.chats.reload_message(chat, mid)
        if msg is None:
            return
        if new:
            self.core.chats.show_message(chat, msg)
        if call.end == MISSED and not call.outgoing:
            chat.unread += 1
            self.core.chats.emit("unread-changed")
            await self.core.chats.save(chat, "unread")

    def summary(self, extra: dict) -> str:
        from ...i18n import _

        c = extra.get("call") or {}
        video = "video" in (c.get("media") or [])
        end = c.get("end", "")
        if not end:
            return _("Video call") if video else _("Call")
        if end == MISSED:
            return _("Missed video call") if video else _("Missed call")
        if end in (DECLINED, BUSY) and c.get("outgoing"):
            return _("Busy") if end == BUSY else _("Call declined")
        if end == DECLINED:
            return _("Declined call")
        if end == ENDED and c.get("duration"):
            d = int(c["duration"])
            length = "%d:%02d:%02d" % (d // 3600, d // 60 % 60, d % 60) if d >= 3600 else "%d:%02d" % (d // 60, d % 60)
            return (_("Video call, %s") if video else _("Call, %s")) % length
        if end == ELSEWHERE:
            return _("Answered on another device")
        if end == FAILED:
            return _("Call failed")
        return _("Cancelled call") if c.get("outgoing") else _("Missed call")
