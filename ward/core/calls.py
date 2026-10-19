"""Jingle calls: XEP-0353 invites, XEP-0166 sessions and the GStreamer media behind them.

A call has two connections: the invite (Jingle Message Initiation, messages) and the session (Jingle, IQ).  The
media engine is created only after the other side accepts, so an incoming call does not open the microphone or
camera while it is still ringing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from gi.repository import Gio, GLib, GObject

from .. import aio, util
from ..i18n import _
from . import jingle
from .chats import insert_message_job
from .jingle import Candidate, Content
from .models import STATUS_SENT, AccountItem, CallItem
from .rtp import CallMedia
from .rtp import available as media_available
from .turn import TurnService

log = logging.getLogger("ward.calls")

RING_SECONDS = 60


class CallError(Exception):
    pass


@dataclass
class _Call:
    call: CallItem
    session: Any
    peer: str
    offer: list[Content] = field(default_factory=list)
    answer: list[Content] = field(default_factory=list)
    media: CallMedia | None = None
    contents: list[Content] = field(default_factory=list)
    pending_candidates: list[tuple[int, Candidate]] = field(default_factory=list)
    remote_candidates: list[tuple[str, Candidate]] = field(default_factory=list)
    timeout: int = 0
    answered: bool = False


def _format_duration(seconds: int) -> str:
    if seconds >= 3600:
        return "%d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)
    return "%d:%02d" % (seconds // 60, seconds % 60)


class Calls(GObject.Object):
    __gsignals__ = {
        "incoming": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "ended": (GObject.SignalFlags.RUN_LAST, None, (object, str)),
    }
    iq_handlers = {(jingle.NS_JINGLE, "jingle"): "_on_jingle_iq"}

    def __init__(self, core):
        super().__init__()
        self.core = core
        self.turn: TurnService = core.turn
        self.store = Gio.ListStore.new(CallItem)
        self.active: CallItem | None = None
        self._calls: dict[str, _Call] = {}
        self._sessions: dict[int, object] = {}

    # ================================================================== sessions
    def attach(self, session) -> None:
        self._sessions[session.account.id] = session
        session.connect("message", lambda s, el: self._on_message(s, el))

    def detach(self, account_id: int) -> None:
        self._sessions.pop(account_id, None)

    def _session_for(self, account_id: int):
        return self._sessions.get(account_id)

    def account_offline(self, account_id: int) -> None:
        for sid, info in list(self._calls.items()):
            if info.call.account_id == account_id:
                self._finish(sid, _("Account disconnected"), notify=True)
        self._sessions.pop(account_id, None)

    # ================================================================== placing
    def _busy(self) -> bool:
        """Whether a call is still live.  A window or a notification can outlive its call for a moment; do not
        let that stale object block the next call."""
        if self.active is None:
            return False
        info = self._calls.get(self.active.sid)
        if info is None or self.active.state == "ended":
            self.active = None
            return False
        return True

    def busy(self) -> bool:
        return self._busy()

    async def place_call(self, account: AccountItem, peer: str, video: bool = False) -> CallItem:
        session = self._session_for(account.id)
        if session is None or not getattr(session, "online", False):
            raise CallError(_("You are not connected"))
        if self._busy():
            raise CallError(_("You are already in a call"))
        if not media_available():
            raise CallError(_("Calling needs GStreamer (webrtcbin and libnice-gstreamer)"))
        sid = jingle.new_sid()
        media = "video" if video else "audio"
        call = CallItem(sid=sid, account_id=account.id, jid=util.bare(peer), name=self.core.names.contact_name(
            account.id, peer), direction=1, media=media, state="ringing", video=video)
        info = _Call(call, session, peer)
        self._calls[sid] = info
        self.active = call
        self.store.append(call)
        try:
            self._send_jmi(session, peer, sid, "propose", ("audio", "video") if video else ("audio",))
        except Exception:
            self._finish(sid, "", notify=False)
            raise
        info.timeout = GLib.timeout_add_seconds(RING_SECONDS, self._ring_timeout, sid)
        return call

    def _ring_timeout(self, sid: str) -> bool:
        info = self._calls.get(sid)
        if info is not None and info.call.state in ("incoming", "ringing", "connecting"):
            self._finish(sid, _("No answer"), notify=True)
        return False

    def _send_jmi(self, session, peer: str, sid: str, action: str, media: tuple[str, ...] = ()) -> None:
        msg = session.new_message(peer, "chat")
        msg.append(jingle.build_jmi(sid, action, media))
        session.send(msg)

    # ================================================================== incoming invites
    def _on_message(self, session, el) -> None:
        jm = el.find(jingle.q(jingle.NS_JMI, "jingle-message"))
        if jm is None:
            return
        sid, action, media = jingle.parse_jmi(jm)
        if not sid or not action:
            return
        aio.spawn(self._on_jmi(session, el.get("from") or "", sid, action, media), name="jingle invite")

    async def _on_jmi(self, session, peer: str, sid: str, action: str, media: tuple[str, ...]) -> None:
        info = self._calls.get(sid)
        if action == "propose":
            if self._busy():
                self._send_jmi(session, peer, sid, "reject")
                return
            if info is not None:
                return
            video = "video" in media
            call = CallItem(sid=sid, account_id=session.account.id, jid=util.bare(peer),
                            name=self.core.names.contact_name(session.account.id, peer), direction=0,
                            media="video" if video else "audio", state="incoming", video=video)
            info = _Call(call, session, peer)
            self._calls[sid] = info
            self.active = call
            self.store.append(call)
            info.timeout = GLib.timeout_add_seconds(RING_SECONDS, self._ring_timeout, sid)
            self.emit("incoming", call)
            return
        if info is None:
            return
        if action == "accept" and info.call.direction == 1:
            self._clear_timeout(info)
            info.call.state = "connecting"
            await self._start_offer(info)
        elif action in ("reject", "retract"):
            self._finish(sid, _("Declined") if action == "reject" else _("Cancelled"), notify=True)
        elif action == "proceed":
            info.call.state = "connecting"
        elif action == "finish":
            self._finish(sid, "", notify=True)

    # ================================================================== accepting
    async def accept(self, call: CallItem) -> None:
        info = self._calls.get(call.sid)
        if info is None or call.direction != 0:
            return
        self._clear_timeout(info)
        info.answered = True
        info.call.state = "connecting"
        if not info.offer:
            self._send_jmi(info.session, info.peer, call.sid, "accept")
            return
        await self._answer(info)

    async def reject(self, call: CallItem) -> None:
        info = self._calls.get(call.sid)
        if info is None:
            if self.active is call:
                self.active = None
            return
        try:
            self._send_jmi(info.session, info.peer, call.sid, "reject")
        except Exception as e:
            log.info("could not reject call %s: %s", call.sid, e)
        # Let the next call start immediately; the Jingle session-terminate can finish in the background.
        self._finish(call.sid, _("Declined"), notify=True)
        if info.offer:
            await self._send_terminate(info, _("Declined"))

    async def hangup(self, call: CallItem) -> None:
        info = self._calls.get(call.sid)
        if info is None:
            if self.active is call:
                self.active = None
            return
        try:
            self._send_jmi(info.session, info.peer, call.sid, "finish")
        except Exception as e:
            log.info("could not end call %s: %s", call.sid, e)
        # Let the next call start immediately; the Jingle session-terminate can finish in the background.
        self._finish(call.sid, "", notify=True)
        if info.offer or info.answer:
            await self._send_terminate(info, _("Call ended"))

    # ================================================================== Jingle session
    async def _start_offer(self, info: _Call) -> None:
        if not media_available():
            self._finish(info.call.sid, _("GStreamer WebRTC is not installed"), notify=True)
            return
        try:
            stun, turns = await self.turn.uris(info.session)
            kinds = ["audio", "video"] if info.call.video else ["audio"]
            info.media = CallMedia(stun, turns, kinds, bool(self.core.settings["calls.relay_only"]))
            self._wire_media(info)
            contents = await info.media.create_offer()
            info.contents = contents
            self._flush_candidates(info)
            await info.session.iq("set", info.peer, jingle.build_jingle(
                "session-initiate", info.call.sid, info.session.jid, contents))
        except Exception as e:
            log.info("could not start call %s: %s", info.call.sid, e)
            self._finish(info.call.sid, str(e), notify=True)

    async def _answer(self, info: _Call) -> None:
        if not media_available():
            self._finish(info.call.sid, _("GStreamer WebRTC is not installed"), notify=True)
            return
        try:
            stun, turns = await self.turn.uris(info.session)
            kinds = [c.description.media for c in info.offer if c.description is not None] or ["audio"]
            info.media = CallMedia(stun, turns, kinds, bool(self.core.settings["calls.relay_only"]))
            self._wire_media(info)
            info.contents = info.offer
            for name, candidate in info.remote_candidates:
                line = next((i for i, c in enumerate(info.contents) if c.name == name), 0)
                info.media.add_candidate(line, candidate)
            info.remote_candidates = []
            answer = await info.media.create_answer(info.offer)
            info.answer = answer
            info.contents = answer
            self._flush_candidates(info)
            await info.session.iq("set", info.peer, jingle.build_jingle(
                "session-accept", info.call.sid, "", answer, responder=info.session.jid))
            info.call.state = "active"
            info.call.started_at = int(time.time() * 1000)
        except Exception as e:
            log.info("could not answer call %s: %s", info.call.sid, e)
            self._finish(info.call.sid, str(e), notify=True)

    def _wire_media(self, info: _Call) -> None:
        media = info.media
        if media is None:
            return
        media.connect("candidate", lambda _m, line, cand: self._candidate(info, int(line), cand))
        media.connect("paintable", lambda _m, p: setattr(info.call, "paintable", p))
        media.connect("error", lambda _m, text: self._media_error(info, text))

    def _media_error(self, info: _Call, text: str) -> None:
        if info.call.state not in ("ended",):
            log.info("call media error: %s", text)
            info.call.error = text
            self._finish(info.call.sid, text, notify=True)

    def _candidate(self, info: _Call, line: int, candidate) -> None:
        if not info.contents:
            info.pending_candidates.append((line, candidate))
            return
        aio.spawn(self._send_candidate(info, line, candidate), name="jingle candidate")

    def _flush_candidates(self, info: _Call) -> None:
        pending, info.pending_candidates = info.pending_candidates, []
        for line, candidate in pending:
            self._candidate(info, line, candidate)

    async def _send_candidate(self, info: _Call, line: int, candidate) -> None:
        if info.call.state == "ended" or not info.contents:
            return
        name = info.contents[line].name if line < len(info.contents) else "audio"
        content = Content(name, creator="initiator", transport=jingle.Transport(candidates=[candidate]))
        try:
            await info.session.iq("set", info.peer, jingle.build_jingle(
                "transport-info", info.call.sid, "", [content]))
        except Exception as e:
            log.debug("could not send a call candidate: %s", e)

    async def _send_terminate(self, info: _Call, reason: str) -> None:
        try:
            await info.session.iq("set", info.peer, jingle.build_jingle(
                "session-terminate", info.call.sid, "", [], reason=reason))
        except Exception as e:
            log.debug("could not send session-terminate: %s", e)

    # ================================================================== incoming Jingle
    def _on_jingle_iq(self, session, iq) -> None:
        el = iq.xml.find(jingle.q(jingle.NS_JINGLE, "jingle"))
        if el is None:
            session.reply_iq(iq, error="bad-request")
            return
        try:
            j = jingle.parse_jingle(el)
        except Exception:
            session.reply_iq(iq, error="bad-request")
            return
        session.reply_iq(iq)
        aio.spawn(self._handle_jingle(session, j), name="jingle %s" % j.action)

    async def _handle_jingle(self, session, j) -> None:
        info = self._calls.get(j.sid)
        if j.action == "session-initiate":
            if info is None:
                video = any(c.description is not None and c.description.media == "video" for c in j.contents)
                call = CallItem(sid=j.sid, account_id=session.account.id, jid=util.bare(j.initiator),
                                name=self.core.names.contact_name(session.account.id, j.initiator),
                                direction=0, media="video" if video else "audio", state="incoming", video=video)
                info = _Call(call, session, j.initiator or session.jid, offer=j.contents)
                self._calls[j.sid] = info
                if not self._busy():
                    self.active = call
                    self.store.append(call)
                    info.timeout = GLib.timeout_add_seconds(RING_SECONDS, self._ring_timeout, j.sid)
                    self.emit("incoming", call)
                else:
                    await self._send_terminate(info, _("Busy"))
                    self._calls.pop(j.sid, None)
                    return
            else:
                info.offer = j.contents
                info.peer = j.initiator or info.peer
            if info.answered:
                await self._answer(info)
        elif info is None:
            return
        elif j.action == "session-accept":
            info.answer = j.contents
            info.contents = j.contents
            if info.media is not None:
                try:
                    await info.media.apply_answer(j.contents)
                    info.call.state = "active"
                    info.call.started_at = int(time.time() * 1000)
                except Exception as e:
                    self._finish(j.sid, str(e), notify=True)
        elif j.action == "transport-info":
            for content in j.contents:
                if content.transport is None:
                    continue
                try:
                    line = next(i for i, c in enumerate(info.contents) if c.name == content.name)
                except StopIteration:
                    line = 0
                for candidate in content.transport.candidates:
                    if info.media is None:
                        info.remote_candidates.append((content.name, candidate))
                    else:
                        info.media.add_candidate(line, candidate)
        elif j.action == "session-terminate":
            self._finish(j.sid, j.reason or _("Call ended"), notify=True)

    # ================================================================== helpers
    def _clear_timeout(self, info: _Call) -> None:
        if info.timeout:
            GLib.source_remove(info.timeout)
            info.timeout = 0

    def _finish(self, sid: str, reason: str, notify: bool = False) -> None:
        info = self._calls.pop(sid, None)
        if info is None:
            return
        self._clear_timeout(info)
        if info.media is not None:
            try:
                info.media.close()
            except Exception:
                pass
            info.media = None
        info.call.state = "ended"
        if reason:
            info.call.error = reason
        if self.active is info.call:
            self.active = None
        aio.spawn(self._record_history(info, reason), name="call history")
        for index in range(self.store.get_n_items()):
            if self.store.get_item(index) is info.call:
                self.store.remove(index)
                break
        if notify:
            self.emit("ended", info.call, reason)

    async def _record_history(self, info: _Call, reason: str) -> None:
        chats = getattr(self.core, "chats", None)
        db = getattr(self.core, "db", None)
        call = info.call
        if chats is None or db is None or not call.jid:
            return
        try:
            chat = await chats.get_or_create(call.account_id, call.jid, "chat", name=call.name)
            media = _("video") if call.media == "video" else _("audio")
            if call.started_at:
                seconds = max(0, int((time.time() * 1000 - call.started_at) / 1000))
                body = _("%(kind)s call · %(duration)s") % {"kind": media.capitalize(),
                                                            "duration": _format_duration(seconds)}
            elif call.direction == 0:
                if reason == _("Declined"):
                    body = _("Declined %(kind)s call") % {"kind": media}
                else:
                    body = _("Missed %(kind)s call") % {"kind": media}
            else:
                body = _("Cancelled %(kind)s call") % {"kind": media}
            account = self.core.accounts.get(call.account_id)
            sender = account.jid if account is not None and call.direction else call.jid
            values = {
                "chat_id": chat.id, "direction": call.direction,
                "timestamp": call.started_at or int(time.time() * 1000),
                "sender": sender, "sender_jid": sender, "body": body, "is_markdown": 0, "kind": "call",
                "status": STATUS_SENT if call.direction else 0, "extra": {"call": {
                    "media": call.media, "reason": reason, "duration": seconds if call.started_at else 0}},
            }
            msg_id = await db.run(lambda conn: insert_message_job(conn, values))
            message = await chats.reload_message(chat, msg_id)
            if message is not None:
                chats.show_message(chat, message)
        except Exception as e:
            log.info("could not record call history: %s", e)

    def toggle_mute(self, call: CallItem) -> None:
        info = self._calls.get(call.sid)
        if info is None or info.media is None:
            return
        call.muted = not call.muted
        info.media.set_muted(call.muted)

    def toggle_video(self, call: CallItem) -> None:
        info = self._calls.get(call.sid)
        if info is None or info.media is None:
            return
        call.video = not call.video
        info.media.set_video(call.video)
        if not call.video:
            call.screen = False

    def toggle_screen(self, call: CallItem) -> None:
        info = self._calls.get(call.sid)
        if info is None or info.media is None:
            return
        call.screen = not call.screen
        info.media.set_screen(call.screen)
