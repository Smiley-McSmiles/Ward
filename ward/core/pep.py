"""Personal eventing (XEP-0163): PEP notifications arrive as messages with a pubsub#event; services register a
handler per node.  Registering also announces "+notify" interest in our capabilities, so the server sends us
updates for that node from our contacts (avatars, nicknames, OMEMO device lists, read markers...)."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from xml.etree import ElementTree as ET

from .. import util
from . import forms

log = logging.getLogger("ward.pep")

NS_PUBSUB = "http://jabber.org/protocol/pubsub"
NS_EVENT = "http://jabber.org/protocol/pubsub#event"
NS_OWNER = "http://jabber.org/protocol/pubsub#owner"

Handler = Callable[..., object]   # handler(session, from_bare, items: list[ET.Element], retracted: list[str])


class Pep:
    def __init__(self, core):
        self.core = core
        self.handlers: dict[str, Handler] = {}
        self._interests: set[str] = set()

    def register(self, node: str, handler: Handler, notify: bool = True) -> None:
        self.handlers[node] = handler
        if notify:
            self._interests.add(node)

    def attach(self, session) -> None:
        if session.xmpp is None:
            session.xmpp = session._make_client()
        for node in sorted(self._interests):
            with contextlib.suppress(Exception):
                session.xmpp.plugin["xep_0163"].add_interest(node)

    @staticmethod
    def event_of(el: ET.Element) -> ET.Element | None:
        return el.find("{%s}event" % NS_EVENT)

    async def handle(self, session, el: ET.Element) -> bool:
        """Returns True when the message was a pubsub event (handled or not)."""
        ev = self.event_of(el)
        if ev is None:
            return False
        frm = util.bare(el.get("from") or session.jid)
        for items in ev.findall("{%s}items" % NS_EVENT):
            node = items.get("node", "")
            h = self.handlers.get(node)
            if h is None:
                continue
            got = items.findall("{%s}item" % NS_EVENT)
            retracted = [r.get("id", "") for r in items.findall("{%s}retract" % NS_EVENT)]
            try:
                res = h(session, frm, got, retracted)
                if hasattr(res, "__await__"):
                    await res
            except Exception:
                log.exception("PEP handler for %s failed", node)
        return True

    # ------------------------------------------------------------------ requests
    @staticmethod
    async def get_items(session, jid: str | None, node: str, item_ids: list[str] | None = None,
                        max_items: int | None = None) -> list[ET.Element]:
        ps = ET.Element("{%s}pubsub" % NS_PUBSUB)
        items = ET.SubElement(ps, "{%s}items" % NS_PUBSUB, {"node": node})
        if max_items:
            items.set("max_items", str(max_items))
        for i in item_ids or []:
            ET.SubElement(items, "{%s}item" % NS_PUBSUB, {"id": i})
        res = await session.iq("get", jid, ps)
        found = res.find("{%s}pubsub/{%s}items" % (NS_PUBSUB, NS_PUBSUB))
        return found.findall("{%s}item" % NS_PUBSUB) if found is not None else []

    @staticmethod
    async def publish(session, node: str, payload: ET.Element, item_id: str | None = None,
                      options: dict | None = None) -> None:
        ps = ET.Element("{%s}pubsub" % NS_PUBSUB)
        pub = ET.SubElement(ps, "{%s}publish" % NS_PUBSUB, {"node": node})
        item = ET.SubElement(pub, "{%s}item" % NS_PUBSUB, {"id": item_id} if item_id else {})
        item.append(payload)
        if options:
            po = ET.SubElement(ps, "{%s}publish-options" % NS_PUBSUB)
            po.append(forms.simple("http://jabber.org/protocol/pubsub#publish-options", options))
        try:
            await session.iq("set", None, ps)
        except Exception as e:
            cond = getattr(e, "condition", "")
            if options and cond in ("conflict", "precondition-not-met", "not-acceptable"):
                # the node exists with other settings: reconfigure it, then publish again
                await Pep.configure(session, node, options)
                opts_el = ps.find("{%s}publish-options" % NS_PUBSUB)
                if opts_el is not None:
                    ps.remove(opts_el)
                await session.iq("set", None, ps)
            else:
                raise

    @staticmethod
    async def configure(session, node: str, options: dict) -> None:
        ps = ET.Element("{%s}pubsub" % NS_OWNER)
        cfg = ET.SubElement(ps, "{%s}configure" % NS_OWNER, {"node": node})
        cfg.append(forms.simple("http://jabber.org/protocol/pubsub#node_config", options))
        await session.iq("set", None, ps)

    @staticmethod
    async def retract(session, node: str, item_id: str, notify: bool = True) -> None:
        ps = ET.Element("{%s}pubsub" % NS_PUBSUB)
        r = ET.SubElement(ps, "{%s}retract" % NS_PUBSUB, {"node": node, "notify": "true" if notify else "false"})
        ET.SubElement(r, "{%s}item" % NS_PUBSUB, {"id": item_id})
        await session.iq("set", None, ps)

    @staticmethod
    async def delete_node(session, node: str) -> None:
        ps = ET.Element("{%s}pubsub" % NS_OWNER)
        ET.SubElement(ps, "{%s}delete" % NS_OWNER, {"node": node})
        await session.iq("set", None, ps)

    @staticmethod
    async def nodes(session, jid: str | None = None) -> list[tuple[str, str]]:
        """(node, name) of an entity's PEP nodes (PEP browser)."""
        q = ET.Element("{http://jabber.org/protocol/disco#items}query")
        res = await session.iq("get", jid or session.jid, q)
        return [(i.get("node", ""), i.get("name", "")) for i in res.iter("{http://jabber.org/protocol/disco#items}item")
                if i.get("node")]
