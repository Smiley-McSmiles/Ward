"""Jingle call signaling, with the media engine replaced by a recording fake."""

from __future__ import annotations

from xml.etree import ElementTree as ET

from gi.repository import GObject

from ward.core import calls, jingle
from ward.core.jingle import Content, Description, Payload, Transport
from ward.core.models import AccountItem
from ward.settings import Settings


class FakeSession:
    def __init__(self):
        self.account = AccountItem(id=1, jid="alice@example.org")
        self.jid = "alice@example.org/ward"
        self.bound_jid = "alice@example.org/ward"
        self.online = True
        self.messages: list[ET.Element] = []
        self.iqs: list[tuple[str, str, tuple]] = []
        self.features: set[str] = set()

    def add_feature(self, feature):
        self.features.add(feature)

    def connect(self, *_a):
        pass

    def new_message(self, to, type_="chat", msg_id=None):
        return ET.Element("{jabber:client}message", {"to": to, "type": type_})

    def send(self, el):
        self.messages.append(el)

    async def iq(self, type_, to, *payload, timeout=30):
        self.iqs.append((type_, to, payload))
        return ET.Element("{jabber:client}iq")

    def reply_iq(self, iq, payload=None, error=None):
        pass


class FakeMedia(GObject.Object):
    __gsignals__ = {"candidate": (GObject.SignalFlags.RUN_LAST, None, (int, object)),
                    "paintable": (GObject.SignalFlags.RUN_LAST, None, (object,)),
                    "error": (GObject.SignalFlags.RUN_LAST, None, (str,))}

    def __init__(self, *_a, **_kw):
        super().__init__()
        self.closed = False

    async def create_offer(self):
        return [content("initiator", "offer")]

    async def create_answer(self, _remote):
        return [content("initiator", "answer")]

    async def apply_answer(self, _remote):
        pass

    def add_candidate(self, _line, _candidate):
        pass

    def set_muted(self, _muted):
        pass

    def set_video(self, _enabled):
        pass

    def set_screen(self, _enabled):
        pass

    def close(self):
        self.closed = True


def content(creator: str, ufrag: str) -> Content:
    return Content("audio", creator=creator,
                   description=Description("audio", [Payload(111, "opus", 48000, 2)]),
                   transport=Transport(ufrag, "pwd", [("sha-256", "AA:BB", "actpass")]))


class FakeTurn:
    async def uris(self, _session):
        return ("stun://stun.example.org:3478", ["turn://user:pass@turn.example.org:3478?transport=udp"])


class FakeNames:
    def contact_name(self, _account_id, jid):
        return jid.split("@")[0].title()


class FakeCore:
    def __init__(self):
        self.settings = Settings()
        self.turn = FakeTurn()
        self.names = FakeNames()


def _jmi(el: ET.Element):
    return jingle.parse_jmi(el.find(jingle.q(jingle.NS_JMI, "jingle-message")))


def test_outgoing_call(monkeypatch, run):
    monkeypatch.setattr(calls, "media_available", lambda: True)
    monkeypatch.setattr(calls, "CallMedia", FakeMedia)

    async def scenario():
        service = calls.Calls(FakeCore())
        session = FakeSession()
        service.attach(session)
        call = await service.place_call(session.account, "bob@example.org", video=False)
        assert call.state == "ringing" and call.name == "Bob"
        sid, action, media = _jmi(session.messages[-1])
        assert (sid, action, media) == (call.sid, "propose", ("audio",))

        await service._on_jmi(session, "bob@example.org/phone", call.sid, "proceed", ("audio",))
        assert call.state == "connecting"
        _type, to, payload = session.iqs[-1]
        j = jingle.parse_jingle(payload[0])
        assert j.action == "session-initiate" and j.sid == call.sid
        assert to == "bob@example.org/phone", "the Jingle offer must go to the accepting resource"
        assert j.initiator == "alice@example.org/ward"

        accept = jingle.build_jingle("session-accept", call.sid, "", [content("responder", "answer")],
                                     responder="bob@example.org/phone")
        await service._handle_jingle(session, jingle.parse_jingle(accept))
        assert call.state == "active"

        await service.hangup(call)
        assert call.state == "ended" and service.active is None
        assert jingle.parse_jmi(session.messages[-1].find(jingle.q(jingle.NS_JMI, "jingle-message")))[1] == "finish"
        assert jingle.parse_jingle(session.iqs[-1][2][0]).action == "session-terminate"

    run(scenario(), timeout=20)


def test_incoming_call(monkeypatch, run):
    monkeypatch.setattr(calls, "media_available", lambda: True)
    monkeypatch.setattr(calls, "CallMedia", FakeMedia)

    async def scenario():
        service = calls.Calls(FakeCore())
        session = FakeSession()
        service.attach(session)
        await service._on_jmi(session, "bob@example.org/phone", "sid2", "propose", ("audio",))
        call = service.active
        assert call is not None and call.state == "incoming" and call.direction == 0

        await service.accept(call)
        assert call.state == "connecting"
        assert _jmi(session.messages[-1])[1] == "proceed"
        assert session.messages[-1].get("to") == "bob@example.org"

        offer = jingle.build_jingle("session-initiate", "sid2", "bob@example.org/phone",
                                    [content("initiator", "offer")])
        await service._handle_jingle(session, jingle.parse_jingle(offer))
        assert call.state == "active"
        assert jingle.parse_jingle(session.iqs[-1][2][0]).action == "session-accept"

        await service.hangup(call)
        assert call.state == "ended"

    run(scenario(), timeout=20)


