"""The media of a call: GStreamer's webrtcbin, with Opus audio and VP8 video, ICE through libnice and DTLS-SRTP.

webrtcbin speaks SDP; jingle.py translates.  Its signals arrive on GStreamer's threads and are handed to the main
loop.  Video comes out as a Gdk.Paintable: gtk4paintablesink's where it is installed (gst-plugins-rs), otherwise
frames copied into textures."""

from __future__ import annotations

import asyncio
import itertools
import logging
import os

from gi.repository import GLib, GObject

log = logging.getLogger(__name__)

_probe_names = itertools.count(1)

_gst: tuple | bool | None = None


def gst():
    """Gst, GstWebRTC and GstSdp, initialised once; None when missing."""
    global _gst
    if _gst is None:
        try:
            import gi

            gi.require_version("Gst", "1.0")
            gi.require_version("GstWebRTC", "1.0")
            gi.require_version("GstSdp", "1.0")
            from gi.repository import Gst, GstSdp, GstWebRTC

            Gst.init(None)
            _gst = (Gst, GstWebRTC, GstSdp)
        except (ImportError, ValueError):
            _gst = False
    return _gst if isinstance(_gst, tuple) else None


REQUIRED = ("webrtcbin", "nicesrc", "dtlssrtpenc", "srtpenc", "opusenc", "opusdec", "rtpopuspay", "rtpopusdepay",
            "vp8enc", "vp8dec", "rtpvp8pay", "rtpvp8depay", "audioconvert", "audioresample", "videoconvert",
            "videoscale", "decodebin")


def missing_elements() -> list[str]:
    g = gst()
    if g is None:
        return ["GStreamer (Gst, GstWebRTC, GstSdp)"]
    return [e for e in REQUIRED if g[0].ElementFactory.find(e) is None]


def available() -> bool:
    return not missing_elements()


def _have(name: str) -> bool:
    g = gst()
    return g is not None and g[0].ElementFactory.find(name) is not None


def payload_types(sdp_text: str) -> dict[str, int]:
    """The payload types an offer uses for Opus and VP8 ({'audio': 111, 'video': 96})."""
    out: dict[str, int] = {}
    media = ""
    for line in sdp_text.replace("\r\n", "\n").split("\n"):
        if line.startswith("m="):
            media = line[2:].split()[0]
        elif line.startswith("a=rtpmap:"):
            pt, _, enc = line[len("a=rtpmap:"):].partition(" ")
            name = enc.split("/")[0].lower()
            if media == "audio" and name == "opus" and "audio" not in out:
                out["audio"] = int(pt)
            elif media == "video" and name == "vp8" and "video" not in out:
                out["video"] = int(pt)
    return out


_paintable_cls = None


def _paintable_class():
    global _paintable_cls
    if _paintable_cls is None:
        from gi.repository import Gdk, Graphene

        class _Frames(GObject.Object, Gdk.Paintable):
            __gtype_name__ = "WardCallFrames"

            def __init__(self):
                super().__init__()
                self.texture = None

            def set_texture(self, tex) -> bool:
                size_changed = self.texture is None or (tex.get_width(), tex.get_height()) != (
                    self.texture.get_width(), self.texture.get_height())
                self.texture = tex
                if size_changed:
                    self.invalidate_size()
                self.invalidate_contents()
                return False

            def do_snapshot(self, snapshot, width, height):
                if self.texture is not None:
                    snapshot.append_texture(self.texture, Graphene.Rect().init(0, 0, width, height))

            def do_get_intrinsic_width(self):
                return self.texture.get_width() if self.texture is not None else 0

            def do_get_intrinsic_height(self):
                return self.texture.get_height() if self.texture is not None else 0

            def do_get_intrinsic_aspect_ratio(self):
                if self.texture is None or not self.texture.get_height():
                    return 0.0
                return self.texture.get_width() / self.texture.get_height()

        _paintable_cls = _Frames
    return _paintable_cls


