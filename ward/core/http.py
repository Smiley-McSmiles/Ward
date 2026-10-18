"""A small HTTP(S) client for uploads, downloads and link previews.

It uses the standard library (http.client) in the worker pool, so there is no extra dependency, and it goes
through the account's proxy (SOCKS5 with remote DNS for Tor, HTTP CONNECT) just like the XMPP connection.
Certificates are verified with the system's trust store; redirects are followed (at most 5, never from https
to http); every transfer has a size limit and can be cancelled."""

from __future__ import annotations

import http.client
import os
import ssl
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from .. import __version__
from . import proxy as proxymod

USER_AGENT = "Ward/%s" % __version__
CHUNK = 64 * 1024


class HttpError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class Cancelled(Exception):
    pass


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes = b""
    url: str = ""


@dataclass
class Transfer:
    """Shared between the worker thread and the interface: progress and cancellation."""
    total: int = 0
    done: int = 0
    cancel: threading.Event = field(default_factory=threading.Event)
    on_progress: Callable[[int, int], object] | None = None

    def tick(self, n: int) -> None:
        self.done += n
        if self.cancel.is_set():
            raise Cancelled()
        if self.on_progress is not None:
            self.on_progress(self.done, self.total)


def ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    extra = os.environ.get("WARD_EXTRA_CA")
    if extra and os.path.isfile(extra):
        ctx.load_verify_locations(cafile=extra)
    return ctx


def _connection(url: str, prox: proxymod.Proxy | None, timeout: float) -> tuple[http.client.HTTPConnection, str]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise HttpError("unsupported URL scheme: %s" % parts.scheme)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    if parts.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(host, port, timeout=timeout,
                                                                      context=ssl_context())
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    if prox is not None:
        sock = proxymod.open_socket_sync(prox, host, port, timeout)
        if parts.scheme == "https":
            sock = ssl_context().wrap_socket(sock, server_hostname=host)
        sock.settimeout(timeout)
        conn.sock = sock
    return conn, path


def request(method: str, url: str, headers: dict[str, str] | None = None, body: bytes | None = None,
            body_file: str | None = None, prox: proxymod.Proxy | None = None, timeout: float = 30,
            max_bytes: int | None = None, out_file: str | None = None, transfer: Transfer | None = None,
            redirects: int = 5, allow_http: bool = False) -> Response:
    """Blocking; run it with aio.run_in_thread()."""
    hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    hdrs.update(headers or {})
    current = url
    for _ in range(redirects + 1):
        if urlsplit(current).scheme == "http" and not allow_http:
            raise HttpError("refusing an unencrypted http:// address")
        conn, path = _connection(current, prox, timeout)
        try:
            if body_file is not None:
                size = os.path.getsize(body_file)
                hdrs["Content-Length"] = str(size)
                conn.putrequest(method, path, skip_accept_encoding=True)
                for k, v in hdrs.items():
                    conn.putheader(k, v)
                conn.endheaders()
                with open(body_file, "rb") as fh:
                    while True:
                        chunk = fh.read(CHUNK)
                        if not chunk:
                            break
                        conn.send(chunk)
                        if transfer is not None:
                            transfer.tick(len(chunk))
            else:
                if body is not None:
                    hdrs["Content-Length"] = str(len(body))
                conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            rh = {k.lower(): v for k, v in resp.getheaders()}
            if resp.status in (301, 302, 303, 307, 308) and "location" in rh and method in ("GET", "HEAD"):
                nxt = urljoin(current, rh["location"])
                if urlsplit(current).scheme == "https" and urlsplit(nxt).scheme != "https":
                    raise HttpError("redirect to an unencrypted address refused")
                current = nxt
                resp.read()
                continue
            length = int(rh.get("content-length") or 0)
            if max_bytes is not None and length > max_bytes:
                raise HttpError("the file is larger than allowed (%d bytes)" % length, resp.status)
            if transfer is not None and out_file is not None:
                transfer.total = length or transfer.total
                transfer.done = 0
            data = b""
            read = 0
            part = (out_file or "") + ".part"
            out_fh = open(part, "wb") if out_file is not None and resp.status < 300 else None  # noqa: SIM115
            try:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    read += len(chunk)
                    if max_bytes is not None and read > max_bytes:
                        if out_fh is None:
                            data += chunk[: max(0, max_bytes - (read - len(chunk)))]
                            break
                        raise HttpError("the file is larger than allowed", resp.status)
                    if out_fh is not None:
                        out_fh.write(chunk)
                        if transfer is not None:
                            transfer.tick(len(chunk))
                    else:
                        data += chunk
            except BaseException:
                if out_fh is not None:
                    out_fh.close()
                    os.unlink(part)
                    out_fh = None
                raise
            finally:
                if out_fh is not None and out_file is not None:
                    out_fh.close()
                    os.replace(part, out_file)
            return Response(resp.status, rh, data, current)
        finally:
            conn.close()
    raise HttpError("too many redirects")
