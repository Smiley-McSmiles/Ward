"""Display names: who is this JID, nickname or occupant, in plain words."""

from __future__ import annotations

from .. import util
from ..i18n import _


class Names:
    def __init__(self, core):
        self.core = core

    def contact_name(self, account_id: int, jid: str) -> str:
        acc = self.core.accounts.get(account_id)
        if acc is not None and util.bare(jid) == acc.jid:
            return _("Note to self")
        c = self.core.roster.contacts.get(account_id, jid)
        if c is not None:
            return c.name
        return util.localpart(jid) or jid

    def own_name(self, account_id: int) -> str:
        acc = self.core.accounts.get(account_id)
        if acc is None:
            return _("You")
        return acc.nickname or util.localpart(acc.jid)

    def sender_name(self, chat, msg) -> str:
        if chat.type == "groupchat":
            return msg.sender or _("Someone")
        if msg.outgoing:
            return self.own_name(chat.account_id)
        if chat.type == "pm":
            return util.resource(chat.jid)
        return self.contact_name(chat.account_id, msg.sender or chat.jid)

    def reactor_name(self, chat, sender: str) -> str:
        acc = self.core.accounts.get(chat.account_id)
        if acc is not None and sender == acc.jid:
            return _("You")
        if chat.type == "groupchat":
            occ = self.core.muc.occupant_by_id(chat, sender)
            return occ.nick if occ is not None else sender
        return self.contact_name(chat.account_id, sender)
