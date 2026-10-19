"""Calls end to end on a real server: Jingle Message Initiation, Jingle with ICE and DTLS-SRTP, media both ways
(test tones and pictures), answered on one of two devices, declined, cancelled, busy, and the line in the chat."""

import asyncio

import pytest
from helpers import account, befriend, fresh, make_core, stop, wait_for

from ward.core.calls import media
from ward.core.calls.service import BUSY, CANCELLED, DECLINED, ELSEWHERE, ENDED, MISSED

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not media.available(), reason="GStreamer WebRTC is missing: %s"
                                 % ", ".join(media.missing_elements()))]


@pytest.fixture(autouse=True)
def test_sources(monkeypatch):
    monkeypatch.setenv("WARD_CALL_SOURCES", "test")
    monkeypatch.setenv("WARD_CALL_FAKESINK", "1")


async def _pair(tmp_path, prosody, second_bob: bool = False):
    alice = await make_core(tmp_path, prosody, fresh(prosody, "calla"))
    bob_user = fresh(prosody, "callb")
    bob = await make_core(tmp_path, prosody, bob_user, name="bob1", resource="laptop")
    cores = [alice, bob]
    for c in cores:
        c.settings.set("encryption.default", "off")
    await befriend(alice, bob)
    if second_bob:
        bob2 = await make_core(tmp_path, prosody, bob_user, name="bob2", resource="phone")
        bob2.settings.set("encryption.default", "off")
        cores.append(bob2)
    contact = alice.roster.contacts.get(account(alice).id, account(bob).jid)
    await wait_for(lambda: len(contact.resources) == (2 if second_bob else 1), 15, "bob's devices")
    return cores


def _incoming(core):
    seen = []
    core.calls.connect("incoming", lambda _s, call: seen.append(call))
    return seen


async def _line(core, peer_jid: str):
    acc = account(core)
    chat = core.chats.find(acc.id, peer_jid, "chat")
    ml = await core.chats.open_messages(chat)
    return next(m for m in reversed(ml.items()) if m.kind == "call")


def test_call_answered_on_one_device(trust_test_ca, tmp_path, run):
    prosody = trust_test_ca

    async def main():
        alice, bob, bob2 = await _pair(tmp_path, prosody, second_bob=True)
        try:
            a, b = account(alice), account(bob)
            assert await alice.calls.can_call(a.id, b.jid) == {"audio", "video"}
            ringing1, ringing2 = _incoming(bob), _incoming(bob2)
            call = await alice.calls.start(a, b.jid, video=True)
            assert call.state == "proposing"
            await wait_for(lambda: ringing1 and ringing2, 15, "both of bob's devices ringing")
            await wait_for(lambda: call.state == "ringing", 10, "ringing at alice")
            incoming, other = ringing1[0], ringing2[0]
            assert incoming.media == {"audio", "video"} and not incoming.outgoing

            await bob.calls.accept(incoming, video=True)
            await wait_for(lambda: other.state == "ended", 15, "the other device to stop ringing")
            assert other.end == ELSEWHERE
            await wait_for(lambda: call.state == "active" and incoming.state == "active", 30, "media connected")
            try:
                await wait_for(lambda: all(ms.received["audio"] > 20 and ms.received["video"] > 10
                                           for ms in (call.ms, incoming.ms)), 30, "audio and video both ways")
            except AssertionError:
                raise AssertionError("received: alice %s, bob %s" % (call.ms.received, incoming.ms.received)) \
                    from None

            alice.calls.set_muted(call, True)
            assert call.muted
            await asyncio.sleep(1.2)
            await alice.calls.hang_up(call)
            assert call.state == "ended" and call.end == ENDED
            await wait_for(lambda: incoming.state == "ended", 15, "the call to end at bob")
            assert incoming.end == ENDED
            line = await _line(alice, b.jid)
            c = line.extra["call"]
            assert c["end"] == ENDED and c["outgoing"] and c["duration"] >= 1 and c["media"] == ["audio", "video"]
            assert alice.calls.summary(line.extra).startswith("Video call, 0:0")
            bline = await _line(bob, a.jid)
            assert bline.extra["call"]["end"] == ENDED and not bline.extra["call"]["outgoing"]
        finally:
            await stop(alice, bob, bob2)

    run(main(), timeout=180)


def test_declined_cancelled_and_busy(trust_test_ca, tmp_path, run):
    prosody = trust_test_ca

    async def main():
        alice, bob = await _pair(tmp_path, prosody)
        try:
            a, b = account(alice), account(bob)
            ringing = _incoming(bob)

            # declined
            call = await alice.calls.start(a, b.jid, video=False)
            await wait_for(lambda: ringing, 15, "ringing")
            await bob.calls.decline(ringing[-1])
            await wait_for(lambda: call.state == "ended", 15, "decline at alice")
            assert call.end == DECLINED and ringing[-1].end == DECLINED

            # cancelled before an answer: a missed call at bob
            call = await alice.calls.start(a, b.jid, video=False)
            await wait_for(lambda: len(ringing) == 2, 15, "ringing again")
            await alice.calls.hang_up(call)
            assert call.end == CANCELLED
            await wait_for(lambda: ringing[-1].state == "ended", 15, "retract at bob")
            assert ringing[-1].end == MISSED
            assert bob.calls.summary((await _line(bob, a.jid)).extra) == "Missed call"

            # busy: bob is in another call
            carol = await make_core(tmp_path, prosody, fresh(prosody, "callc"))
            try:
                carol.settings.set("encryption.default", "off")
                await befriend(carol, bob)
                call1 = await carol.calls.start(account(carol), b.jid, video=False)
                await wait_for(lambda: len(ringing) == 3, 15, "carol ringing")
                await bob.calls.accept(ringing[-1], video=False)
                await wait_for(lambda: call1.state == "active", 30, "carol's call connected")
                call2 = await alice.calls.start(a, b.jid, video=False)
                await wait_for(lambda: call2.state == "ended", 15, "busy at alice")
                assert call2.end == BUSY
                await carol.calls.hang_up(call1)
            finally:
                await stop(carol)
        finally:
            await stop(alice, bob)

    run(main(), timeout=180)
