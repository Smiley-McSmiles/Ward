"""Proxies: SOCKS5 (with remote DNS, so .onion servers work through Tor) and HTTP CONNECT, for the XMPP
connection (asyncio) and for HTTP uploads and downloads (blocking sockets in the worker pool).  "System settings"
asks Gio.ProxyResolver, which follows GNOME, KDE and the environment variables."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import socket
import struct
from dataclasses import dataclass

from .. import aio


@dataclass(frozen=True)
class Proxy:
    kind: str           # socks5 | http
    host: str
    port: int
    user: str = ""
    password: str = ""


class ProxyError(OSError):
    pass


TOR = Proxy("socks5", "127.0.0.1", 9050)


def from_options(opts: dict, password: str = "") -> Proxy | str | None:
    """Proxy from account options; returns "system" when the system settings must be asked."""
    kind = opts.get("proxy", "system")
    if kind == "none":
        return None
    if kind == "tor":
        return TOR
    if kind == "system":
        return "system"
    host, port = opts.get("proxy_host", ""), int(opts.get("proxy_port") or 0)
    if not host:
        return None
    return Proxy("socks5" if kind == "socks5" else "http", host, port or (1080 if kind == "socks5" else 8080),
                 opts.get("proxy_user", ""), password)


async def system_proxy(url: str) -> Proxy | None:
    """Ask Gio for the proxy of a URL like 'xmpp://example.org' or 'https://upload.example.org/'."""
    try:
        from gi.repository import Gio

        resolver = Gio.ProxyResolver.get_default()
        proxies = await aio.gio_call(resolver.lookup_async, resolver.lookup_finish, url, None)
    except Exception:
        return None
    for p in proxies or []:
        if p == "direct://":
            return None
        scheme, _, rest = p.partition("://")
        hostport = rest.split("@")[-1].rstrip("/")
        host, _, port = hostport.rpartition(":")
        if not host:
            host, port = hostport, ""
        if scheme in ("socks", "socks5", "socks5h"):
            return Proxy("socks5", host, int(port or 1080))
        if scheme in ("http", "https"):
            return Proxy("http", host, int(port or 8080))
    return None


def _socks5_request(host: str, port: int) -> bytes:
    try:
        ip = ipaddress.ip_address(host)
        if ip.version == 4:
            addr = b"\x01" + ip.packed
        else:
            addr = b"\x04" + ip.packed
    except ValueError:
        raw = host.encode("idna")
        if len(raw) > 255:
            raise ProxyError("host name too long for SOCKS5") from None
        addr = b"\x03" + bytes([len(raw)]) + raw   # remote DNS: the proxy resolves (needed for .onion)
    return b"\x05\x01\x00" + addr + struct.pack(">H", port)


_SOCKS_ERRORS = {1: "general failure", 2: "not allowed by ruleset", 3: "network unreachable",
                 4: "host unreachable", 5: "connection refused", 6: "TTL expired", 7: "command not supported",
                 8: "address type not supported"}


def _http_request(host: str, port: int, proxy: Proxy) -> bytes:
    req = "CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n" % (host, port, host, port)
    if proxy.user:
        cred = base64.b64encode(("%s:%s" % (proxy.user, proxy.password)).encode()).decode()
        req += "Proxy-Authorization: Basic %s\r\n" % cred
    return (req + "\r\n").encode()


# ------------------------------------------------------------------------------------------- asyncio version
async def open_socket(proxy: Proxy, host: str, port: int, timeout: float = 30) -> socket.socket:
    """A connected, non-blocking socket tunnelled through the proxy, ready for loop.create_connection(sock=...)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(proxy.host, proxy.port, type=socket.SOCK_STREAM)
    if not infos:
        raise ProxyError("cannot resolve proxy %s" % proxy.host)
    fam, typ, proto, _, addr = infos[0]
    sock = socket.socket(fam, typ, proto)
    sock.setblocking(False)
    try:
        await asyncio.wait_for(loop.sock_connect(sock, addr), timeout)

        async def recv_exact(n: int) -> bytes:
            buf = b""
            while len(buf) < n:
                chunk = await asyncio.wait_for(loop.sock_recv(sock, n - len(buf)), timeout)
                if not chunk:
                    raise ProxyError("proxy closed the connection")
                buf += chunk
            return buf

        if proxy.kind == "socks5":
            methods = b"\x00\x02" if proxy.user else b"\x00"
            await loop.sock_sendall(sock, b"\x05" + bytes([len(methods)]) + methods)
            ver, method = await recv_exact(2)
            if ver != 5 or method == 0xFF:
                raise ProxyError("the SOCKS5 proxy refused our authentication methods")
            if method == 2:
                u, p = proxy.user.encode(), proxy.password.encode()
                await loop.sock_sendall(sock, b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
                _v, status = await recv_exact(2)
                if status != 0:
                    raise ProxyError("the SOCKS5 proxy rejected the user name or password")
            await loop.sock_sendall(sock, _socks5_request(host, port))
            head = await recv_exact(4)
            if head[1] != 0:
                raise ProxyError("SOCKS5: %s" % _SOCKS_ERRORS.get(head[1], "error %d" % head[1]))
            atyp = head[3]
            if atyp == 1:
                await recv_exact(4 + 2)
            elif atyp == 4:
                await recv_exact(16 + 2)
            else:
                (n,) = await recv_exact(1)
                await recv_exact(n + 2)
        else:
            await loop.sock_sendall(sock, _http_request(host, port, proxy))
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = await asyncio.wait_for(loop.sock_recv(sock, 1024), timeout)
                if not chunk:
                    raise ProxyError("proxy closed the connection")
                buf += chunk
                if len(buf) > 16384:
                    raise ProxyError("bad answer from the HTTP proxy")
            status = buf.split(b"\r\n", 1)[0].split(b" ")
            if len(status) < 2 or status[1] != b"200":
                raise ProxyError("HTTP proxy: %s" % buf.split(b"\r\n", 1)[0].decode("latin-1"))
        return sock
    except BaseException:
        sock.close()
        raise


# ------------------------------------------------------------------------------------------ blocking version
def open_socket_sync(proxy: Proxy, host: str, port: int, timeout: float = 30) -> socket.socket:
    sock = socket.create_connection((proxy.host, proxy.port), timeout)
    try:
        def recv_exact(n: int) -> bytes:
            buf = b""
            while len(buf) < n:
                chunk = sock.recv(n - len(buf))
                if not chunk:
                    raise ProxyError("proxy closed the connection")
                buf += chunk
            return buf

        if proxy.kind == "socks5":
            methods = b"\x00\x02" if proxy.user else b"\x00"
            sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
            ver, method = recv_exact(2)
            if ver != 5 or method == 0xFF:
                raise ProxyError("the SOCKS5 proxy refused our authentication methods")
            if method == 2:
                u, p = proxy.user.encode(), proxy.password.encode()
                sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
                if recv_exact(2)[1] != 0:
                    raise ProxyError("the SOCKS5 proxy rejected the user name or password")
            sock.sendall(_socks5_request(host, port))
            head = recv_exact(4)
            if head[1] != 0:
                raise ProxyError("SOCKS5: %s" % _SOCKS_ERRORS.get(head[1], "error %d" % head[1]))
            atyp = head[3]
            recv_exact(6 if atyp == 1 else 18 if atyp == 4 else recv_exact(1)[0] + 2)
        else:
            sock.sendall(_http_request(host, port, proxy))
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(1024)
                if not chunk:
                    raise ProxyError("proxy closed the connection")
                buf += chunk
            if buf.split(b"\r\n", 1)[0].split(b" ")[1:2] != [b"200"]:
                raise ProxyError("HTTP proxy: %s" % buf.split(b"\r\n", 1)[0].decode("latin-1"))
        return sock
    except BaseException:
        sock.close()
        raise
