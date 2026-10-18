"""Service discovery (XEP-0030): what the server, our account and the server's components can do.

After login we learn the server's features (carbons, blocking, CSI...), our account's (MAM, PEP), and find the
components: file upload (XEP-0363, with its size limit), group chats, channel search, proxies.  Results for other
entities are cached by entity-caps hash (XEP-0115) in the database, shared by all accounts."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from . import forms

log = logging.getLogger("ward.disco")

NS_INFO = "http://jabber.org/protocol/disco#info"
NS_ITEMS = "http://jabber.org/protocol/disco#items"
NS_UPLOAD = "urn:xmpp:http:upload:0"
NS_MUC = "http://jabber.org/protocol/muc"
NS_COMMANDS = "http://jabber.org/protocol/commands"
NS_CHANNEL_SEARCH = "urn:xmpp:channel-search:0:search"
NS_SERVERINFO = "http://jabber.org/network/serverinfo"


@dataclass
class Info:
    jid: str
    node: str = ""
    identities: list[tuple[str, str, str]] = field(default_factory=list)   # (category, type, name)
    features: set[str] = field(default_factory=set)
    forms: list[forms.Form] = field(default_factory=list)

    def has_identity(self, category: str, type_: str | None = None) -> bool:
        return any(c == category and (type_ is None or t == type_) for c, t, _n in self.identities)

    def form(self, form_type: str) -> forms.Form | None:
        for f in self.forms:
            if f.form_type == form_type:
                return f
        return None

    @property
    def name(self) -> str:
        for _c, _t, n in self.identities:
            if n:
                return n
        return ""


def parse_info(jid: str, node: str, query: ET.Element | None) -> Info:
    info = Info(jid, node)
    if query is None:
        return info
    for i in query.findall("{%s}identity" % NS_INFO):
        info.identities.append((i.get("category", ""), i.get("type", ""), i.get("name", "")))
    for f in query.findall("{%s}feature" % NS_INFO):
        if f.get("var"):
            info.features.add(f.get("var", ""))
    for x in query.findall("{%s}x" % forms.NS):
        info.forms.append(forms.parse(x))
    return info


class DiscoService:
    def __init__(self, core):
        self.core = core

    async def info(self, session, jid: str | None, node: str = "") -> Info:
        q = ET.Element("{%s}query" % NS_INFO, {"node": node} if node else {})
        res = await session.iq("get", jid, q)
        return parse_info(jid or session.jid, node, res.find("{%s}query" % NS_INFO))

    async def items(self, session, jid: str | None, node: str = "") -> list[tuple[str, str, str]]:
        """(jid, node, name) children of an entity."""
        q = ET.Element("{%s}query" % NS_ITEMS, {"node": node} if node else {})
        res = await session.iq("get", jid, q)
        return [(i.get("jid", ""), i.get("node", ""), i.get("name", "")) for i in res.iter("{%s}item" % NS_ITEMS)]

    async def caps_info(self, session, jid: str, caps_node: str) -> Info | None:
        """Disco info of a contact's resource, through the caps cache."""
        row = await self.core.db.fetchone("SELECT * FROM caps WHERE node = ?", (caps_node,))
        if row is not None:
            info = Info(jid)
            info.identities = [tuple(x) for x in json.loads(row["identities"])]
            info.features = set(json.loads(row["features"]))
            return info
        try:
            info = await self.info(session, jid, caps_node)
        except Exception:
            return None
        await self.core.db.execute("INSERT OR REPLACE INTO caps (node, identities, features, fetched_at) "
                                   "VALUES (?, ?, ?, ?)", (caps_node, json.dumps(info.identities),
                                                           json.dumps(sorted(info.features)), int(time.time())))
        return info

    async def on_online(self, session, resumed: bool) -> None:
        if resumed and session.server_features:
            return
        acc = session.account
        try:
            server = await self.info(session, session.domain)
        except Exception as e:
            log.info("server disco failed: %s", e)
            server = Info(session.domain)
        session.server_features = server.features
        session.server_identities = server.identities
        session.server_info = server
        try:
            mine = await self.info(session, None)
            session.account_features = mine.features
        except Exception:
            session.account_features = set()
        acc.has_mam = NS_MAM_FEATURE in session.account_features
        services: dict[str, dict] = {}
        try:
            children = await self.items(session, session.domain)
        except Exception:
            children = []
        for jid, node, _name in children:
            if node or not jid:
                continue
            try:
                ci = await self.info(session, jid)
            except Exception:
                continue
            services[jid] = {"features": ci.features, "identities": ci.identities, "info": ci}
        session.services = services
        upload = self.upload_service(session)
        acc.can_upload = upload is not None
        acc.upload_limit = upload[1] if upload else 0

    @staticmethod
    def upload_service(session) -> tuple[str, int] | None:
        """(jid, max size in bytes or 0 when unlimited) of the HTTP upload service."""
        candidates = [(session.domain, getattr(session, "server_info", None))]
        candidates += [(jid, s.get("info")) for jid, s in session.services.items()]
        for jid, info in candidates:
            if info is None or NS_UPLOAD not in info.features:
                continue
            limit = 0
            f = info.form(NS_UPLOAD)
            if f is not None:
                try:
                    limit = int(f.value("max-file-size") or 0)
                except ValueError:
                    limit = 0
            return jid, limit
        return None

    @staticmethod
    def muc_service(session) -> str | None:
        for jid, s in session.services.items():
            info = s.get("info")
            if info is not None and NS_MUC in info.features and info.has_identity("conference", "text"):
                return jid
        for jid, s in session.services.items():
            if NS_MUC in s.get("features", set()):
                return jid
        return None

    @staticmethod
    def channel_search_service(session) -> str | None:
        for jid, s in session.services.items():
            if NS_CHANNEL_SEARCH in s.get("features", set()):
                return jid
        return None

    @staticmethod
    def contact_addresses(session) -> dict[str, list[str]]:
        """XEP-0157 server contact information (abuse, admin, support...)."""
        info = getattr(session, "server_info", None)
        out: dict[str, list[str]] = {}
        f = info.form(NS_SERVERINFO) if info is not None else None
        if f is not None:
            for fld in f.fields:
                if fld.var != "FORM_TYPE" and fld.values:
                    out[fld.var] = list(fld.values)
        return out

    async def software(self, session, jid: str) -> tuple[str, str, str] | None:
        """XEP-0092 (name, version, os)."""
        try:
            res = await session.iq("get", jid, ET.Element("{jabber:iq:version}query"))
        except Exception:
            return None
        q = res.find("{jabber:iq:version}query")
        if q is None:
            return None
        return (q.findtext("{jabber:iq:version}name") or "", q.findtext("{jabber:iq:version}version") or "",
                q.findtext("{jabber:iq:version}os") or "")

    async def uptime(self, session, jid: str) -> int | None:
        """XEP-0012 last activity of a server = its uptime in seconds."""
        try:
            res = await session.iq("get", jid, ET.Element("{jabber:iq:last}query"))
        except Exception:
            return None
        q = res.find("{jabber:iq:last}query")
        try:
            return int(q.get("seconds")) if q is not None else None
        except (TypeError, ValueError):
            return None


NS_MAM_FEATURE = "urn:xmpp:mam:2"
