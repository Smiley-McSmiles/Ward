"""GStreamer WebRTC media for Jingle calls.

webrtcbin does ICE, DTLS-SRTP and RTP; this module builds its pipeline, translates offers and answers through
ward.core.sdp, and exposes the remote video as a Gdk.Paintable for the call window.  Audio uses PulseAudio/PipeWire
through pulsesrc; video uses the first V4L2 camera and the X11 screen, with test sources when there is no device.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from gi.repository import GObject

from . import sdp
from .jingle import Candidate, Content

log = logging.getLogger("ward.calls")

GST: Any = None
WEBRTC: Any = None
SDP: Any = None


def _load():
    global GST, WEBRTC, SDP
    if GST is not None:
        return GST, WEBRTC
    try:
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstWebRTC", "1.0")
        gi.require_version("GstSdp", "1.0")
        from gi.repository import Gst, GstSdp, GstWebRTC

        Gst.init(None)
    except (ImportError, ValueError, AttributeError) as e:
        log.info("GStreamer WebRTC is unavailable: %s", e)
        return None, None
    GST, WEBRTC, SDP = Gst, GstWebRTC, GstSdp
    return GST, WEBRTC


def screen_available() -> bool:
    """Screen capture is X11-only until the PipeWire portal source is added."""
    gst, _webrtc = _load()
    return bool(gst is not None and not os.environ.get("WAYLAND_DISPLAY") and
                gst.ElementFactory.find("ximagesrc"))


def available() -> bool:
    gst, _webrtc = _load()
    if gst is None:
        return False
    return bool(gst.ElementFactory.find("webrtcbin") and gst.ElementFactory.find("nicesrc"))


def _factory(name: str):
    return GST.ElementFactory.find(name) if GST is not None else None


def _make(name: str):
    element = GST.ElementFactory.make(name, None)
    if element is None:
        raise RuntimeError("GStreamer element %s is missing" % name)
    return element


class CallMedia(GObject.Object):
    """One WebRTC connection; create it, exchange offers/answers and candidates, then close it."""

    __gsignals__ = {
        "candidate": (GObject.SignalFlags.RUN_LAST, None, (int, object)),
        "paintable": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "error": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    def __init__(self, stun: str, turns: list[str], kinds: list[str], relay_only: bool = False,
                 test_mode: bool = False):
        super().__init__()
        gst, webrtc = _load()
        if gst is None or webrtc is None:
            raise RuntimeError("GStreamer webrtcbin is not available")
        self.test_mode = test_mode
        self.kinds = [k for k in kinds if k in ("audio", "video")] or ["audio"]
        self.pipeline = gst.Pipeline.new("ward-call")
        self.webrtc = _make("webrtcbin")
        self.webrtc.set_property("bundle-policy", "max-bundle")
        if stun:
            self.webrtc.set_property("stun-server", stun)
        for uri in turns:
            self.webrtc.emit("add-turn-server", uri)
        if relay_only:
            self.webrtc.set_property("ice-transport-policy", webrtc.WebRTCICETransportPolicy.RELAY)
        self.pipeline.add(self.webrtc)
        self._pads: list[Any] = []
        self._receive: list[Any] = []
        self._video_sink = None
        self._selector = None
        self._volume = None
        self._camera_pad = None
        self._screen_pad = None
        self._blank_pad = None
        self._video_enabled = True
        self._screen_enabled = False
        self._closed = False
        self._remote_ready = False
        self._pending_candidates: list[tuple[int, Candidate]] = []
        self.webrtc.connect("pad-added", self._on_pad_added)
        self.webrtc.connect("on-ice-candidate", self._on_ice)
        for kind in self.kinds:
            if kind == "audio":
                self._add_audio()
            else:
                self._add_video()
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)
        self.pipeline.set_state(gst.State.PLAYING)
        if self._selector is not None:
            self._selector.set_property("active-pad", self._camera_pad)

    # ------------------------------------------------------------------ pipeline
    def _sink_pad(self):
        pad = self.webrtc.request_pad(self.webrtc.get_pad_template("sink_%u"), None, None)
        if pad is None:
            raise RuntimeError("webrtcbin could not create a sink pad (is libnice-gstreamer installed?)")
        self._pads.append(pad)
        return pad

    def _add_audio(self) -> None:
        source = None
        if not self.test_mode:
            for name in ("pulsesrc", "pipewiresrc"):
                if _factory(name):
                    source = _make(name)
                    break
        if source is None:
            source = _make("audiotestsrc")
            source.set_property("is-live", True)
            source.set_property("wave", 0)       # silence by default in test mode
        volume = _make("volume")
        self._volume = volume
        enc = _make("opusenc")
        enc.set_property("bitrate", 64000)
        pay = _make("rtpopuspay")
        queue = _make("queue")
        caps = _make("capsfilter")
        caps.set_property("caps", GST.Caps.from_string(
            "application/x-rtp,media=audio,encoding-name=OPUS,payload=111,clock-rate=48000"))
        for element in (source, volume, enc, pay, queue, caps):
            self.pipeline.add(element)
        source.link(volume)
        volume.link(enc)
        enc.link(pay)
        pay.link(queue)
        queue.link(caps)
        caps.get_static_pad("src").link(self._sink_pad())
        self._audio_source = source

    def _add_video(self) -> None:
        selector = _make("input-selector")
        self._selector = selector
        enc = _make("vp8enc")
        enc.set_property("deadline", 1)
        pay = _make("rtpvp8pay")
        queue = _make("queue")
        caps = _make("capsfilter")
        caps.set_property("caps", GST.Caps.from_string(
            "application/x-rtp,media=video,encoding-name=VP8,payload=96,clock-rate=90000"))
        for element in (selector, enc, pay, queue, caps):
            self.pipeline.add(element)
        selector.link(enc)
        enc.link(pay)
        pay.link(queue)
        queue.link(caps)
        caps.get_static_pad("src").link(self._sink_pad())
        self._camera_pad = self._add_video_source("camera")
        self._video_enabled = True
        self._screen_enabled = False
        if self._camera_pad is not None:
            selector.set_property("active-pad", self._camera_pad)

    def _add_video_source(self, kind: str):
        if kind == "camera":
            source = None
            if not self.test_mode and _factory("v4l2src"):
                source = _make("v4l2src")
            if source is None:
                source = _make("videotestsrc")
                source.set_property("is-live", True)
                source.set_property("pattern", "ball")
        elif kind == "screen":
            source = None
            if not self.test_mode and not os.environ.get("WAYLAND_DISPLAY") and _factory("ximagesrc"):
                try:
                    source = _make("ximagesrc")
                    source.set_property("use-damage", False)
                except Exception:
                    source = None
            if source is None:
                source = _make("videotestsrc")
                source.set_property("is-live", True)
                source.set_property("pattern", "smpte")
        else:
            source = _make("videotestsrc")
            source.set_property("is-live", True)
            source.set_property("pattern", "black")
        convert = _make("videoconvert")
        scale = _make("videoscale")
        caps = _make("capsfilter")
        caps.set_property("caps", GST.Caps.from_string("video/x-raw,width=640,height=480,framerate=30/1"))
        for element in (source, convert, scale, caps):
            self.pipeline.add(element)
        source.link(convert)
        convert.link(scale)
        scale.link(caps)
        if not caps.link(self._selector):
            raise RuntimeError("could not connect the %s video source" % kind)
        for element in (source, convert, scale, caps):
            element.sync_state_with_parent()
        pad = caps.get_static_pad("src").get_peer()
        if kind == "screen":
            self._screen_pad = pad
        elif kind == "blank":
            self._blank_pad = pad
        return pad

    def _on_bus_error(self, _bus, message) -> None:
        err, debug = message.parse_error()
        log.warning("call media error: %s (%s)", err, debug)
        self.emit("error", str(err))

    # ------------------------------------------------------------------ receiving
    def _on_pad_added(self, _webrtc, pad) -> None:
        caps = pad.get_current_caps() or pad.query_caps(None)
        name = caps.get_structure(0).get_name() if caps and caps.get_size() else ""
        if name != "application/x-rtp":
            return
        media = caps.get_structure(0).get_string("media") or ""
        encoding = (caps.get_structure(0).get_string("encoding-name") or "").upper()
        elements = []
        try:
            if media == "audio" and encoding in ("OPUS", "MULTIOPUS"):
                depay, dec = _make("rtpopusdepay"), _make("opusdec")
                sink = _make("fakesink") if self.test_mode else _make("autoaudiosink")
                chain = [depay, dec, _make("audioconvert"), _make("audioresample"), sink]
            elif media == "video" and encoding == "VP8":
                depay, dec = _make("rtpvp8depay"), _make("vp8dec")
                vc = _make("videoconvert")
                if self.test_mode:
                    sink = _make("fakesink")
                elif _factory("gtk4paintablesink"):
                    sink = _make("gtk4paintablesink")
                    self._video_sink = sink
                else:
                    sink = _make("autovideosink")
                chain = [depay, dec, vc, sink]
            elif media == "video" and encoding in ("H264", "VP8"):
                depay = _make("rtph264depay")
                dec = _make("avdec_h264") if _factory("avdec_h264") else _make("openh264dec")
                vc = _make("videoconvert")
                if self.test_mode:
                    sink = _make("fakesink")
                else:
                    sink = _make("gtk4paintablesink") if _factory("gtk4paintablesink") else _make("autovideosink")
                    if _factory("gtk4paintablesink"):
                        self._video_sink = sink
                chain = [depay, dec, vc, sink]
            else:
                return
            for element in chain:
                self.pipeline.add(element)
                elements.append(element)
            import itertools

            for a, b in itertools.pairwise(chain):
                a.link(b)
            pad.link(chain[0].get_static_pad("sink"))
            for element in chain:
                element.sync_state_with_parent()
            self._receive.extend(elements)
            if self._video_sink is not None:
                GLib_idle(self._emit_paintable)
        except Exception as e:
            log.warning("could not build the receive pipeline: %s", e)
            self.emit("error", str(e))

    def _emit_paintable(self) -> bool:
        if self._video_sink is not None:
            paintable = self._video_sink.get_property("paintable")
            if paintable is not None:
                self.emit("paintable", paintable)
        return False

    # ------------------------------------------------------------------ SDP
    def _promise(self, timeout: float = 8):
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def changed(promise, _user):
            reply = promise.get_reply()
            if not future.done():
                loop.call_soon_threadsafe(lambda: future.set_result(reply))

        return GST.Promise.new_with_change_func(changed, None), future

    async def _wait(self, future, timeout: float = 8):
        return await asyncio.wait_for(future, timeout)

    async def create_offer(self) -> list[Content]:
        promise, future = self._promise()
        self.webrtc.emit("create-offer", None, promise)
        reply = await self._wait(future)
        offer = reply.get_value("offer")
        desc = WEBRTC.WebRTCSessionDescription.new(WEBRTC.WebRTCSDPType.OFFER, offer.sdp)
        local, local_future = self._promise()
        self.webrtc.emit("set-local-description", desc, local)
        await self._wait(local_future)
        return sdp.contents_from_sdp(offer.sdp.as_text())

    async def create_answer(self, remote: list[Content]) -> list[Content]:
        offer_sdp = sdp.sdp_from_contents(remote, role="responder")
        message = SDP.SDPMessage.new_from_text(offer_sdp)[1]
        desc = WEBRTC.WebRTCSessionDescription.new(WEBRTC.WebRTCSDPType.OFFER, message)
        promise, future = self._promise()
        self.webrtc.emit("set-remote-description", desc, promise)
        await self._wait(future)
        self._flush_candidates()
        answer_promise, answer_future = self._promise()
        self.webrtc.emit("create-answer", None, answer_promise)
        reply = await self._wait(answer_future)
        answer = reply.get_value("answer")
        local, local_future = self._promise()
        self.webrtc.emit("set-local-description",
                         WEBRTC.WebRTCSessionDescription.new(WEBRTC.WebRTCSDPType.ANSWER, answer.sdp), local)
        await self._wait(local_future)
        return sdp.contents_from_sdp(answer.sdp.as_text())

    async def apply_answer(self, remote: list[Content]) -> None:
        answer_sdp = sdp.sdp_from_contents(remote, role="initiator")
        message = SDP.SDPMessage.new_from_text(answer_sdp)[1]
        desc = WEBRTC.WebRTCSessionDescription.new(WEBRTC.WebRTCSDPType.ANSWER, message)
        promise, future = self._promise()
        self.webrtc.emit("set-remote-description", desc, promise)
        await self._wait(future)
        self._flush_candidates()

    # ------------------------------------------------------------------ ICE
    def _on_ice(self, _webrtc, mline: int, candidate: str) -> None:
        parsed = _candidate_from_gst(candidate)
        if parsed is not None:
            self.emit("candidate", int(mline), parsed)

    def _flush_candidates(self) -> None:
        self._remote_ready = True
        pending, self._pending_candidates = self._pending_candidates, []
        for mline, candidate in pending:
            self.add_candidate(mline, candidate)

    def add_candidate(self, mline: int, candidate: Candidate) -> None:
        if not self._remote_ready:
            self._pending_candidates.append((int(mline), candidate))
            return
        line = "%s %d %s %d %s %d typ %s" % (
            candidate.foundation or "1", candidate.component, candidate.protocol or "udp",
            candidate.priority, candidate.ip, candidate.port, candidate.type or "host")
        if candidate.rel_addr:
            line += " raddr %s" % candidate.rel_addr
        if candidate.rel_port:
            line += " rport %d" % candidate.rel_port
        if candidate.tcp_type:
            line += " tcptype %s" % candidate.tcp_type
        self.webrtc.emit("add-ice-candidate", int(mline), "candidate:" + line)

    # ------------------------------------------------------------------ controls
    def set_muted(self, muted: bool) -> None:
        if self._volume is not None:
            self._volume.set_property("mute", bool(muted))

    def set_video(self, enabled: bool) -> None:
        self._video_enabled = enabled
        if not enabled and self._blank_pad is None:
            self._blank_pad = self._add_video_source("blank")
        self._use_source()

    def set_screen(self, enabled: bool) -> None:
        self._screen_enabled = enabled
        if enabled and self._screen_pad is None:
            self._screen_pad = self._add_video_source("screen")
        self._use_source()

    def _use_source(self) -> None:
        if self._selector is None:
            return
        if self._screen_enabled:
            pad = self._screen_pad
        elif self._video_enabled:
            pad = self._camera_pad
        else:
            pad = self._blank_pad
        if pad is not None:
            self._selector.set_property("active-pad", pad)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.pipeline.set_state(GST.State.NULL)
        except Exception:
            pass
        self.pipeline = None
        self.webrtc = None


def _candidate_from_gst(value: str) -> Candidate | None:
    value = value[10:] if value.startswith("candidate:") else value
    parts = value.split()
    if len(parts) < 8:
        return None
    try:
        cand = Candidate(parts[0], int(parts[1]), parts[2].lower(), int(parts[3]), parts[4], int(parts[5]),
                         parts[7])
    except (ValueError, IndexError):
        return None
    rest = parts[8:]
    for i in range(0, len(rest) - 1, 2):
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
    return cand


def GLib_idle(callback):
    from gi.repository import GLib

    return GLib.idle_add(callback)
