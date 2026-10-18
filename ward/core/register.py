"""Creating accounts: in-band registration (XEP-0077) with whatever form the server sends (CAPTCHAs included,
XEP-0158/0221/0231), invites with a pre-authentication token (XEP-0401/0379), and the server list from the
XMPP Providers project.

Registration needs a stream that is encrypted but not signed in.  We open that small stream ourselves
(same resolver, proxies and TLS rules as a session), which works identically on every slixmpp version."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

from .. import aio, util
from ..i18n import _
from . import forms, resolver
from . import proxy as proxymod
from .session import CONNECT_TIMEOUT, CertInfo, IqFailed, base_ssl_context, cert_info_from_der, probe_certificate
from .stanzas import NS_CLIENT, parse_error, to_xml

log = logging.getLogger("ward.register")

NS_STREAM = "http://etherx.jabber.org/streams"
NS_TLS = "urn:ietf:params:xml:ns:xmpp-tls"
NS_REGISTER = "jabber:iq:register"
NS_PARS = "urn:xmpp:pars:0"
NS_IBR_FEATURE = "http://jabber.org/features/iq-register"
PROVIDERS_URL = "https://data.xmpp.net/providers/v2/providers-Ds.json"


class RegistrationError(Exception):
    def __init__(self, message: str, condition: str = "", cert: CertInfo | None = None):
        super().__init__(message)
        self.condition = condition
        self.cert = cert


@dataclass
class RegistrationForm:
    domain: str
    form: forms.Form | None = None                 # a data form (preferred)
    legacy_fields: list[str] = field(default_factory=list)   # username/password/email when there is no form
    instructions: str = ""
    redirect_url: str = ""                         # registration only on a website (jabber:x:oob)
    registered: bool = False


class _Stream:
    """A bare XMPP client stream: TLS, features, iq.  No authentication."""

    def __init__(self, domain: str):
        self.domain = domain
        # set by open()
        self.reader: Any = None
        self.writer: Any = None
        self.parser: Any = None
        self.depth = 0
        self.queue: asyncio.Queue[ET.Element] = asyncio.Queue()
        self.features: ET.Element | None = None
        self._ids = 0

    async def _open_raw(self, cand: resolver.Candidate, prox, ctx: ssl.SSLContext) -> None:
        sock = await proxymod.open_socket(prox, cand.host, cand.port) if prox else None
        kw = {"sock": sock} if sock else {"host": cand.host, "port": cand.port}
        if cand.direct_tls:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(ssl=ctx, server_hostname=self.domain, **kw), CONNECT_TIMEOUT)
        else:
            self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(**kw), CONNECT_TIMEOUT)

    def _restart(self) -> None:
        self.parser = ET.XMLPullParser(events=("start", "end"))
        self.depth = 0
        self.writer.write(("<?xml version='1.0'?><stream:stream to='%s' xmlns='jabber:client' "
                           "xmlns:stream='%s' version='1.0'>" % (self.domain, NS_STREAM)).encode())

    async def _next(self, timeout: float = CONNECT_TIMEOUT) -> ET.Element:
        while self.queue.empty():
            chunk = await asyncio.wait_for(self.reader.read(65536), timeout)
            if not chunk:
                raise RegistrationError(_("The server closed the connection"))
            self.parser.feed(chunk)
            for ev, el in self.parser.read_events():
                if ev == "start":
                    self.depth += 1
                elif ev == "end":
                    self.depth -= 1
                    if self.depth == 1:
                        self.queue.put_nowait(el)
                    elif self.depth == 0:
                        raise RegistrationError(_("The server closed the stream"))
        return self.queue.get_nowait()

    async def open(self, cand: resolver.Candidate, prox, ctx: ssl.SSLContext) -> None:
        await self._open_raw(cand, prox, ctx)
        self._restart()
        feats = await self._next()
        if not cand.direct_tls:
            if feats.find("{%s}starttls" % NS_TLS) is None:
                raise RegistrationError(_("The server does not offer an encrypted connection"))
            self.writer.write(("<starttls xmlns='%s'/>" % NS_TLS).encode())
            answer = await self._next()
            if answer.tag != "{%s}proceed" % NS_TLS:
                raise RegistrationError(_("The server refused to encrypt the connection"))
            await asyncio.wait_for(self.writer.start_tls(ctx, server_hostname=self.domain), CONNECT_TIMEOUT)
            self._restart()
            feats = await self._next()
        self.features = feats

    async def iq(self, type_: str, payload: ET.Element, to: str | None = None, timeout: float = 30) -> ET.Element:
        self._ids += 1
        iq_id = "reg%d" % self._ids
        iq = ET.Element("{%s}iq" % NS_CLIENT, {"type": type_, "id": iq_id})
        if to:
            iq.set("to", to)
        iq.append(payload)
        self.writer.write(to_xml(iq).encode())
        await self.writer.drain()
        while True:
            el = await self._next(timeout)
            if el.tag == "{%s}iq" % NS_CLIENT and el.get("id") == iq_id:
                if el.get("type") == "error":
                    err = el.find("{%s}error" % NS_CLIENT)
                    cond, text = parse_error(err) if err is not None else ("undefined-condition", "")
                    raise IqFailed(cond, text, el)
                return el

    async def close(self) -> None:
        if self.writer is not None:
            with contextlib.suppress(Exception):
                self.writer.write(b"</stream:stream>")
                self.writer.close()


async def _connect(domain: str, options: dict, pinned_sha256: str = "") -> _Stream:
    prox = proxymod.from_options(options)
    if prox == "system":
        prox = await proxymod.system_proxy("xmpp://" + domain)
    prox = prox if isinstance(prox, proxymod.Proxy) else None
    cands = await resolver.candidates(domain, options.get("host", ""), int(options.get("port") or 0),
                                      options.get("tls", "auto"))
    last = ""
    for cand in cands:
        stream = _Stream(domain)
        ctx = base_ssl_context(verify=not pinned_sha256)
        try:
            await stream.open(cand, prox, ctx)
            if pinned_sha256:
                import hashlib

                der = stream.writer.get_extra_info("ssl_object").getpeercert(True)
                if hashlib.sha256(der).hexdigest() != pinned_sha256:
                    await stream.close()
                    raise RegistrationError(_("The server's certificate changed"))
            return stream
        except ssl.SSLCertVerificationError as e:
            await stream.close()
            try:
                der = await probe_certificate(cand.host, cand.port, cand.direct_tls, domain)
                info = cert_info_from_der(der, cand.host, cand.port, domain, getattr(e, "verify_message", str(e)))
            except Exception:
                info = None
            raise RegistrationError(_("The server's certificate is not trusted"), "certificate", info) from None
        except (TimeoutError, OSError, ssl.SSLError, proxymod.ProxyError) as e:
            await stream.close()
            last = str(e) or type(e).__name__
            continue
    raise RegistrationError(_("Could not reach %s (%s)") % (domain, last or _("no server found")), "network")


def _parse_query(domain: str, res: ET.Element) -> RegistrationForm:
    q = res.find("{%s}query" % NS_REGISTER)
    out = RegistrationForm(domain)
    if q is None:
        return out
    out.registered = q.find("{%s}registered" % NS_REGISTER) is not None
    out.instructions = q.findtext("{%s}instructions" % NS_REGISTER) or ""
    out.form = forms.find(q, res)
    oob = q.find("{jabber:x:oob}x")
    if oob is not None:
        out.redirect_url = oob.findtext("{jabber:x:oob}url") or ""
    for name in ("username", "password", "email", "name", "nick"):
        if q.find("{%s}%s" % (NS_REGISTER, name)) is not None:
            out.legacy_fields.append(name)
    return out


async def get_form(domain: str, options: dict | None = None, preauth: str = "", pinned_sha256: str = ""
                   ) -> RegistrationForm:
    """Ask a server for its registration form (on a throwaway stream)."""
    stream = await _connect(domain, options or {}, pinned_sha256)
    try:
        if preauth:
            await _preauth(stream, preauth)
        if stream.features is not None and stream.features.find("{%s}register" % NS_IBR_FEATURE) is None and \
                not preauth:
            log.debug("server does not advertise in-band registration; asking anyway")
        res = await stream.iq("get", ET.Element("{%s}query" % NS_REGISTER))
        return _parse_query(domain, res)
    except IqFailed as e:
        if e.condition in ("service-unavailable", "feature-not-implemented", "not-allowed"):
            raise RegistrationError(_("This server does not allow creating accounts from apps"), e.condition) from None
        raise RegistrationError(e.text or e.condition, e.condition) from None
    finally:
        await stream.close()


async def _preauth(stream: _Stream, token: str) -> None:
    el = ET.Element("{%s}preauth" % NS_PARS, {"token": token})
    try:
        await stream.iq("set", el)
    except IqFailed as e:
        raise RegistrationError(_("This invite is not valid any more"), e.condition) from None


async def register(domain: str, username: str, password: str, form: forms.Form | None = None,
                   legacy_extra: dict | None = None, options: dict | None = None, preauth: str = "",
                   pinned_sha256: str = "") -> str:
    """Create the account; returns the new JID.  The form (with CAPTCHA answers etc.) comes from get_form."""
    stream = await _connect(domain, options or {}, pinned_sha256)
    try:
        if preauth:
            await _preauth(stream, preauth)
        q = ET.Element("{%s}query" % NS_REGISTER)
        if form is not None and form.fields:
            f = forms.Form(type="submit", fields=[forms.Field(var=x.var, type=x.type, values=list(x.values))
                                                  for x in form.fields])
            if f.get("username") is not None or form.form_type == NS_REGISTER:
                f.set("username", username)
            if f.get("password") is not None or form.form_type == NS_REGISTER:
                f.set("password", password)
            q.append(forms.to_element(f, "submit"))
        else:
            ET.SubElement(q, "{%s}username" % NS_REGISTER).text = username
            ET.SubElement(q, "{%s}password" % NS_REGISTER).text = password
            for k, v in (legacy_extra or {}).items():
                ET.SubElement(q, "{%s}%s" % (NS_REGISTER, k)).text = v
        try:
            await stream.iq("set", q, timeout=60)
        except IqFailed as e:
            msg = {"conflict": _("This name is taken; try another one"),
                   "not-acceptable": _("The server did not accept these details (a CAPTCHA answer or a too weak "
                                       "password?)"),
                   "not-allowed": _("The server does not allow creating accounts right now"),
                   "resource-constraint": _("Too many new accounts from your address; try again later"),
                   "bad-request": _("Something is missing in the form")}.get(e.condition, e.condition)
            # the server's own explanation ("invitation only", "password too short"...) beats our guess
            raise RegistrationError(e.text or msg, e.condition) from None
        return "%s@%s" % (username.lower(), domain)
    finally:
        await stream.close()


async def change_password(session, new_password: str) -> None:
    q = ET.Element("{%s}query" % NS_REGISTER)
    ET.SubElement(q, "{%s}username" % NS_REGISTER).text = util.localpart(session.jid)
    ET.SubElement(q, "{%s}password" % NS_REGISTER).text = new_password
    await session.iq("set", session.domain, q)


async def delete_account(session) -> None:
    """Remove the account from the server (XEP-0077 cancellation)."""
    q = ET.Element("{%s}query" % NS_REGISTER)
    ET.SubElement(q, "{%s}remove" % NS_REGISTER)
    with contextlib.suppress(IqFailed):
        await session.iq("set", session.domain, q, timeout=20)


@dataclass
class Provider:
    domain: str
    free: bool | None = None
    in_band: bool | None = None
    upload_mb: int | None = None
    archive_days: int | None = None
    locations: list[str] = field(default_factory=list)
    web_signup: str = ""
    category: str = ""


def parse_providers(data) -> list[Provider]:
    """XMPP Providers JSON (https://providers.xmpp.net): a list of domains, or of objects with details."""
    out = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, str):
            out.append(Provider(item))
            continue
        if not isinstance(item, dict) or not isinstance(item.get("jid"), str):
            continue

        def val(key, item=item):
            v = item.get(key)
            return v.get("content") if isinstance(v, dict) and "content" in v else v

        p = Provider(item["jid"], category=str(item.get("category", "")))
        free = val("freeOfCharge")
        p.free = free if isinstance(free, bool) else None
        ibr = val("inBandRegistration")
        p.in_band = ibr if isinstance(ibr, bool) else None
        up = val("maximumHttpFileUploadFileSize")
        if isinstance(up, (int, float)) and up > 0:
            p.upload_mb = int(up) if up < 100000 else int(up / 1000000)
        days = val("maximumMessageArchiveManagementStorageTime")
        if isinstance(days, (int, float)) and days > 0:
            p.archive_days = int(days)
        locs = val("serverLocations")
        if isinstance(locs, list):
            p.locations = [str(x) for x in locs]
        web = val("registrationWebPage")
        if isinstance(web, dict):
            web = web.get("en") or next(iter(web.values()), "")
        p.web_signup = web if isinstance(web, str) else ""
        out.append(p)
    return out


async def fetch_providers(prox=None) -> list[Provider]:
    import json

    from . import http

    res = await aio.run_in_thread(lambda: http.request("GET", PROVIDERS_URL, prox=prox, timeout=10,
                                                       max_bytes=2 * 1024 * 1024))
    if res.status != 200:
        raise RegistrationError(_("Could not load the list of servers"))
    return parse_providers(json.loads(res.body))
