"""STUN/TURN for calls: XEP-0215 discovery, manual servers, and time-limited TURN credentials.

coturn and eturnal both speak ordinary TURN, so the addresses found here become the turn:// URIs of GStreamer's
webrtcbin.  A TURN shared secret is the ``use-auth-secret`` setup: the username is an expiry timestamp and a name,
and the password is an HMAC-SHA1 of it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote, urlsplit

from ..i18n import _
from .session import IqFailed

log = logging.getLogger("ward.calls")

NS_EXTDISCO2 = "urn:xmpp:extdisco:2"
NS_EXTDISCO1 = "urn:xmpp:extdisco:1"
DEFAULT_TURN_PORT = 3478
DEFAULT_TURNS_PORT = 5349
DEFAULT_STUN_PORT = 3478
CACHE_SECONDS = 300


@dataclass
class Service:
    type: str                     # stun | turn
    host: str
    port: int = DEFAULT_TURN_PORT
    transport: str = "udp"        # udp | tcp | tls
    username: str = ""
    password: str = ""
    secret: str = ""
    expires: float = 0.0

    def key(self) -> tuple:
        return (self.type, self.host.lower(), self.port, self.transport)


def _parse_hostport(text: str, default_port: int) -> tuple[str, int] | None:
    text = text.strip()
    if not text:
        return None
    if any(text.lower().startswith(x) for x in ("stun:", "stuns:", "turn:", "turns:")) and "://" not in text:
        text = text.replace(":", "://", 1)
    elif "://" not in text:
        text = "//" + text
    try:
        split = urlsplit(text)
    except ValueError:
        return None
    host = split.hostname or ""
    if not host:
        return None
    try:
        port = split.port or default_port
    except ValueError:
        return None
    return host, port


def parse_manual(text: str, type_: str) -> list[Service]:
    """Parse the Preferences lists ("turn:host:port?transport=tcp ...")."""
    out = []
    for item in text.replace(",", " ").split():
        item = item.strip()
        if not item:
            continue
        scheme = item.split(":", 1)[0].lower() if ":" in item else type_
        default = 5349 if scheme in ("turns", "stuns") else (DEFAULT_TURN_PORT if type_ == "turn"
                                                             else DEFAULT_STUN_PORT)
        parsed = _parse_hostport(item, default)
        if parsed is None:
            continue
        host, port = parsed
        normalized = item
        if "://" not in normalized and normalized.lower().startswith(("stun:", "stuns:", "turn:", "turns:")):
            normalized = normalized.replace(":", "://", 1)
        elif "://" not in normalized:
            normalized = ("turn://" if type_ == "turn" else "stun://") + normalized
        split = urlsplit(normalized)
        query = (split.query or "").lower()
        transport = "udp"
        for value in ("udp", "tcp", "tls"):
            if "transport=" + value in query:
                transport = value
        if scheme in ("turns", "stuns") or transport == "tls":
            transport = "tls"
        kind = "stun" if scheme in ("stun", "stuns") else "turn" if scheme in ("turn", "turns") else type_
        out.append(Service(kind, host, port, transport))
    return out


def _expiry(value: str) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return 0.0


def parse_services(el, now: float | None = None) -> list[Service]:
    """Read a <services/> element from XEP-0215 v1 or v2."""
    now = time.time() if now is None else now
    out = []
    for child in el:
        if child.tag.split("}")[-1] != "service":
            continue
        raw_kind = (child.get("type") or "").lower()
        if raw_kind == "turns":
            # Some servers advertise TURN-over-TLS as its own service type.  GStreamer's webrtcbin takes
            # turns:// URIs, so keep it as a TURN service with transport tls.
            kind, default_transport, default_port = "turn", "tls", DEFAULT_TURNS_PORT
        elif raw_kind == "turn":
            kind, default_transport, default_port = "turn", "udp", DEFAULT_TURN_PORT
        elif raw_kind == "stun":
            kind, default_transport, default_port = "stun", "udp", DEFAULT_STUN_PORT
        else:
            continue
        host = child.get("host") or ""
        if not host:
            continue
        try:
            port = int(child.get("port") or default_port)
        except ValueError:
            continue
        transport = (child.get("transport") or default_transport).lower()
        if raw_kind == "turns" or transport.startswith("tls"):
            transport = "tls"
        elif transport in ("udp", "tcp"):
            pass
        else:
            transport = "udp"
        expires = _expiry(child.get("expires") or "")
        if not expires:
            try:
                expires = now + int(child.get("ttl") or 0)
            except ValueError:
                expires = 0.0
        out.append(Service(kind, host, port, transport, child.get("username") or "",
                           child.get("password") or "", child.get("secret") or "", expires))
    return out


def time_limited_credentials(secret: str, ttl: int, name: str = "ward", now: float | None = None) -> tuple[str, str]:
    """coturn/eturnal TURN REST credentials: username "expiry:name", password HMAC-SHA1."""
    expiry = int((time.time() if now is None else now) + max(300, ttl))
    username = "%d:%s" % (expiry, name or "ward")
    digest = hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    return username, base64.b64encode(digest).decode()


def _urn(scheme: str, host: str, port: int, username: str = "", password: str = "",
         transport: str = "udp") -> str:
    if scheme.startswith("turn") and username:
        # libnice's TURN URI format for time-limited credentials is
        # turn://timestamp:username:password@host:port, not the ordinary user:password form.
        timestamp, sep, name = username.partition(":")
        if sep and timestamp.isdigit():
            userinfo = "%s:%s:%s" % (timestamp, quote(name, safe=""), quote(password, safe=""))
        else:
            userinfo = quote(username, safe="") + ":" + quote(password, safe="")
        base = "%s://%s@%s:%d" % (scheme, userinfo, host, port)
    else:
        base = "%s://%s:%d" % (scheme, host, port)
    if scheme.startswith("turn"):
        base += "?transport=%s" % ("tcp" if transport in ("tcp", "tls") else "udp")
    return base


def _credentials(service: Service, settings) -> tuple[str, str]:
    if service.username:
        return service.username, service.password
    secret = service.secret or str(settings["calls.turn_secret"] or "")
    if secret:
        name = str(settings["calls.turn_user"] or "") or "ward"
        try:
            ttl = int(settings["calls.turn_ttl"])
        except (TypeError, ValueError):
            ttl = 86400
        return time_limited_credentials(secret, ttl, name)
    return str(settings["calls.turn_user"] or ""), str(settings["calls.turn_password"] or "")


def credentials_for(service: Service, settings) -> tuple[str, str]:
    return _credentials(service, settings)


async def test_service(service: Service, settings, timeout: float = 5):
    """A real Binding/Allocate request, without GStreamer; used by Preferences > Calls > Test."""
    from . import stun

    if service.type == "stun":
        return await stun.binding(service, timeout)
    username, password = credentials_for(service, settings)
    try:
        return await stun.allocate(service, username, password, timeout)
    except (OSError, TimeoutError, Exception) as e:  # TLS errors, DNS, malformed replies
        if isinstance(e, stun.StunError):
            return stun.Allocation(False, bool(username), message=str(e))
        return stun.Allocation(False, bool(username), message=str(e) or e.__class__.__name__)


def webrtc_uris(services: list[Service], settings) -> tuple[str, list[str]]:
    """The stun-server property and the add-turn-server URIs for GStreamer webrtcbin."""
    stun = ""
    turns: list[str] = []
    seen: set[tuple] = set()
    relay_only = bool(settings["calls.relay_only"])
    for service in services:
        if service.key() in seen:
            continue
        seen.add(service.key())
        if service.type == "stun":
            if not relay_only and not stun:
                stun = _urn("stun", service.host, service.port)
            continue
        username, password = _credentials(service, settings)
        scheme = "turns" if service.transport == "tls" else "turn"
        turns.append(_urn(scheme, service.host, service.port, username, password, service.transport))
    return stun, turns


class TurnService:
    """Per-account XEP-0215 discovery with a short cache; manual settings are always included."""

    def __init__(self, core):
        self.core = core
        self.settings = core.settings
        self._cache: dict[int, tuple[float, list[Service]]] = {}

    def manual(self) -> list[Service]:
        services = parse_manual(str(self.settings["calls.stun"] or ""), "stun")
        services += parse_manual(str(self.settings["calls.turn"] or ""), "turn")
        return services

    def clear(self, account_id: int | None = None) -> None:
        if account_id is None:
            self._cache.clear()
        else:
            self._cache.pop(account_id, None)

    async def discovered(self, session) -> list[Service]:
        if not self.settings["calls.discover"]:
            return []
        account_id = session.account.id
        cached = self._cache.get(account_id)
        if cached is not None and cached[0] > time.time():
            return cached[1]
        from xml.etree import ElementTree as ET

        el = None
        for ns, payload in ((NS_EXTDISCO2, ET.Element("{%s}services" % NS_EXTDISCO2)),
                            (NS_EXTDISCO1, ET.Element("{%s}services" % NS_EXTDISCO1))):
            try:
                el = await session.iq("get", session.domain, payload, timeout=15)
                break
            except IqFailed as e:
                log.debug("XEP-0215 discovery with %s failed: %s", ns, e.condition)
        if el is None:
            self._cache[account_id] = (time.time() + CACHE_SECONDS, [])
            return []
        # session.iq() returns the whole <iq/> result; the services are in its <services/> child.
        services_el = el.find("{%s}services" % ns)
        if services_el is None:
            services_el = next((c for c in el if c.tag.split("}")[-1] == "services"), None)
        services = parse_services(services_el if services_el is not None else el)
        if not services:
            self._cache[account_id] = (time.time() + CACHE_SECONDS, [])
            return []
        # never keep expired credentials; a refresh must fetch new ones
        expiry = min([s.expires for s in services if s.expires] or [time.time() + CACHE_SECONDS])
        self._cache[account_id] = (min(time.time() + CACHE_SECONDS, expiry - 60), services)
        return services

    async def for_session(self, session) -> list[Service]:
        services = self.manual()
        services += await self.discovered(session)
        return services

    async def uris(self, session) -> tuple[str, list[str]]:
        return webrtc_uris(await self.for_session(session), self.settings)


def describe(service: Service) -> str:
    return _("%(type)s at %(host)s:%(port)d") % {"type": service.type.upper(), "host": service.host,
                                                 "port": service.port}
