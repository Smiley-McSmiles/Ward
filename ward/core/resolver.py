"""Finding a server: DNS SRV records for STARTTLS (_xmpp-client) and direct TLS (_xmpps-client, XEP-0368).

We resolve with Gio.Resolver instead of slixmpp's resolver: it works the same on every distribution and BSD
(no aiodns needed), honours the system's DNS setup, and works inside the Flatpak sandbox.  The result is an
ordered list of candidates; the session tries them one by one."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from gi.repository import Gio, GLib

from .. import aio

log = logging.getLogger("ward.resolver")


@dataclass(frozen=True)
class Candidate:
    host: str
    port: int
    direct_tls: bool

    def __str__(self) -> str:
        return "%s:%d (%s)" % (self.host, self.port, "direct TLS" if self.direct_tls else "STARTTLS")


def _order(targets: list[tuple[int, int, str, int]]) -> list[tuple[str, int]]:
    """RFC 2782 ordering: by priority, weighted random within a priority."""
    out = []
    for prio in sorted({t[0] for t in targets}):
        group = [t for t in targets if t[0] == prio]
        while group:
            total = sum(t[1] for t in group)
            if total == 0:
                pick = group[0]
            else:
                r = random.uniform(0, total)
                acc = 0.0
                pick = group[-1]
                for t in group:
                    acc += t[1]
                    if r <= acc:
                        pick = t
                        break
            group.remove(pick)
            out.append((pick[2], pick[3]))
    return out


async def lookup_srv(service: str, domain: str) -> list[tuple[str, int]] | None:
    """Ordered (host, port) targets, [] when the domain says "no service" (target '.'), None when no records."""
    resolver = Gio.Resolver.get_default()
    try:
        targets = await aio.gio_call(resolver.lookup_service_async, resolver.lookup_service_finish, service, "tcp",
                                     domain, None)
    except GLib.Error as e:
        log.debug("no %s SRV records for %s: %s", service, domain, e.message)
        return None
    rows = [(t.get_priority(), t.get_weight(), t.get_hostname(), t.get_port()) for t in targets]
    if len(rows) == 1 and rows[0][2] in (".", ""):
        return []
    return _order([r for r in rows if r[2] not in (".", "")])


async def candidates(domain: str, host: str = "", port: int = 0, tls: str = "auto") -> list[Candidate]:
    """Where to connect for a JID domain.  A manual host (and port) override skips DNS SRV."""
    if host:
        if tls == "direct":
            return [Candidate(host, port or 5223, True)]
        if tls == "starttls" or port not in (0, 443, 5223):
            return [Candidate(host, port or 5222, False)]
        return [Candidate(host, port or 5223, True), Candidate(host, port or 5222, False)]
    want_direct = tls in ("auto", "direct")
    want_start = tls in ("auto", "starttls")
    direct = await lookup_srv("xmpps-client", domain) if want_direct else None
    start = await lookup_srv("xmpp-client", domain) if want_start else None
    out: list[Candidate] = []
    # XEP-0368: direct TLS first when both are published (it saves a round trip and hides the STARTTLS step)
    for h, p in direct or []:
        out.append(Candidate(h, p, True))
    for h, p in start or []:
        out.append(Candidate(h, p, False))
    if not out and direct is None and start is None:
        if want_start:
            out.append(Candidate(domain, 5222, False))
        if want_direct:
            out.append(Candidate(domain, 5223, True))
    seen: set[Candidate] = set()
    return [c for c in out if not (c in seen or seen.add(c))]


async def lookup_host(host: str) -> list[str]:
    resolver = Gio.Resolver.get_default()
    addrs = await aio.gio_call(resolver.lookup_by_name_async, resolver.lookup_by_name_finish, host, None)
    return [a.to_string() for a in addrs]