def test_reject_incoming_call(monkeypatch, run):
    monkeypatch.setattr(calls, "media_available", lambda: True)
    monkeypatch.setattr(calls, "CallMedia", FakeMedia)

    async def scenario():
        service = calls.Calls(FakeCore())
        session = FakeSession()
        service.attach(session)
        await service._on_jmi(session, "bob@example.org/phone", "sid3", "propose", ("audio",))
        call = service.active
        await service.reject(call)
        assert call.state == "ended" and service.active is None
        assert _jmi(session.messages[-1])[1] == "reject"

    run(scenario(), timeout=20)


def test_cancel_before_answer_allows_the_next_call(monkeypatch, run):
    monkeypatch.setattr(calls, "media_available", lambda: True)
    monkeypatch.setattr(calls, "CallMedia", FakeMedia)

    async def scenario():
        service = calls.Calls(FakeCore())
        session = FakeSession()
        service.attach(session)
        first = await service.place_call(session.account, "bob@example.org")
        await service.hangup(first)
        assert first.state == "ended" and service.active is None
        second = await service.place_call(session.account, "bob@example.org")
        assert second.state == "ringing" and service.active is second
        await service.hangup(second)

    run(scenario(), timeout=20)


def test_failed_hangup_still_clears_the_call(monkeypatch, run):
    monkeypatch.setattr(calls, "media_available", lambda: True)
    monkeypatch.setattr(calls, "CallMedia", FakeMedia)

    class OfflineSession(FakeSession):
        fail_send = False

        def send(self, el):
            if self.fail_send:
                raise RuntimeError("offline")
            super().send(el)

    async def scenario():
        service = calls.Calls(FakeCore())
        session = OfflineSession()
        service.attach(session)
        call = await service.place_call(session.account, "bob@example.org")
        session.fail_send = True
        await service.hangup(call)
        assert call.state == "ended" and service.active is None
        session.fail_send = False
        second = await service.place_call(session.account, "bob@example.org")
        assert second.state == "ringing"
        await service.hangup(second)

    run(scenario(), timeout=20)


def test_an_ended_active_call_does_not_block_a_new_one(monkeypatch, run):
    monkeypatch.setattr(calls, "media_available", lambda: True)
    monkeypatch.setattr(calls, "CallMedia", FakeMedia)

    async def scenario():
        service = calls.Calls(FakeCore())
        session = FakeSession()
        service.attach(session)
        first = await service.place_call(session.account, "bob@example.org")
        # Simulate a stale UI/core hand-off: the call object says ended but the active reference is still there.
        first.state = "ended"
        second = await service.place_call(session.account, "bob@example.org")
        assert service.active is second and second.state == "ringing"
        await service.hangup(second)

    run(scenario(), timeout=20)


def test_failed_place_call_does_not_leave_an_active_call(monkeypatch, run):
    monkeypatch.setattr(calls, "media_available", lambda: True)
    monkeypatch.setattr(calls, "CallMedia", FakeMedia)

    class OfflineSession(FakeSession):
        def send(self, _el):
            raise RuntimeError("offline")

    async def scenario():
        service = calls.Calls(FakeCore())
        session = OfflineSession()
        service.attach(session)
        try:
            await service.place_call(session.account, "bob@example.org")
        except RuntimeError:
            pass
        else:
            raise AssertionError("the failed call should have raised")
        assert service.active is None and service.store.get_n_items() == 0

    run(scenario(), timeout=20)


def test_a_reconnect_keeps_the_call_session(monkeypatch, run):
    monkeypatch.setattr(calls, "media_available", lambda: True)
    monkeypatch.setattr(calls, "CallMedia", FakeMedia)

    async def scenario():
        service = calls.Calls(FakeCore())
        session = FakeSession()
        service.attach(session)
        service.account_offline(1)
        call = await service.place_call(session.account, "bob@example.org")
        assert call.state == "ringing"
        await service.hangup(call)

    run(scenario(), timeout=20)


def test_call_features_are_advertised(monkeypatch):
    monkeypatch.setattr(calls, "media_available", lambda: True)
    service = calls.Calls(FakeCore())
    session = FakeSession()
    service.attach(session)
    assert {"urn:xmpp:jingle-message:0", "urn:xmpp:jingle:1", "urn:xmpp:jingle:apps:rtp:1",
            "urn:xmpp:jingle:transports:ice-udp:1", "urn:xmpp:jingle:apps:dtls:0",
            "urn:xmpp:jingle:apps:rtp:audio", "urn:xmpp:jingle:apps:rtp:video"} <= session.features
