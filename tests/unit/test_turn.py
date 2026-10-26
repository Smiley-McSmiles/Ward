"""coturn/eturnal support: server lists, XEP-0215, REST credentials and a real TURN allocation probe."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import socket
import struct
from xml.etree import ElementTree as ET

from ward.core import stun, turn


class FakeSettings(dict):
    def __getitem__(self, key):
        return self.get(key, "")


def test_manual_server_lists() -> None:
    services = turn.parse_manual(
        "turn:turn.example.org:3478?transport=tcp turns:secure.example.org:5349 "
        "stun:stun.example.org plain.example.org:3479", "turn")
    assert [(s.type, s.host, s.port, s.transport) for s in services] == [
        ("turn", "turn.example.org", 3478, "tcp"),
        ("turn", "secure.example.org", 5349, "tls"),
        ("stun", "stun.example.org", 3478, "udp"),
        ("turn", "plain.example.org", 3479, "udp"),
    ]


def test_xep_0215_services() -> None:
    el = ET.fromstring(
        "<services xmlns='urn:xmpp:extdisco:2'>"
        "<service type='turn' host='turn.example.org' port='3478' transport='udp' username='u' password='p'/>"
        "<service type='stun' host='stun.example.org' port='3478'/>"
        "</services>")
    services = turn.parse_services(el, now=1000)
    assert services[0].type == "turn" and services[0].username == "u"
    assert services[0].host == "turn.example.org" and services[0].port == 3478
    assert services[1].type == "stun"


def test_xep_0215_turns_service() -> None:
    el = ET.fromstring(
        "<services xmlns='urn:xmpp:extdisco:2'>"
        "<service type='turns' host='turn.example.org'/>"
        "</services>")
    services = turn.parse_services(el, now=1000)
    assert [(s.type, s.host, s.port, s.transport) for s in services] == [
        ("turn", "turn.example.org", 5349, "tls")]
    settings = FakeSettings({"calls.relay_only": False})
    _stun, turn_uris = turn.webrtc_uris(services, settings)
    assert turn_uris and turn_uris[0].startswith("turns://turn.example.org:5349")


def test_rest_credentials() -> None:
    username, password = turn.time_limited_credentials("secret", 3600, "alice", now=0)
    assert username == "3600:alice"
    assert password == base64.b64encode(hmac.new(b"secret", b"3600:alice", hashlib.sha1).digest()).decode()


def test_discovery_reads_services_inside_iq(run):
    """session.iq() returns the <iq/> result: parse its <services/> child, not the empty top level."""
    from types import SimpleNamespace

    class Core:
        settings = FakeSettings({"calls.discover": True})

    class Session:
        account = SimpleNamespace(id=1)
        domain = "example.org"

        async def iq(self, _type, _to, *_payload, timeout=15):
            return ET.fromstring(
                "<iq xmlns='jabber:client' type='result' id='x'>"
                "<services xmlns='urn:xmpp:extdisco:2'>"
                "<service type='turn' host='turn.example.org' port='3478' transport='udp' "
                "username='u' password='p'/>"
                "</services></iq>")

    services = run(turn.TurnService(Core()).discovered(Session()), timeout=10)
    assert len(services) == 1
    assert services[0].type == "turn" and services[0].host == "turn.example.org"
    assert services[0].username == "u" and services[0].password == "p"


def test_webrtc_uris() -> None:
    settings = FakeSettings({"calls.turn_secret": "", "calls.turn_user": "user", "calls.turn_password": "pass",
                             "calls.relay_only": False, "calls.turn_ttl": 3600})
    services = [turn.Service("stun", "stun.example.org", 3478),
                turn.Service("turn", "turn.example.org", 3478, "udp")]
    stun_uri, turn_uris = turn.webrtc_uris(services, settings)
    assert stun_uri == "stun://stun.example.org:3478"
    assert turn_uris == ["turn://user:pass@turn.example.org:3478?transport=udp"]

    settings["calls.turn_secret"] = "secret"
    _stun, turn_uris = turn.webrtc_uris(services, settings)
    userinfo, host = turn_uris[0].split("@", 1)
    assert userinfo.startswith("turn://")
    timestamp, name, password = userinfo[len("turn://"):].split(":", 2)
    assert timestamp.isdigit() and name == "user" and password
    assert host.startswith("turn.example.org:3478")

    settings["calls.relay_only"] = True
    stun_uri, turn_uris = turn.webrtc_uris(services, settings)
    assert stun_uri == "" and len(turn_uris) == 1


# ---------------------------------------------------------------------------------------------- fake TURN server
def _xor_relay(ip: str, port: int) -> bytes:
    raw = bytes(a ^ b for a, b in zip(socket.inet_aton(ip), stun.MAGIC_BYTES, strict=False))
    return b"\0\1" + struct.pack("!H", port ^ (stun.MAGIC >> 16)) + raw


def _error(code: int, reason: str, realm: str = "test", nonce: str = "nonce") -> bytes:
    attrs = [(stun.ATTR_ERROR_CODE, b"\0\0" + bytes([code // 100, code % 100]) + reason.encode())]
    if realm:
        attrs.append((stun.ATTR_REALM, realm.encode()))
    if nonce:
        attrs.append((stun.ATTR_NONCE, nonce.encode()))
    return attrs


async def _serve_once(sock, handler):
    loop = asyncio.get_running_loop()
    data, address = await loop.sock_recvfrom(sock, 65535)
    response = handler(data)
    await loop.sock_sendto(sock, response, address)


async def _start_fake(authenticated: bool, no_auth: bool = False):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    host, port = "127.0.0.1", sock.getsockname()[1]

    def first(data: bytes) -> bytes:
        request = stun._parse(data)
        if no_auth:
            return stun._message(stun.ALLOCATE_SUCCESS, request.txid,
                                 [(stun.ATTR_XOR_RELAYED_ADDRESS, _xor_relay("127.0.0.1", 50000))])
        return stun._message(stun.ALLOCATE_ERROR, request.txid, _error(401, "Unauthorized"))

    def second(data: bytes) -> bytes:
        request = stun._parse(data)
        if authenticated:
            assert request.first(stun.ATTR_USERNAME) == b"user"
            assert request.first(stun.ATTR_MESSAGE_INTEGRITY)
            return stun._message(stun.ALLOCATE_SUCCESS, request.txid,
                                 [(stun.ATTR_XOR_RELAYED_ADDRESS, _xor_relay("127.0.0.1", 50000))])
        return stun._message(stun.ALLOCATE_ERROR, request.txid, _error(401, "Unauthorized", nonce="new"))

    async def serve():
        await _serve_once(sock, first)
        if not no_auth:
            await _serve_once(sock, second)

    task = asyncio.create_task(serve())
    return host, port, sock, task


def test_allocate_authenticates(run):
    async def go():
        host, port, sock, task = await _start_fake(authenticated=True)
        try:
            result = await stun.allocate(turn.Service("turn", host, port), "user", "pass")
            await task
        finally:
            sock.close()
        assert result.ok and result.auth
        assert result.realm == "test" and result.relay == "127.0.0.1:50000"

    run(go(), timeout=10)


def test_allocate_without_authentication_is_reported(run):
    async def go():
        host, port, sock, task = await _start_fake(authenticated=True, no_auth=True)
        try:
            result = await stun.allocate(turn.Service("turn", host, port), "user", "pass")
            await task
        finally:
            sock.close()
        assert result.ok and not result.auth
        assert "without authentication" in result.message

    run(go(), timeout=10)


def test_allocate_missing_realm_is_reported(run):
    async def go():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        port = sock.getsockname()[1]

        def first(data: bytes) -> bytes:
            request = stun._parse(data)
            return stun._message(stun.ALLOCATE_ERROR, request.txid, _error(401, "Unauthorized", realm="",
                                                                           nonce=""))

        task = asyncio.create_task(_serve_once(sock, first))
        try:
            result = await stun.allocate(turn.Service("turn", "127.0.0.1", port), "user", "pass")
            await task
        finally:
            sock.close()
        assert not result.ok and not result.auth
        assert "no realm/nonce" in result.message

    run(go(), timeout=10)


def test_allocate_bad_credentials(run):
    async def go():
        host, port, sock, task = await _start_fake(authenticated=False)
        try:
            result = await stun.allocate(turn.Service("turn", host, port), "user", "pass")
            await task
        finally:
            sock.close()
        assert not result.ok and result.auth
        assert "authentication failed" in result.message.lower()

    run(go(), timeout=10)
