"""Ad-hoc commands (XEP-0050) and server administration (XEP-0133).

After login we list the server's commands; if admin commands are offered to us, the account is an admin and the
interface shows the Server section.  Each page only offers what the server actually lists, so Prosody and ejabberd
both work without special cases.  Any command on any entity (bots, gateways, components) runs through the same
generic CommandSession: forms in, forms or notes out, with prev/next/complete/cancel."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from . import forms

log = logging.getLogger("ward.admin")

NS_COMMANDS = "http://jabber.org/protocol/commands"
ADMIN = "http://jabber.org/protocol/admin#"

ADMIN_NODES = {
    "add-user": ADMIN + "add-user", "delete-user": ADMIN + "delete-user", "disable-user": ADMIN + "disable-user",
    "reenable-user": ADMIN + "reenable-user", "end-user-session": ADMIN + "end-user-session",
    "change-user-password": ADMIN + "change-user-password", "get-user-password": ADMIN + "get-user-password",
    "get-user-lastlogin": ADMIN + "get-user-lastlogin", "user-stats": ADMIN + "user-stats",
    "get-registered-users-num": ADMIN + "get-registered-users-num",
    "get-disabled-users-num": ADMIN + "get-disabled-users-num",
    "get-online-users-num": ADMIN + "get-online-users-num", "get-active-users-num": ADMIN + "get-active-users-num",
    "get-registered-users-list": ADMIN + "get-registered-users-list",
    "get-online-users-list": ADMIN + "get-online-users-list", "get-online-users": ADMIN + "get-online-users",
    "announce": ADMIN + "announce", "set-motd": ADMIN + "set-motd", "edit-motd": ADMIN + "edit-motd",
    "delete-motd": ADMIN + "delete-motd", "set-welcome": ADMIN + "set-welcome",
    "restart": ADMIN + "restart", "shutdown": ADMIN + "shutdown",
    # Prosody extras
    "reload": "http://prosody.im/protocol/modules#reload", "load": "http://prosody.im/protocol/modules#load",
    "unload": "http://prosody.im/protocol/modules#unload", "list-modules": "http://prosody.im/protocol/modules#list",
    "config-reload": "http://prosody.im/protocol/config#reload",
    "invite": "urn:xmpp:invite#invite", "create-account": "urn:xmpp:invite#create-account",
    "uptime": "http://prosody.im/protocol/uptime",
}


@dataclass
class Note:
    type: str          # info | warn | error
    text: str


@dataclass
class CommandStep:
    jid: str
    node: str
    sessionid: str = ""
    status: str = "executing"      # executing | completed | canceled
    form: forms.Form | None = None
    notes: list[Note] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)   # prev | next | complete
    default_action: str = ""

    @property
    def finished(self) -> bool:
        return self.status in ("completed", "canceled")


def parse_command(jid: str, node: str, res: ET.Element) -> CommandStep:
    cmd = res.find("{%s}command" % NS_COMMANDS)
    step = CommandStep(jid, node)
    if cmd is None:
        step.status = "completed"
        return step
    step.sessionid = cmd.get("sessionid", "")
    step.status = cmd.get("status", "executing")
    for n in cmd.findall("{%s}note" % NS_COMMANDS):
        step.notes.append(Note(n.get("type", "info"), n.text or ""))
    acts = cmd.find("{%s}actions" % NS_COMMANDS)
    if acts is not None:
        step.default_action = acts.get("execute", "")
        step.actions = [a.tag.split("}")[1] for a in acts]
        if step.default_action and step.default_action not in step.actions:
            step.actions.append(step.default_action)
    step.form = forms.find(cmd, res)
    return step


class AdminService:
    def __init__(self, core):
        self.core = core
        self.commands: dict[int, dict[str, str]] = {}     # account id -> node -> name (server commands)

    async def list_commands(self, session, jid: str | None = None) -> list[tuple[str, str, str]]:
        """(jid, node, name) of the commands an entity offers us."""
        q = ET.Element("{http://jabber.org/protocol/disco#items}query", {"node": NS_COMMANDS})
        res = await session.iq("get", jid or session.domain, q)
        return [(i.get("jid", ""), i.get("node", ""), i.get("name", "") or i.get("node", ""))
                for i in res.iter("{http://jabber.org/protocol/disco#items}item")]

    async def on_online(self, session, resumed: bool) -> None:
        if resumed:
            return
        try:
            cmds = await self.list_commands(session)
        except Exception:
            cmds = []
        self.commands[session.account.id] = {node: name for _j, node, name in cmds}
        admin_nodes = {v for v in ADMIN_NODES.values() if v.startswith(ADMIN)}
        session.account.is_admin = any(node in admin_nodes for node in self.commands[session.account.id])

    def offers(self, account_id: int, key: str) -> bool:
        return ADMIN_NODES.get(key, key) in self.commands.get(account_id, {})

    async def execute(self, session, jid: str, node: str) -> CommandStep:
        c = ET.Element("{%s}command" % NS_COMMANDS, {"node": node, "action": "execute"})
        res = await session.iq("set", jid, c, timeout=60)
        return parse_command(jid, node, res)

    async def advance(self, session, step: CommandStep, action: str = "", form: forms.Form | None = None
                      ) -> CommandStep:
        action = action or step.default_action or "complete"
        attrs = {"node": step.node, "sessionid": step.sessionid}
        if action != "execute":
            attrs["action"] = action
        c = ET.Element("{%s}command" % NS_COMMANDS, attrs)
        if form is not None and action != "cancel":
            c.append(forms.to_element(form, "submit"))
        res = await session.iq("set", step.jid, c, timeout=60)
        return parse_command(step.jid, step.node, res)

    async def cancel(self, session, step: CommandStep) -> None:
        if step.finished or not step.sessionid:
            return
        try:
            await self.advance(session, step, "cancel")
        except Exception:
            pass

    async def run(self, session, key: str, values: dict, jid: str | None = None) -> CommandStep:
        """Run a one-form admin command with values: execute, fill in, complete."""
        node = ADMIN_NODES.get(key, key)
        step = await self.execute(session, jid or session.domain, node)
        if step.finished or step.form is None:
            return step
        f = step.form
        for k, v in values.items():
            f.set(k, v)
        return await self.advance(session, step, "complete" if "complete" in step.actions or not step.actions
                                  else step.default_action or "next", f)

    async def stats(self, session) -> dict[str, str]:
        """Overview numbers that the server offers (registered, online, active users)."""
        out = {}
        for key in ("get-registered-users-num", "get-online-users-num", "get-active-users-num"):
            if not self.offers(session.account.id, key):
                continue
            try:
                step = await self.execute(session, session.domain, ADMIN_NODES[key])
                if step.form is not None:
                    for f in step.form.fields:
                        if f.var and f.var != "FORM_TYPE" and f.values:
                            out[f.var] = f.value
            except Exception as e:
                log.debug("%s failed: %s", key, e)
        return out

    async def users(self, session) -> list[str]:
        key = "get-registered-users-list"
        if not self.offers(session.account.id, key):
            return []
        step = await self.run(session, key, {"max_items": "all"})
        if step.form is None:
            return []
        out = []
        for f in step.form.fields:
            if f.type == "jid-multi" or f.var.endswith("registereduserjids") or f.var.endswith("users"):
                out.extend(f.values)
        return sorted(set(out))

    async def create_invite(self, session, contact_invite: bool = False) -> str:
        """Prosody mod_invites (XEP-0401 links): returns the invite URI or page."""
        key = "invite" if contact_invite else "create-account"
        step = await self.execute(session, session.domain, ADMIN_NODES[key])
        if step.form is not None and step.status != "completed":
            step = await self.advance(session, step, "complete", step.form)
        if step.form is not None:
            for var in ("landing-url", "uri", "url"):
                v = step.form.value(var)
                if v:
                    return v
        for n in step.notes:
            if "xmpp:" in n.text or "https://" in n.text:
                return n.text.strip()
        raise RuntimeError("the server did not return an invite")
