"""AccountSession: one connected XMPP account.

It wraps a slixmpp ClientXMPP but opens the network connection itself: candidates come from our resolver,
proxies (SOCKS5/Tor, HTTP CONNECT) are applied, TLS always verifies the JID's domain (never an SRV target), and
a certificate the system does not trust can be inspected and pinned.  This works the same on slixmpp 1.8 to 1.17,
whose connection APIs differ a lot.  Reconnects use exponential backoff with jitter, capped by a setting, and
happen at once when the network comes back.  Stream management (XEP-0198) resumes short drops without losing
messages.

Services (roster, chats, history, OMEMO...) attach to the session's signals; the session itself knows nothing
about them."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import random
import ssl
import time
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from gi.repository import GObject

from .. import __version__, aio, log as wardlog, util
from ..i18n import _
from . import proxy as proxymod
from . import resolver, secrets
from .models import AccountItem
from .stanzas import NS_CLIENT, NS_STANZAS, parse_error

logging.getLogger("slixmpp.stringprep").setLevel(logging.ERROR)
log = logging.getLogger("ward.session")

NS_IDLE = "urn:xmpp:idle:1"
CONNECT_TIMEOUT = 20
NEGOTIATION_TIMEOUT = 45


class IqFailed(Exception):
    def __init__(self, condition: str, text: str = "", element: ET.Element | None = None):
        super().__init__(text or condition)
        self.condition = condition
        self.text = text
        self.element = element


class NotConnected(Exception):
    pass


@dataclass
class CertInfo:
    """What we show when a server's certificate is not trusted, so the user can decide to pin it."""
    host: str
    port: int
    domain: str
    sha256: str
    subject: str = ""
    issuer: str = ""
    names: list[str] = field(default_factory=list)
    not_before: str = ""
    not_after: str = ""
    error: str = ""
    der: bytes = b""

    def pretty_fingerprint(self) -> str:
        fp = self.sha256.upper()
        return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))


def cert_info_from_der(der: bytes, host: str, port: int, domain: str, error: str = "") -> CertInfo:
    info = CertInfo(host, port, domain, hashlib.sha256(der).hexdigest(), error=error, der=der)
    try:
        from cryptography import x509

        c = x509.load_der_x509_certificate(der)
        info.subject = c.subject.rfc4514_string()
        info.issuer = c.issuer.rfc4514_string()
        try:
            san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            info.names = [str(n) for n in san.get_values_for_type(x509.DNSName)]
        except x509.ExtensionNotFound:
            pass
        nb = getattr(c, "not_valid_before_utc", None) or c.not_valid_before
        na = getattr(c, "not_valid_after_utc", None) or c.not_valid_after
        info.not_before, info.not_after = nb.strftime("%Y-%m-%d"), na.strftime("%Y-%m-%d")
    except Exception as e:  # unparseable: still show the fingerprint
        log.debug("could not parse certificate: %s", e)
    return info


def base_ssl_context(verify: bool = True) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    extra = os.environ.get("WARD_EXTRA_CA")
    if extra and os.path.isfile(extra):
        ctx.load_verify_locations(cafile=extra)
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def probe_certificate(host: str, port: int, direct_tls: bool, domain: str,
                            sock_factory=None) -> bytes:
    """Fetch the server's certificate without verifying it (for the "do you trust this?" dialog)."""
    ctx = base_ssl_context(verify=False)
    sock = await sock_factory() if sock_factory else None
    kw = {"sock": sock} if sock else {"host": host, "port": port}
    if direct_tls:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ssl=ctx, server_hostname=domain, **kw),
                                                CONNECT_TIMEOUT)
    else:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(**kw), CONNECT_TIMEOUT)
        writer.write(("<?xml version='1.0'?><stream:stream to='%s' xmlns='jabber:client' "
                      "xmlns:stream='http://etherx.jabber.org/streams' version='1.0'>" % domain).encode())
        await writer.drain()
        buf = b""
        while b"</stream:features>" not in buf:
            chunk = await asyncio.wait_for(reader.read(4096), CONNECT_TIMEOUT)
            if not chunk:
                raise OSError("the server closed the connection")
            buf += chunk
        if b"starttls" not in buf:
            writer.close()
            raise OSError("the server does not offer encryption (STARTTLS)")
        writer.write(b"<starttls xmlns='urn:ietf:params:xml:ns:xmpp-tls'/>")
        await writer.drain()
        buf = b""
        while b"proceed" not in buf:
            chunk = await asyncio.wait_for(reader.read(1024), CONNECT_TIMEOUT)
            if not chunk or b"failure" in chunk:
                raise OSError("STARTTLS failed")
            buf += chunk
        await asyncio.wait_for(writer.start_tls(ctx, server_hostname=domain), CONNECT_TIMEOUT)
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(True)
    finally:
        writer.close()
    return der


