"""End-to-end encryption policy: what happens to a message before it leaves, and after it arrives.

    Always (default)  never send unencrypted; if no device can receive, the message fails with a reason
    When available    encrypt if the contact has devices (or an OpenPGP key), otherwise send plain
    Off               plain

The policy comes from the chat (its encryption override), then the account, then Preferences.  A chat can also
be switched to OpenPGP (XEP-0373) instead of OMEMO.  Group chats can only be encrypted when members-only and
non-anonymous."""

from __future__ import annotations

import logging
from xml.etree import ElementTree as ET

from ..i18n import _
from .chat import EncryptionBlocked, Incoming

log = logging.getLogger("ward.e2e")


class E2E:
    def __init__(self, core):
        self.core = core

    # ------------------------------------------------------------------ policy
    def policy(self, chat) -> str:
        """always | available | off"""
        if chat.encryption == "off":
            return "off"
        acc = self.core.accounts.get(chat.account_id)
        pol = acc.option("encryption") if acc is not None else "default"
        if pol in (None, "", "default"):
            pol = self.core.settings["encryption.default"]
        if chat.encryption in ("omemo", "pgp") and pol == "off":
            return "always"
        return pol

    def method(self, chat) -> str:
        """omemo | pgp | none"""
        if self.policy(chat) == "off":
            return "none"
        if chat.encryption == "pgp":
            return "pgp"
        return "omemo"

    def describe(self, chat) -> tuple[str, str]:
        """(state, reason) for the chat header: what will happen to the next message, in plain words."""
        meth = self.method(chat)
        if meth == "none":
            return "plain", _("Encryption is off for this chat")
        if chat.type == "groupchat" and not chat.encryptable:
            if self.policy(chat) == "always" and chat.encryption in ("omemo", "pgp"):
                return "blocked", _("This group cannot be encrypted: it is public or members can't see each "
                                    "other's addresses")
            return "plain", _("Public groups are not encrypted")
        if meth == "pgp":
            if self.core.pgp is None:
                return "blocked", _("OpenPGP is not available")
            return self.core.pgp.describe(chat)
        omemo = self.core.omemo
        if omemo is None or not omemo.available:
            if self.policy(chat) == "always":
                return "blocked", _("Encryption support (OMEMO) is not installed")
            return "plain", _("Encryption support (OMEMO) is not installed")
        return omemo.describe(chat)

    def refresh(self, chat) -> None:
        state, reason = self.describe(chat)
        chat.update(encryption_state=state, encryption_reason=reason)

    def history_sync(self, session, started: bool) -> None:
        if self.core.omemo is not None:
            self.core.omemo.history_sync(session, started)

    # ------------------------------------------------------------------ outgoing
    async def encrypt(self, session, chat, el: ET.Element) -> tuple[ET.Element, str]:
        """Returns (stanza to send, encryption used: '' | omemo | omemo2 | pgp) or raises EncryptionBlocked."""
        meth = self.method(chat)
        policy = self.policy(chat)
        has_payload = el.find("{jabber:client}body") is not None or any(
            child.tag.endswith("}reactions") or child.tag.endswith("}reply") for child in el)
        if meth == "none" or not has_payload:
            return el, ""
        if chat.type == "groupchat" and not chat.encryptable:
            if policy == "always" and chat.encryption in ("omemo", "pgp"):
                raise EncryptionBlocked(_("This group cannot be encrypted"))
            return el, ""
        if meth == "pgp":
            if self.core.pgp is None:
                raise EncryptionBlocked(_("OpenPGP is not available"))
            return await self.core.pgp.encrypt(session, chat, el), "pgp"
        omemo = self.core.omemo
        usable = omemo is not None and omemo.available
        if usable and not omemo.ready(session):
            try:
                await omemo.manager(session)       # still starting up after login: wait for it
            except Exception as e:
                log.warning("OMEMO is not ready: %s", e)
                usable = False
        if not usable:
            if policy == "always":
                raise EncryptionBlocked(_("Encryption (OMEMO) is not available, so the message was not sent. "
                                          "Install the OMEMO libraries or turn encryption off for this chat."))
            return el, ""
        try:
            return await omemo.encrypt(session, chat, el)
        except EncryptionBlocked:
            if policy == "always":
                raise
            return el, ""

    # ------------------------------------------------------------------ incoming
    async def decrypt(self, session, el: ET.Element, inc: Incoming, is_pm: bool = False) -> None:
        """Replace inc.pm with the decrypted content, or set inc.decrypt_error to a readable reason."""
        pm = inc.pm
        if pm.openpgp is not None:
            if self.core.pgp is None:
                inc.decrypt_error = _("This message is encrypted with OpenPGP, which is not set up here")
                inc.encryption = "pgp"
                return
            await self.core.pgp.decrypt(session, el, inc)
            return
        omemo = self.core.omemo
        if omemo is None or not omemo.available:
            inc.encryption = "omemo2" if pm.omemo2 is not None else "omemo"
            inc.decrypt_error = _("This message is encrypted with OMEMO, which is not installed here")
            return
        await omemo.decrypt(session, el, inc, is_pm=is_pm)
