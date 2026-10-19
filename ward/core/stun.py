"""A small STUN/TURN client, used to test coturn/eturnal from Preferences.

It performs a Binding request (STUN) or an Allocate request (TURN, RFC 5766).  The first unauthenticated Allocate
shows whether the server actually challenges with a realm and nonce; the authenticated retry checks the static
credentials or the REST secret Ward also uses for calls.  This intentionally does not depend on GStreamer, so it can
diagnose a relay even when calls cannot start yet.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import socket
import ssl
import struct
from dataclasses import dataclass

MAGIC = 0x2112A442
MAGIC_BYTES = struct.pack("!I", MAGIC)

BINDING_REQUEST = 0x0001
BINDING_SUCCESS = 0x0101
BINDING_ERROR = 0x0111
ALLOCATE_REQUEST = 0x0003
ALLOCATE_SUCCESS = 0x0103
ALLOCATE_ERROR = 0x0113

ATTR_MAPPED_ADDRESS = 0x0001
ATTR_USERNAME = 0x0006
ATTR_MESSAGE_INTEGRITY = 0x0008
ATTR_ERROR_CODE = 0x0009
ATTR_LIFETIME = 0x000D
ATTR_REALM = 0x0014
ATTR_NONCE = 0x0015
ATTR_XOR_RELAYED_ADDRESS = 0x0016
ATTR_REQUESTED_TRANSPORT = 0x0019
ATTR_XOR_MAPPED_ADDRESS = 0x0020
ATTR_SOFTWARE = 0x8022
ATTR_FINGERPRINT = 0x8028

TURN_UDP = 17


class StunError(Exception):
    pass


@dataclass
class Response:
    type: int
    txid: bytes
    attributes: dict[int, list[bytes]]

    def first(self, attr: int) -> bytes | None:
        values = self.attributes.get(attr)
        return values[0] if values else None

    def text(self, attr: int) -> str:
        value = self.first(attr)
        return value.decode("utf-8", "replace") if value else ""


@dataclass
class Allocation:
    ok: bool
    auth: bool
    realm: str = ""
    nonce: bool = False
    relay: str = ""
    lifetime: int = 0
    message: str = ""


@dataclass
class Binding:
    ok: bool
    address: str = ""
    message: str = ""


def _attribute(kind: int, value: bytes) -> bytes:
    pad = (-len(value)) % 4
    return struct.pack("!HH", kind, len(value)) + value + b"\0" * pad


def _message(kind: int, txid: bytes, attributes: list[tuple[int, bytes]], key: bytes | None = None) -> bytes:
    body = b"".join(_attribute(k, v) for k, v in attributes)
    if key is not None:
        # MESSAGE-INTEGRITY covers the header with its length already including the 24-byte attribute.
        unsigned = struct.pack("!HHI", kind, len(body) + 24, MAGIC) + txid + body
        body += _attribute(ATTR_MESSAGE_INTEGRITY, hmac.new(key, unsigned, hashlib.sha1).digest())
    return struct.pack("!HHI", kind, len(body), MAGIC) + txid + body


def _parse(data: bytes, expected_txid: bytes | None = None) -> Response:
    if len(data) < 20:
        raise StunError("short STUN response")
    kind, length, magic = struct.unpack("!HHI", data[:8])
    if magic != MAGIC:
        raise StunError("not a STUN response (bad magic cookie)")
    txid = data[8:20]
    if expected_txid is not None and txid != expected_txid:
        raise StunError("STUN transaction id does not match")
    body = data[20:20 + length]
    if len(body) < length:
        raise StunError("truncated STUN response")
    attrs: dict[int, list[bytes]] = {}
    pos = 0
    while pos + 4 <= len(body):
        atype, alen = struct.unpack("!HH", body[pos:pos + 4])
        start = pos + 4
        end = start + alen
        if end > len(body):
            raise StunError("truncated STUN attribute")
        attrs.setdefault(atype, []).append(body[start:end])
        pos = end + ((-alen) % 4)
    return Response(kind, txid, attrs)


def _error(response: Response) -> tuple[int, str]:
    value = response.first(ATTR_ERROR_CODE) or b""
    if len(value) < 4:
        return 0, ""
    code = (value[2] & 0x07) * 100 + value[3]
    return code, value[4:].decode("utf-8", "replace")


def _xor_address(value: bytes, txid: bytes) -> str:
    if len(value) < 8:
        return ""
    family = value[1]
    port = struct.unpack("!H", value[2:4])[0] ^ (MAGIC >> 16)
    if family == 1 and len(value) >= 8:
        raw = bytes(a ^ b for a, b in zip(value[4:8], MAGIC_BYTES, strict=False))
        return "%s:%d" % (socket.inet_ntop(socket.AF_INET, raw), port)
    if family == 2 and len(value) >= 20:
        mask = MAGIC_BYTES + txid
        raw = bytes(a ^ b for a, b in zip(value[4:20], mask, strict=False))
        return "[%s]:%d" % (socket.inet_ntop(socket.AF_INET6, raw), port)
    return ""


def _text_address(value: bytes) -> str:
    if len(value) < 8:
        return ""
    family = value[1]
    port = struct.unpack("!H", value[2:4])[0]
    if family == 1 and len(value) >= 8:
        return "%s:%d" % (socket.inet_ntop(socket.AF_INET, value[4:8]), port)
    if family == 2 and len(value) >= 20:
        return "[%s]:%d" % (socket.inet_ntop(socket.AF_INET6, value[4:20]), port)
    return ""


def _key(username: str, realm: str, password: str) -> bytes:
    return hashlib.md5(("%s:%s:%s" % (username, realm, password)).encode()).digest()


async def _request(service, packet: bytes, txid: bytes, timeout: float) -> bytes:
    loop = asyncio.get_running_loop()
    if service.transport in ("tcp", "tls"):
        ctx = ssl.create_default_context() if service.transport == "tls" else None
        reader, writer = await asyncio.wait_for(asyncio.open_connection(service.host, service.port, ssl=ctx),
                                                timeout)
        try:
            writer.write(packet)
            await writer.drain()
            header = await asyncio.wait_for(reader.readexactly(20), timeout)
            _kind, length, _magic = struct.unpack("!HHI", header[:8])
            body = await asyncio.wait_for(reader.readexactly(length), timeout)
            return header + body
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
    # UDP: retry until a response with our transaction id arrives (stray packets are ignored)
    infos = await loop.getaddrinfo(service.host, service.port, type=socket.SOCK_DGRAM)
    family, socktype, proto, _canon, address = infos[0]
    sock = socket.socket(family, socktype, proto)
    sock.setblocking(False)
    try:
        await loop.sock_sendto(sock, packet, address)
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            data, _addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 65535), remaining)
            try:
                response = _parse(data)
            except StunError:
                continue
            if response.txid == txid:
                return data
    finally:
        sock.close()


async def _exchange(service, kind: int, attributes: list[tuple[int, bytes]], key: bytes | None,
                    timeout: float) -> Response:
    txid = os.urandom(12)
    packet = _message(kind, txid, attributes, key)
    data = await _request(service, packet, txid, timeout)
    return _parse(data, txid)


async def allocate(service, username: str, password: str, timeout: float = 5) -> Allocation:
    """Try an unauthenticated Allocate, then the authenticated one; report what the server actually did."""
    requested = struct.pack("!B3x", TURN_UDP)
    try:
        first = await _exchange(service, ALLOCATE_REQUEST, [(ATTR_REQUESTED_TRANSPORT, requested)], None, timeout)
    except TimeoutError:
        return Allocation(False, False, message="The TURN server did not answer")
    if first.type == ALLOCATE_SUCCESS:
        relay = _xor_address(first.first(ATTR_XOR_RELAYED_ADDRESS) or b"", first.txid)
        return Allocation(True, False, relay=relay,
                          lifetime=_lifetime(first),
                          message="TURN answered without authentication")
    if first.type != ALLOCATE_ERROR:
        return Allocation(False, False, message="Unexpected TURN response")
    code, reason = _error(first)
    realm = first.text(ATTR_REALM)
    nonce = first.first(ATTR_NONCE) or b""
    if not realm or not nonce:
        return Allocation(False, False, realm=realm, nonce=bool(nonce),
                          message="Authentication is not enabled: no realm/nonce in the 401 response")
    if not username:
        return Allocation(False, True, realm=realm, nonce=True,
                          message="TURN requires authentication, but no username is configured")
    key = _key(username, realm, password)
    attrs = [
        (ATTR_USERNAME, username.encode()),
        (ATTR_REALM, realm.encode()),
        (ATTR_NONCE, nonce),
        (ATTR_REQUESTED_TRANSPORT, struct.pack("!B3x", TURN_UDP)),
    ]
    try:
        second = await _exchange(service, ALLOCATE_REQUEST, attrs, key, timeout)
    except TimeoutError:
        return Allocation(False, True, realm=realm, nonce=True,
                          message="The TURN server did not answer the authenticated request")
    if second.type == ALLOCATE_SUCCESS:
        relay = _xor_address(second.first(ATTR_XOR_RELAYED_ADDRESS) or b"", second.txid)
        return Allocation(True, True, realm=realm, nonce=True, relay=relay, lifetime=_lifetime(second),
                          message="TURN allocation succeeded")
    code, reason = _error(second)
    if code == 438:       # stale nonce: one retry with the fresh one
        nonce = second.first(ATTR_NONCE) or nonce
        attrs[2] = (ATTR_NONCE, nonce)
        try:
            retry = await _exchange(service, ALLOCATE_REQUEST, attrs, key, timeout)
        except TimeoutError:
            retry = None
        if retry is not None and retry.type == ALLOCATE_SUCCESS:
            relay = _xor_address(retry.first(ATTR_XOR_RELAYED_ADDRESS) or b"", retry.txid)
            return Allocation(True, True, realm=realm, nonce=True, relay=relay, lifetime=_lifetime(retry),
                              message="TURN allocation succeeded")
        if retry is not None:
            code, reason = _error(retry)
    if code in (401, 403):
        message = "TURN authentication failed: %s" % (reason or "check the username/password or shared secret")
    else:
        message = "TURN allocation failed: %s" % (reason or "error %d" % code)
    return Allocation(False, True, realm=realm, nonce=True, message=message)


def _lifetime(response: Response) -> int:
    value = response.first(ATTR_LIFETIME)
    if value and len(value) >= 4:
        return struct.unpack("!I", value[:4])[0]
    return 0


async def binding(service, timeout: float = 5) -> Binding:
    try:
        response = await _exchange(service, BINDING_REQUEST, [], None, timeout)
    except TimeoutError:
        return Binding(False, message="The STUN server did not answer")
    if response.type != BINDING_SUCCESS:
        code, reason = _error(response)
        return Binding(False, message="STUN request failed: %s" % (reason or "error %d" % code))
    address = _xor_address(response.first(ATTR_XOR_MAPPED_ADDRESS) or b"", response.txid)
    if not address:
        address = _text_address(response.first(ATTR_MAPPED_ADDRESS) or b"")
    return Binding(True, address, "STUN request succeeded")