class MediaSession(GObject.Object):
    """One call's media.  sources: 'auto' (microphone and camera) or 'test' (tones and test pictures)."""

    __gsignals__ = {
        "local-candidate": (GObject.SignalFlags.RUN_LAST, None, (int, str)),
        "state-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "remote-video": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "error": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    def __init__(self, audio: bool, video: bool, stun: str = "", turns: tuple[str, ...] | list[str] = (),
                 relay_only: bool = False, sources: str = ""):
        """stun and turns: webrtcbin URIs (core.turn makes them); relay_only: never connect directly."""
        super().__init__()
        Gst, _GstWebRTC, _GstSdp = gst()
        self.audio, self.video = audio, video
        self.sources = sources or os.environ.get("WARD_CALL_SOURCES", "auto")
        self.state = "new"
        self.received = {"audio": 0, "video": 0}       # buffers decoded, for tests and diagnostics
        self.local_paintable = None
        self.remote_paintable = None
        self.pipe = Gst.Pipeline.new("call")
        self.webrtc = Gst.ElementFactory.make("webrtcbin", "webrtc")
        self.webrtc.set_property("bundle-policy", 3)        # max-bundle
        if relay_only:
            self.webrtc.set_property("ice-transport-policy", 1)     # relay
        if stun:
            self.webrtc.set_property("stun-server", stun)
        self.pipe.add(self.webrtc)
        for url in turns:
            self.webrtc.emit("add-turn-server", url)
        self.webrtc.connect("on-ice-candidate", self._on_ice_candidate)
        self.webrtc.connect("pad-added", self._on_pad_added)
        self.webrtc.connect("notify::connection-state", self._on_connection_state)
        self.webrtc.connect("notify::ice-connection-state", self._on_ice_state)
        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        self._bus_handler = bus.connect("message", self._on_bus)
        self._mute = None
        self._camera = None
        self._remote_sink = None
        self._remote_video = None
        self._echo_probe = None     # webrtcechoprobe, waiting for their audio
        self._built = False

    # ------------------------------------------------------------------ building
    def _make(self, factory: str, **props):
        Gst = gst()[0]
        el = Gst.ElementFactory.make(factory)
        if el is None:
            raise RuntimeError("GStreamer element missing: %s" % factory)
        for k, v in props.items():
            el.set_property(k.rstrip("_").replace("_", "-"), v)
        self.pipe.add(el)
        return el

    def _chain(self, *els) -> None:
        for a, b in itertools.pairwise(els):
            if not a.link(b):
                raise RuntimeError("cannot link %s to %s" % (a.get_name(), b.get_name()))

    def build(self, pts: dict[str, int] | None = None) -> None:
        """The sending branches, with the payload types to use (an offer's, when answering)."""
        if self._built:
            return
        self._built = True
        Gst = gst()[0]
        pts = pts or {}
        # the other side's video, made here on the main thread (gtk4paintablesink must be) and linked when their
        # video arrives; a sink waiting unlinked is fine, it never holds the pipeline back (async=False)
        self._remote_sink, self._remote_video = self._video_sink()
        if self.audio:
            if self.sources == "test":
                src = self._make("audiotestsrc", is_live=True, wave=0, volume=0.2)
            else:
                src = self._make("autoaudiosrc")
            els = [src, self._make("audioconvert"), self._make("audioresample")]
            if _have("webrtcdsp") and _have("webrtcechoprobe"):
                # Echo cancellation and noise suppression.  webrtcdsp compares the microphone with what the
                # speakers play, which a webrtcechoprobe on the playback side records; it looks the probe up by
                # name when it starts, so the probe exists from now on (linked when their audio arrives), named
                # for this call alone.  Without one the dsp failed to start and the call never connected.
                probe = "ward-echo-probe-%d" % next(_probe_names)
                self._echo_probe = self._make("webrtcechoprobe", name=probe)
                els.append(self._make("webrtcdsp", probe=probe))
                els.append(self._make("audioconvert"))
            self._mute = self._make("volume")
            pt = pts.get("audio", 111)
            caps = Gst.Caps.from_string("application/x-rtp,media=audio,encoding-name=OPUS,clock-rate=48000,"
                                        "encoding-params=(string)2,payload=%d" % pt)
            els += [self._mute, self._make("opusenc", inband_fec=True), self._make("rtpopuspay", pt=pt),
                    self._make("capsfilter", caps=caps)]
            self._chain(*els)
            self._link_to_webrtc(els[-1])
        if self.video:
            if self.sources == "test":
                src = self._make("videotestsrc", is_live=True, pattern=0)      # colour bars
            else:
                src = self._make("autovideosrc") if _have("autovideosrc") else self._make("v4l2src")
            raw = Gst.Caps.from_string("video/x-raw,width=640,height=480,framerate=30/1")
            tee = self._make("tee")
            self._chain(src, self._make("videoconvert"), self._make("videoscale", add_borders=True),
                        self._make("videorate"), self._make("capsfilter", caps=raw), tee)
            self._camera = self._make("valve", drop=False)
            pt = pts.get("video", 96)
            caps = Gst.Caps.from_string("application/x-rtp,media=video,encoding-name=VP8,clock-rate=90000,"
                                        "payload=%d" % pt)
            send = [self._make("queue", max_size_buffers=2, leaky=2), self._camera, self._make("videoconvert"),
                    self._make("vp8enc", deadline=1, cpu_used=8, target_bitrate=800000, keyframe_max_dist=60,
                               error_resilient=1),
                    self._make("rtpvp8pay", pt=pt), self._make("capsfilter", caps=caps)]
            self._chain(*send)
            tee.link(send[0])
            self._link_to_webrtc(send[-1])
            preview = [self._make("queue", max_size_buffers=2, leaky=2), self._make("videoconvert")]
            sink, self.local_paintable = self._video_sink()
            self._chain(*preview, sink)
            tee.link(preview[0])

    def _link_to_webrtc(self, el) -> None:
        pad = el.get_static_pad("src")
        sink = self.webrtc.request_pad_simple("sink_%u")
        if pad.link(sink) != gst()[0].PadLinkReturn.OK:
            raise RuntimeError("cannot link to webrtcbin")

    def _video_sink(self):
        """(sink element, Gdk.Paintable) for a video branch; the paintable is None without a display."""
        # sinks never take part in state changes (async=False): one joining a running call would pause it all
        if self.sources == "test" and os.environ.get("WARD_CALL_FAKESINK"):
            return self._make("fakesink", sync=False, async_=False), None
        if _have("gtk4paintablesink"):
            sink = self._make("gtk4paintablesink", async_=False)
            return sink, sink.get_property("paintable")
        Gst = gst()[0]
        conv = self._make("videoconvert")
        sink = self._make("appsink", emit_signals=True, max_buffers=1, drop=True, sync=False, async_=False,
                          caps=Gst.Caps.from_string("video/x-raw,format=RGBA"))
        try:
            paintable = _paintable_class()()
        except Exception:
            paintable = None
        sink.connect("new-sample", self._on_sample, paintable)
        # (convert to RGBA first: the caller links into conv)
        conv.link(sink)
        return conv, paintable

    def _on_sample(self, sink, paintable):
        Gst = gst()[0]
        sample = sink.emit("pull-sample")
        if sample is None or paintable is None:
            return Gst.FlowReturn.OK
        caps = sample.get_caps().get_structure(0)
        w, h = caps.get_value("width"), caps.get_value("height")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = GLib.Bytes.new(bytes(info.data))
        finally:
            buf.unmap(info)

        def show():
            from gi.repository import Gdk

            tex = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8A8, data, w * 4)
            paintable.set_texture(tex)
            return False
        GLib.idle_add(show)
        return Gst.FlowReturn.OK

    # ------------------------------------------------------------------ receiving
    def _on_pad_added(self, _webrtc, pad) -> None:
        Gst = gst()[0]
        if pad.get_direction() != Gst.PadDirection.SRC:
            return
        dec = Gst.ElementFactory.make("decodebin")
        dec.connect("pad-added", self._on_decoded)
        self.pipe.add(dec)
        dec.sync_state_with_parent()
        pad.link(dec.get_static_pad("sink"))

    def _on_decoded(self, _dec, pad) -> None:
        Gst = gst()[0]
        caps = pad.get_current_caps() or pad.query_caps(None)
        name = caps.get_structure(0).get_name() if caps and caps.get_size() else ""
        if name.startswith("audio"):
            kind = "audio"
            q = Gst.ElementFactory.make("queue")
            conv = Gst.ElementFactory.make("audioconvert")
            res = Gst.ElementFactory.make("audioresample")
            sink = Gst.ElementFactory.make("fakesink" if self.sources == "test" else "autoaudiosink")
            # as for video: joining the running call must not pause it (autoaudiosink is a bin)
            for prop, value in (("async-handling", True), ("async", False)):
                if sink.find_property(prop):
                    sink.set_property(prop, value)
            if self.sources == "test":
                sink.set_property("sync", False)
            # the first stream we play passes the echo probe (made with the dsp, already in the pipeline)
            probe, self._echo_probe = self._echo_probe, None
            if probe is not None:
                for el in (q, conv, res, sink):
                    self.pipe.add(el)
                self._chain(q, conv, res, probe, sink)
                for el in (q, conv, res, probe, sink):
                    el.sync_state_with_parent()
                pad.link(q.get_static_pad("sink"))
                self._count(pad, kind)
                return
            els = [q, conv, res, sink]
        elif name.startswith("video"):
            kind = "video"
            sink, self._remote_sink = self._remote_sink, None
            if sink is None:
                return          # a second video stream: we show one
            q = Gst.ElementFactory.make("queue")
            conv = Gst.ElementFactory.make("videoconvert")
            self.pipe.add(q)
            self.pipe.add(conv)
            q.link(conv)
            conv.link(sink)
            for el in (q, conv):
                el.sync_state_with_parent()
            pad.link(q.get_static_pad("sink"))
            self._count(pad, kind)
            paintable = self._remote_video

            def shown() -> bool:
                self.remote_paintable = paintable
                if paintable is not None:
                    self.emit("remote-video", paintable)
                return False
            GLib.idle_add(shown)
            return
        else:
            return
        for el in els:
            self.pipe.add(el)
        self._chain(*els)
        for el in els:
            el.sync_state_with_parent()
        pad.link(q.get_static_pad("sink"))
        self._count(pad, kind)

    def _count(self, pad, kind: str) -> None:
        Gst = gst()[0]

        def probe(_pad, _info):
            self.received[kind] += 1
            return Gst.PadProbeReturn.OK
        pad.add_probe(Gst.PadProbeType.BUFFER, probe)

    # ------------------------------------------------------------------ negotiation
    def start(self) -> None:
        self.pipe.set_state(gst()[0].State.PLAYING)

    async def _promise(self, action: str, *args):
        Gst = gst()[0]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        def done(promise, _data):
            reply = promise.get_reply()

            def settle() -> None:
                if not fut.done():
                    fut.set_result(reply)
            loop.call_soon_threadsafe(settle)
        self.webrtc.emit(action, *args, Gst.Promise.new_with_change_func(done, None))
        return await asyncio.wait_for(fut, 20)

    def _description(self, kind: str, text: str):
        _Gst, GstWebRTC, GstSdp = gst()
        ok, msg = GstSdp.SDPMessage.new_from_text(text)
        if ok != GstSdp.SDPResult.OK:
            raise ValueError("invalid SDP")
        t = GstWebRTC.WebRTCSDPType.OFFER if kind == "offer" else GstWebRTC.WebRTCSDPType.ANSWER
        return GstWebRTC.WebRTCSessionDescription.new(t, msg)

    async def create_offer(self) -> str:
        self.build()
        self.start()
        reply = await self._promise("create-offer", None)
        offer = reply.get_value("offer") if reply is not None else None
        if offer is None:
            raise RuntimeError("webrtcbin could not create an offer")
        await self._promise("set-local-description", offer)
        return offer.sdp.as_text()

    async def accept_offer(self, sdp_text: str) -> str:
        """Answer a remote offer: returns our answer's SDP."""
        self.build(payload_types(sdp_text))
        self.start()
        await self._promise("set-remote-description", self._description("offer", sdp_text))
        reply = await self._promise("create-answer", None)
        answer = reply.get_value("answer") if reply is not None else None
        if answer is None:
            raise RuntimeError("webrtcbin could not create an answer")
        await self._promise("set-local-description", answer)
        return answer.sdp.as_text()

    async def set_answer(self, sdp_text: str) -> None:
        await self._promise("set-remote-description", self._description("answer", sdp_text))

    def add_remote_candidate(self, mline: int, candidate: str) -> None:
        self.webrtc.emit("add-ice-candidate", mline, candidate)

    def _on_ice_candidate(self, _webrtc, mline: int, candidate: str) -> None:
        GLib.idle_add(lambda: (self.emit("local-candidate", mline, candidate), False)[1])

    def _on_connection_state(self, *_a) -> None:
        state = self.webrtc.get_property("connection-state").value_nick
        GLib.idle_add(self._set_state, state)

    def _on_ice_state(self, *_a) -> None:
        state = self.webrtc.get_property("ice-connection-state").value_nick
        if state == "failed":
            GLib.idle_add(self._set_state, "failed")

    def _set_state(self, state: str) -> bool:
        if state != self.state and self.state != "closed":
            self.state = state
            self.emit("state-changed", state)
        return False

    def _on_bus(self, _bus, msg) -> None:
        Gst = gst()[0]
        if msg.type == Gst.MessageType.LATENCY:
            # elements joined (or changed) while running: the pipeline must be told, or its sinks wait forever
            self.pipe.recalculate_latency()
        elif msg.type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            log.warning("call media error: %s (%s)", err.message, debug)
            self.emit("error", err.message)

    # ------------------------------------------------------------------ controls
    def set_muted(self, muted: bool) -> None:
        if self._mute is not None:
            self._mute.set_property("mute", muted)

    def set_camera(self, on: bool) -> None:
        if self._camera is not None:
            self._camera.set_property("drop", not on)

    def close(self) -> None:
        if self.state == "closed":
            return
        self.state = "closed"
        Gst = gst()[0]
        bus = self.pipe.get_bus()
        bus.disconnect(self._bus_handler)
        bus.remove_signal_watch()
        self.pipe.set_state(Gst.State.NULL)
