"""The GStreamer WebRTC media loopback: offer, answer, ICE and DTLS connect."""

from __future__ import annotations

import asyncio

import pytest

from ward.core import rtp

pytestmark = pytest.mark.skipif(not rtp.available(), reason="needs GStreamer webrtcbin, libnice-gstreamer and plugins")


def test_loopback_audio_offer_answer(run):
    async def go():
        first = rtp.CallMedia("", [], ["audio"], test_mode=True)
        second = rtp.CallMedia("", [], ["audio"], test_mode=True)
        first.connect("candidate", lambda _m, line, cand: second.add_candidate(line, cand))
        second.connect("candidate", lambda _m, line, cand: first.add_candidate(line, cand))
        try:
            offer = await first.create_offer()
            assert offer and offer[0].description.media == "audio"
            answer = await second.create_answer(offer)
            await first.apply_answer(answer)
            for _ in range(50):
                await asyncio.sleep(0.1)
                if (int(first.webrtc.get_property("connection-state")) == 2 and
                        int(second.webrtc.get_property("connection-state")) == 2):
                    break
            assert int(first.webrtc.get_property("connection-state")) == 2
            assert int(second.webrtc.get_property("connection-state")) == 2
        finally:
            first.close()
            second.close()

    run(go(), timeout=20)


def test_loopback_audio_and_video(run):
    async def go():
        first = rtp.CallMedia("", [], ["audio", "video"], test_mode=True)
        second = rtp.CallMedia("", [], ["audio", "video"], test_mode=True)
        first.connect("candidate", lambda _m, line, cand: second.add_candidate(line, cand))
        second.connect("candidate", lambda _m, line, cand: first.add_candidate(line, cand))
        try:
            offer = await first.create_offer()
            assert [c.description.media for c in offer] == ["audio", "video"]
            first.set_video(False)
            first.set_video(True)
            first.set_screen(True)
            first.set_screen(False)
            first.set_muted(True)
            first.set_muted(False)
            answer = await second.create_answer(offer)
            await first.apply_answer(answer)
            for _ in range(50):
                await asyncio.sleep(0.1)
                if int(first.webrtc.get_property("connection-state")) == 2:
                    break
            assert int(first.webrtc.get_property("connection-state")) == 2
        finally:
            first.close()
            second.close()

    run(go(), timeout=20)