def _ssl_error_text(e: BaseException) -> str:
    v = getattr(e, "verify_message", "") or ""
    return v or str(e)


class AccountSession(GObject.Object):
    __gtype_name__ = "WardAccountSession"
    __gsignals__ = {
        "message": (GObject.SignalFlags.RUN_LAST, None, (object,)),      # raw <message/> ET element
        "presence": (GObject.SignalFlags.RUN_LAST, None, (object,)),     # raw <presence/> ET element
        "online": (GObject.SignalFlags.RUN_LAST, None, (bool,)),         # resumed?
        "offline": (GObject.SignalFlags.RUN_LAST, None, ()),
        "acked": (GObject.SignalFlags.RUN_LAST, None, (str,)),           # outgoing message id acked by server
        "cert-problem": (GObject.SignalFlags.RUN_LAST, None, (object,)),  # CertInfo
        "xml": (GObject.SignalFlags.RUN_LAST, None, (bool, str)),        # (incoming, stanza) for the console
    }

    def __init__(self, account: AccountItem, settings=None, handlers=None):
        super().__init__()
        self.account = account
        self.settings = settings
        self.xmpp = None
        self.password: str | None = None
        self.wanted = False
        self.bound_jid = ""
        self.server_features: set[str] = set()
        self.server_identities: list[tuple[str, str, str]] = []
        self.server_info = None                   # disco.Info of the server
        self.account_features: set[str] = set()  # disco#info of our bare JID (MAM, PEP...)
        self.services: dict[str, dict] = {}      # component jid -> {"features": set, "identities": [...]}
        self.csi_available = False
        self.inactive = False
        self.idle_since: float | None = None
        self.console = False
        self._handlers = handlers or {}          # (namespace, tag) -> callback(session, iq) for iq pushes
        self._connect_task: asyncio.Task | None = None
        self._retry_handle: asyncio.Handle | None = None
        self._negotiation_timer: asyncio.TimerHandle | None = None
        self._attempt = 0
        self._candidate: resolver.Candidate | None = None
        self._pinned_fp: str | None = None       # set while connecting with a pinned certificate
        self._cert_error: CertInfo | None = None
        self._last_error = ""
        self._online_event = asyncio.Event()

    # ================================================================== helpers
    @property
    def jid(self) -> str:
        return self.account.jid

    @property
    def domain(self) -> str:
        return util.domain(self.account.jid)

    @property
    def online(self) -> bool:
        return self.account.state == "online"

    def _set_state(self, state: str, step: str = "", error: str = "", kind: str = "") -> None:
        self.account.update(state=state, step=step, error=error, error_kind=kind)

    def setting(self, key: str, default=None):
        return self.settings[key] if self.settings is not None else default

    # ================================================================== client
    def _make_client(self):
        from slixmpp import ClientXMPP
        from slixmpp.xmlstream.handler import Callback
        from slixmpp.xmlstream.matcher import MatchXPath

        resource = self.account.option("resource") or util.random_resource()
        allow_plain = bool(self.account.option("allow_plain"))
        mech_cfg = {"unencrypted_plain": False, "unencrypted_scram": False, "unencrypted_digest": False,
                    "unencrypted_cram": False, "encrypted_plain": allow_plain,
                    "security_callback": self._security}
        x = ClientXMPP("%s/%s" % (self.account.jid, resource), self.password or "",
                       plugin_config={"feature_mechanisms": mech_cfg})
        x.auto_authorize = None          # subscription requests are the user's decision
        x.auto_subscribe = False
        x.use_message_ids = True
        if hasattr(x, "use_origin_id"):
            x.use_origin_id = False      # we add origin-ids ourselves
        x.whitespace_keepalive = True
        x.whitespace_keepalive_interval = 120
        x.ssl_context = base_ssl_context()
        x.ca_certs = None
        # Ping timeouts and stream errors make slixmpp reconnect on its own, through its own resolver: route them
        # through ours instead.
        x.reconnect = self._slixmpp_reconnect  # type: ignore[method-assign]
        if hasattr(x, "enable_direct_tls"):
            x.enable_plaintext = False

        for plugin in ("xep_0030", "xep_0004", "xep_0059", "xep_0060", "xep_0163", "xep_0128", "xep_0115",
                       "xep_0199", "xep_0352", "xep_0297"):
            x.register_plugin(plugin)
        x.register_plugin("xep_0198", {"window": 1, "allow_resume": True})
        if self.account.option("answer_version"):
            x.register_plugin("xep_0092", {"software_name": "Ward", "version": __version__})
        if self.account.option("answer_time"):
            x.register_plugin("xep_0202")
        if self.account.option("answer_last"):
            x.register_plugin("xep_0012")
        with contextlib.suppress(AttributeError, KeyError):
            x.plugin["xep_0030"].add_identity(category="client", itype="pc", name="Ward")
        # We handle the roster (with versioning in our database) and all messages and presences ourselves.
        x.remove_handler("Roster Update")
        x.register_handler(Callback("ward-message", MatchXPath("{%s}message" % NS_CLIENT), self._on_message))
        x.register_handler(Callback("ward-presence", MatchXPath("{%s}presence" % NS_CLIENT), self._on_presence))
        # Only the pushes we process: anything else must reach slixmpp's "feature-not-implemented" reply.
        for (ns, tag), cb in self._handlers.items():
            x.register_handler(Callback("ward-iq-%s" % ns, MatchXPath("{%s}iq/{%s}%s" % (NS_CLIENT, ns, tag)),
                                        lambda stanza, cb=cb: self._on_iq(stanza, cb)))

        x.add_event_handler("connected", lambda *_: self._step("securing" if not self._direct else "signing-in"))
        x.add_event_handler("tls_success", lambda *_: self._step("signing-in"))
        x.add_event_handler("ssl_cert", self._on_ssl_cert)
        x.add_event_handler("ssl_invalid_chain", self._on_invalid_chain)
        x.add_event_handler("session_start", self._on_session_start)
        x.add_event_handler("session_resumed", self._on_session_resumed)
        x.add_event_handler("disconnected", self._on_disconnected)
        x.add_event_handler("failed_auth", self._on_failed_auth)
        x.add_event_handler("failed_all_auth", self._on_failed_all_auth)
        x.add_event_handler("stream_error", self._on_stream_error)
        x.add_event_handler("stanza_acked", self._on_acked)
        x.add_event_handler("csi_enabled", lambda *_: setattr(self, "csi_available", True))
        x.add_filter("in", self._console_in)
        x.add_filter("out", self._console_out)
        if hasattr(x, "init_plugins"):
            x.init_plugins()
        return x

    _direct = False

    def _step(self, step: str) -> None:
        if self.account.state == "connecting":
            self.account.step = step

    def _security(self, values):
        transport = getattr(self.xmpp, "transport", None)
        ssl_obj = transport.get_extra_info("ssl_object") if transport is not None else None
        out = {}
        for v in values:
            if v == "encrypted":
                out[v] = ssl_obj is not None
            elif v == "tls_version":
                out[v] = ssl_obj.version() if ssl_obj is not None else None
            elif v == "binding_proposed":
                mechs = getattr(self.xmpp.plugin["feature_mechanisms"], "mech_list", set()) or set()
                out[v] = any(m.endswith("-PLUS") for m in mechs)
            else:  # encrypted_plain, unencrypted_scram...: our plugin configuration
                out[v] = bool(self.xmpp.plugin["feature_mechanisms"].config.get(v, False))
        return out

    # ================================================================== connecting
    async def start(self, password: str | None = None) -> None:
        """Connect and stay connected until stop()."""
        if self.account.state in ("connecting", "online") and self.wanted:
            return
        self.wanted = True
        if password is not None:
            self.password = password
        if not self.password:
            try:
                self.password = await secrets.lookup(self.jid, self.account.option("store_password"))
            except secrets.KeyringUnavailable as e:
                self._set_state("error", error=_("The keyring is not available: %s") % e, kind="keyring")
                return
        if not self.password:
            self._set_state("error", error=_("Enter the password to connect"), kind="password-needed")
            return
        self._schedule(0)

    def _schedule(self, delay: float) -> None:
        if self._retry_handle is not None:
            self._retry_handle.cancel()
            self._retry_handle = None
        if not self.wanted:
            return
        loop = aio.get_loop()

        def go():
            self._retry_handle = None
            if self._connect_task is None or self._connect_task.done():
                self._connect_task = aio.spawn(self._connect(), name="connect %s" % self.jid)

        if delay <= 0:
            # never inline: we may be called from inside the connect task that is about to finish
            self._retry_handle = loop.call_soon(go)
        else:
            self._retry_handle = loop.call_later(delay, go)
            self.account.step = "waiting"

    def retry_now(self) -> None:
        """The network came back or the machine woke up: skip the backoff."""
        if self.wanted and self.account.state not in ("online", "connecting"):
            self._attempt = 0
            self._schedule(0)

    def _backoff(self) -> float:
        cap = float(self.setting("advanced.reconnect_cap", 5)) * 60
        base = min(cap, 2.0 ** min(self._attempt, 12))
        self._attempt += 1
        return random.uniform(base / 2, base)

    async def _connect(self) -> None:
        self._set_state("connecting", "resolving")
        self._cert_error = None
        if self.xmpp is None:
            self.xmpp = self._make_client()
        else:
            self.xmpp.password = self.password or ""
            if hasattr(self.xmpp, "credentials"):
                self.xmpp.credentials["password"] = self.password or ""
        opts = self.account.options
        prox = proxymod.from_options({**opts, "proxy": self.account.option("proxy")}, "")
        if prox == "system":
            prox = await proxymod.system_proxy("xmpp://" + self.domain)
        tor = self.account.option("proxy") == "tor"
        try:
            if tor or isinstance(prox, proxymod.Proxy) and not self.account.option("host"):
                # DNS through a proxy would leak where we connect; use the standard ports on the domain
                cands = [resolver.Candidate(self.domain, 5222, False), resolver.Candidate(self.domain, 5223, True)]
            else:
                cands = await resolver.candidates(self.domain, self.account.option("host"),
                                                  int(self.account.option("port") or 0),
                                                  self.account.option("tls"))
        except Exception as e:
            cands = []
            self._last_error = str(e)
        if not cands:
            self._failed(_("This server does not offer XMPP"), "network")
            return
        errors = []
        for cand in cands:
            if not self.wanted:
                return
            self._candidate = cand
            self._direct = cand.direct_tls
            self.account.step = "connecting"
            try:
                await self._open(cand, prox if isinstance(prox, proxymod.Proxy) else None)
            except ssl.SSLCertVerificationError as e:
                await self._certificate_failed(cand, prox, _ssl_error_text(e))
                return
            except (OSError, asyncio.TimeoutError, ssl.SSLError, proxymod.ProxyError) as e:
                log.info("%s: %s failed: %s", self.jid, cand, e)
                errors.append("%s: %s" % (cand.host, e.strerror if isinstance(e, OSError) and e.strerror else
                                          (str(e) or type(e).__name__)))
                continue
            loop = aio.get_loop()
            self._negotiation_timer = loop.call_later(NEGOTIATION_TIMEOUT, self._negotiation_timeout)
            return
        self._failed(_("Could not reach the server (%s)") % "; ".join(errors[:2]), "network")

    async def _open(self, cand: resolver.Candidate, prox: proxymod.Proxy | None) -> None:
        x = self.xmpp
        loop = asyncio.get_running_loop()
        # What XMLStream.connect() would have prepared:
        run_filters = getattr(x, "_run_out_filters", None)
        if run_filters is None or run_filters.done():
            x._run_out_filters = asyncio.ensure_future(x.run_filters(), loop=loop)
        x.disconnect_reason = None
        x.default_domain = self.domain
        x.event_when_connected = "connected"
        x._current_connection_attempt = loop.create_future()   # slixmpp >= 1.9 checks it in connection_made
        if hasattr(x, "use_ssl"):
            x.use_ssl = cand.direct_tls
        if hasattr(x, "force_starttls"):
            x.force_starttls = not cand.direct_tls
            x.disable_starttls = False
        if hasattr(x, "enable_direct_tls"):
            x.enable_direct_tls = cand.direct_tls
            x.enable_starttls = not cand.direct_tls
        verify = self._pinned_fp is None
        x.ssl_context = base_ssl_context(verify)
        cert = self.account.option("client_cert")
        if cert and os.path.isfile(cert):
            with contextlib.suppress(OSError, ssl.SSLError):
                x.ssl_context.load_cert_chain(cert)
        x.event("connecting")
        ctx = x.ssl_context if cand.direct_tls else None
        sock = await proxymod.open_socket(prox, cand.host, cand.port) if prox else None
        kwargs = {"sock": sock} if sock else {"host": cand.host, "port": cand.port}
        if ctx is not None:
            kwargs.update(ssl=ctx, server_hostname=self.domain)
        family = self.setting("advanced.ip_preference", "auto")
        if not sock and family in ("ipv4", "ipv6"):
            import socket as _s

            kwargs["family"] = _s.AF_INET if family == "ipv4" else _s.AF_INET6
        await asyncio.wait_for(loop.create_connection(lambda: x, **kwargs), CONNECT_TIMEOUT)

    def _negotiation_timeout(self) -> None:
        self._negotiation_timer = None
        if self.account.state == "connecting":
            log.info("%s: negotiation timed out", self.jid)
            self._last_error = _("The server did not answer")
            if self.xmpp is not None:
                self.xmpp.abort()

    def _cancel_negotiation_timer(self) -> None:
        if self._negotiation_timer is not None:
            self._negotiation_timer.cancel()
            self._negotiation_timer = None

    def _failed(self, message: str, kind: str, retry: bool = True) -> None:
        self._cancel_negotiation_timer()
        self._set_state("error", error=message, kind=kind)
        if retry and self.wanted:
            delay = self._backoff()
            log.info("%s: %s; retrying in %.0fs", self.jid, message, delay)
            self._schedule(delay)

    async def _certificate_failed(self, cand, prox, error: str) -> None:
        """Verification failed: fetch the certificate; connect anyway if the user pinned it before."""
        self._cancel_negotiation_timer()
        try:
            factory = (lambda: proxymod.open_socket(prox, cand.host, cand.port)) if isinstance(
                prox, proxymod.Proxy) else None
            der = await probe_certificate(cand.host, cand.port, cand.direct_tls, self.domain, factory)
        except Exception as e:
            self._failed(_("The server’s certificate is not trusted (%s)") % error, "certificate", retry=False)
            log.info("%s: could not fetch certificate: %s", self.jid, e)
            return
        info = cert_info_from_der(der, cand.host, cand.port, self.domain, error)
        pins = {p.get("sha256") for p in (self.account.option("pinned_certs") or []) if isinstance(p, dict)}
        if info.sha256 in pins and self._pinned_fp is None:
            log.info("%s: certificate is pinned, connecting with it", self.jid)
            self._pinned_fp = info.sha256
            self._schedule(0)
            return
        self._cert_error = info
        self._failed(_("The server’s certificate is not trusted: %s") % error, "certificate", retry=False)
        self.emit("cert-problem", info)

    def _on_ssl_cert(self, pem: str) -> None:
        if self._pinned_fp is None:
            return
        der = ssl.PEM_cert_to_DER_cert(pem)
        if hashlib.sha256(der).hexdigest() != self._pinned_fp:
            log.warning("%s: certificate changed since it was pinned", self.jid)
            self._pinned_fp = None
            self.xmpp.abort()

    def _on_invalid_chain(self, exc) -> None:
        # STARTTLS verification failed; slixmpp leaves the connection to us when this event is handled
        if self.xmpp is not None:
            self.xmpp.abort()
        cand = self._candidate
        if cand is None:
            return
        self._cert_error = CertInfo(cand.host, cand.port, self.domain, "", error=_ssl_error_text(exc))
        aio.spawn(self._certificate_failed(cand, None, _ssl_error_text(exc)))

    def _slixmpp_reconnect(self, wait: float = 2.0, reason: str = "Reconnecting") -> None:
        log.info("%s: reconnecting (%s)", self.jid, reason)
        self._last_error = reason
        if self.xmpp is not None:
            self.xmpp.abort()

    # ================================================================== events
    def _on_session_start(self, _event=None) -> None:
        self._cancel_negotiation_timer()
        self._attempt = 0
        self._pinned_fp = self._pinned_fp  # keep using the pin for this connection only
        self.bound_jid = str(self.xmpp.boundjid)
        self._set_state("online", "syncing")
        self._online_event.set()
        self.emit("online", False)

    def _on_session_resumed(self, _event=None) -> None:
        self._cancel_negotiation_timer()
        self._attempt = 0
        self._set_state("online", "syncing")
        self._online_event.set()
        self.emit("online", True)

    def _on_disconnected(self, reason=None) -> None:
        self._cancel_negotiation_timer()
        self._online_event.clear()
        was_online = self.account.state == "online"
        if self.account.state in ("error",):
            pass
        elif not self.wanted:
            self._set_state("disabled" if not self.account.enabled else "offline")
        else:
            msg = self._last_error or (str(reason) if reason else "") or _("Disconnected")
            self._last_error = ""
            self._failed(msg, "network")
        self._pinned_fp = None
        if was_online:
            self.emit("offline")

    def _on_failed_auth(self, stanza=None) -> None:
        cond = ""
        try:
            for child in stanza.xml:
                cond = child.tag.split("}")[1]
                break
        except Exception:
            pass
        self._auth_condition = cond

    def _on_failed_all_auth(self, _event=None) -> None:
        cond = getattr(self, "_auth_condition", "")
        if cond == "account-disabled":
            text = _("This account is disabled on the server")
        elif cond in ("not-authorized", "credentials-expired", ""):
            text = _("Wrong address or password")
        else:
            text = _("Signing in failed (%s)") % cond
        self.wanted = False
        self._failed(text, "auth", retry=False)
        if self.xmpp is not None:
            self.xmpp.abort()

    def _on_stream_error(self, stanza) -> None:
        cond = ""
        try:
            cond = stanza["condition"]
        except Exception:
            pass
        if cond == "conflict":
            self._last_error = _("Signed in somewhere else with the same resource")
        elif cond:
            self._last_error = _("The server closed the connection (%s)") % cond

    def _on_acked(self, stanza) -> None:
        try:
            if stanza.xml.tag == "{%s}message" % NS_CLIENT and stanza["id"]:
                self.emit("acked", stanza["id"])
        except Exception:
            pass

    def _on_message(self, stanza) -> None:
        self.emit("message", stanza.xml)

    def _on_presence(self, stanza) -> None:
        self.emit("presence", stanza.xml)

    def _on_iq(self, stanza, cb) -> None:
        if stanza["type"] != "set":
            return
        try:
            cb(self, stanza)
        except Exception:
            log.exception("iq handler failed")
            self.reply_iq(stanza, error="internal-server-error")

    def _console_in(self, stanza):
        if self.console:
            with contextlib.suppress(Exception):
                self.emit("xml", True, wardlog.mask(str(stanza)))
        return stanza

    def _console_out(self, stanza):
        if self.console:
            with contextlib.suppress(Exception):
                self.emit("xml", False, wardlog.mask(str(stanza)))
        return stanza

    # ================================================================== stopping
    async def stop(self, unavailable: bool = True) -> None:
        self.wanted = False
        if self._retry_handle is not None:
            self._retry_handle.cancel()
            self._retry_handle = None
        if self._connect_task is not None and not self._connect_task.done():
            self._connect_task.cancel()
        x = self.xmpp
        if x is not None and self.account.state in ("online", "connecting", "error"):
            if self.online and unavailable:
                with contextlib.suppress(Exception):
                    x.send_presence(ptype="unavailable")
            with contextlib.suppress(Exception):
                fut = x.disconnect(1.0)
                if asyncio.isfuture(fut) or asyncio.iscoroutine(fut):
                    await asyncio.wait_for(fut, 3)
        self._cancel_negotiation_timer()
        if x is not None:
            task = getattr(x, "_run_out_filters", None)
            if task is not None and not task.done():
                task.cancel()
        self._set_state("disabled" if not self.account.enabled else "offline")

    def trust_certificate(self, info: CertInfo) -> None:
        """Pin a certificate the user accepted and reconnect with it."""
        pins = [p for p in (self.account.option("pinned_certs") or []) if isinstance(p, dict)]
        pins.append({"sha256": info.sha256, "domain": info.domain, "subject": info.subject,
                     "pinned_at": int(time.time())})
        self.account.options["pinned_certs"] = pins
        self._pinned_fp = info.sha256
        self._cert_error = None
        self.wanted = True
        self._attempt = 0
        self._schedule(0)

    async def wait_online(self, timeout: float = 30) -> None:
        await asyncio.wait_for(self._online_event.wait(), timeout)

    # ================================================================== sending
    def _need(self):
        if self.xmpp is None or not self.online:
            raise NotConnected(self.jid)
        return self.xmpp

    def send(self, el: ET.Element) -> None:
        """Send a stanza built as an ElementTree element (message or presence)."""
        x = self._need()
        tag = el.tag.split("}")[1]
        stanza = {"message": x.Message, "presence": x.Presence, "iq": x.Iq}[tag]()
        for k, v in el.attrib.items():
            stanza.xml.set(k, v)
        for child in list(el):
            stanza.xml.append(child)
        if el.text:
            stanza.xml.text = el.text
        stanza.send()

    def new_message(self, to: str, type_: str = "chat", msg_id: str | None = None) -> ET.Element:
        el = ET.Element("{%s}message" % NS_CLIENT, {"to": to, "type": type_, "id": msg_id or util.random_id(16)})
        return el

    async def iq(self, type_: str, to: str | None, *payload: ET.Element, timeout: float = 30) -> ET.Element:
        """Send an iq and return the result element; raise IqFailed with the error condition otherwise."""
        from slixmpp.exceptions import IqError, IqTimeout

        x = self._need()
        iq = x.Iq()
        iq["type"] = type_
        if to:
            iq["to"] = to
        for p in payload:
            iq.xml.append(p)
        try:
            res = await iq.send(timeout=timeout)
        except IqError as e:
            err = e.iq.xml.find("{%s}error" % NS_CLIENT)
            cond, text = parse_error(err) if err is not None else ("undefined-condition", "")
            raise IqFailed(cond, text, e.iq.xml) from None
        except IqTimeout:
            raise IqFailed("timeout", _("The server did not answer in time")) from None
        return res.xml

    def reply_iq(self, iq, payload: ET.Element | None = None, error: str | None = None) -> None:
        reply = iq.reply()
        if error:
            reply["type"] = "error"
            err = ET.SubElement(reply.xml, "{%s}error" % NS_CLIENT, {"type": "cancel"})
            ET.SubElement(err, "{%s}%s" % (NS_STANZAS, error))
        elif payload is not None:
            reply.xml.append(payload)
        reply.send()

    def send_presence(self, show: str = "available", status: str = "", priority: int | None = None,
                      to: str | None = None, ptype: str | None = None, extra: list[ET.Element] | None = None) -> None:
        x = self._need()
        p = x.Presence()
        if to:
            p["to"] = to
        if ptype:
            p["type"] = ptype
        elif show in ("away", "dnd", "xa", "chat"):
            p["show"] = show
        if status:
            p["status"] = status
        if priority is None:
            priority = int(self.account.option("priority") or 0)
        if not ptype:
            p["priority"] = priority
            if self.idle_since and self.account.option("send_idle"):
                ET.SubElement(p.xml, "{%s}idle" % NS_IDLE, {"since": _stamp(self.idle_since)})
        for e in extra or []:
            p.xml.append(e)
        p.send()

    def set_inactive(self, inactive: bool) -> None:
        """Client State Indication (XEP-0352): the window is hidden, hold back non-urgent traffic."""
        self.inactive = inactive
        if self.online and self.csi_available:
            with contextlib.suppress(Exception):
                if inactive:
                    self.xmpp.plugin["xep_0352"].send_inactive()
                else:
                    self.xmpp.plugin["xep_0352"].send_active()

    def enable_keepalive(self) -> None:
        if self.xmpp is None:
            return
        with contextlib.suppress(Exception):
            self.xmpp.plugin["xep_0199"].enable_keepalive(interval=int(self.setting("advanced.keepalive", 60)), timeout=30)

    def add_feature(self, namespace: str) -> None:
        if self.xmpp is None:
            self.xmpp = self._make_client()
        with contextlib.suppress(Exception):
            self.xmpp.plugin["xep_0030"].add_feature(namespace)

    @property
    def sm_enabled(self) -> bool:
        try:
            return bool(self.xmpp.plugin["xep_0198"].enabled)
        except Exception:
            return False


def _stamp(ts: float) -> str:
    from .stanzas import format_stamp

    return format_stamp(ts)
