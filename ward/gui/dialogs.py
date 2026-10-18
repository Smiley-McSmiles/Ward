"""Dialogs: starting chats, contacts and groups, the quick switcher, message details, images, passwords,
certificates, requests, invitations and the profile."""

from __future__ import annotations

import logging
import shutil

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import util
from ..core import register, secrets
from ..core.models import AccountItem, AttachmentItem, ChatItem, MessageItem
from ..i18n import _
from . import compat, lifetime, widgets

log = logging.getLogger("ward.gui")


# ================================================================================================ base
class BaseDialog:
    """A dialog with Cancel and one main action in the header, preference groups as content and its own toasts."""

    def __init__(self, window, title: str, action: str = "", width: int = 480, height: int = -1,
                 destructive: bool = False):
        self.window = window
        self.core = window.core
        self.dialog = compat.Dialog(title, width, height)
        self.toasts = Adw.ToastOverlay()
        self.view = Adw.ToolbarView()
        self.header = Adw.HeaderBar()
        self.view.add_top_bar(self.header)
        self.action_button = None
        if action:
            self.header.set_show_start_title_buttons(False)
            self.header.set_show_end_title_buttons(False)
            cancel = Gtk.Button(label=_("Cancel"))
            cancel.connect("clicked", lambda *_: self.close())
            self.header.pack_start(cancel)
            self.action_button = Gtk.Button(label=action, css_classes=["destructive-action" if destructive
                                                                      else "suggested-action"])
            self.action_button.connect("clicked", lambda *_: self.on_action())
            self.header.pack_end(self.action_button)
            self.dialog.set_default_widget(self.action_button)
        self.page = Adw.PreferencesPage()
        self.view.set_content(self.page)
        self.toasts.set_child(self.view)
        self.dialog.set_child(self.toasts)
        self.spinner = None
        lifetime.forget_when_closed(self, self.dialog.widget)

    def group(self, title: str = "", description: str = "") -> Adw.PreferencesGroup:
        g = Adw.PreferencesGroup(title=title, description=description)
        self.page.add(g)
        return g

    def present(self, parent: Gtk.Widget) -> None:
        self.dialog.present(parent)

    def close(self) -> None:
        self.dialog.close()

    def on_action(self) -> None:
        pass

    def error(self, text) -> None:
        self.busy(False)
        self.toasts.add_toast(Adw.Toast(title=str(text), timeout=6))

    def busy(self, on: bool) -> None:
        if self.action_button is None:
            return
        self.action_button.set_sensitive(not on)
        if on and self.spinner is None:
            self.spinner = compat.spinner()
            self.header.pack_end(self.spinner)
        elif not on and self.spinner is not None:
            self.header.remove(self.spinner)
            self.spinner = None

    def run(self, coro, done=None):
        self.busy(True)

        def ok(res):
            self.busy(False)
            if done is not None:
                done(res)
        return widgets.run(self.toasts, coro, done=ok, on_error=self.error)


def entry_row(title: str, text: str = "", password: bool = False) -> Adw.EntryRow:
    row = Adw.PasswordEntryRow(title=title) if password else Adw.EntryRow(title=title)
    row.set_text(text)
    return row


class AccountChooser:
    """A combo row with the enabled accounts, hidden when there is only one."""

    def __init__(self, core, group: Adw.PreferencesGroup, preselect: AccountItem | None = None):
        self.accounts = [a for a in core.accounts.list() if a.enabled] or core.accounts.list()
        self.row = Adw.ComboRow(title=_("Account"))
        self.row.set_model(Gtk.StringList.new([a.display_name for a in self.accounts]))
        if preselect in self.accounts:
            self.row.set_selected(self.accounts.index(preselect))
        self.row.set_visible(len(self.accounts) > 1)
        group.add(self.row)

    @property
    def account(self) -> AccountItem | None:
        i = self.row.get_selected()
        return self.accounts[i] if 0 <= i < len(self.accounts) else None


def online_session(core, acc: AccountItem):
    s = core.sessions.get(acc.id) if acc is not None else None
    if s is None or not s.online:
        raise RuntimeError(_("%s is not connected") % (acc.jid if acc else _("The account")))
    return s


# ================================================================================================ start chat
class StartChatDialog(BaseDialog):
    def __init__(self, window, jid: str = ""):
        super().__init__(window, _("New chat"), _("Chat"))
        g = self.group(description=_("Enter an address like alex@example.org, or pick a contact"))
        self.acc = AccountChooser(self.core, g, window.sidebar._main_account())
        self.address = entry_row(_("Address"), jid)
        self.address.connect("changed", lambda *_: self._suggest())
        self.address.connect("entry-activated", lambda *_: self.on_action())
        g.add(self.address)
        self.suggestions = self.group(_("Contacts"))
        self._rows: list[Gtk.Widget] = []
        self._suggest()

    def _suggest(self) -> None:
        for r in self._rows:
            self.suggestions.remove(r)
        self._rows = []
        text = self.address.get_text().strip().lower()
        acc = self.acc.account
        items = [c for c in self.core.contacts.for_account(acc.id) if c.in_roster] if acc else []
        if text:
            items = [c for c in items if text in c.name.lower() or text in c.jid.lower()]
        items.sort(key=lambda c: (c.show == "offline", c.name.lower()))
        for c in items[:8]:
            row = Adw.ActionRow(title=widgets.escape(c.name), subtitle=c.jid, activatable=True)
            row.add_prefix(widgets.RowAvatar(32, c.name, c.avatar, c.show))
            row.connect("activated", lambda _r, c=c: self._open(c.jid))
            self.suggestions.add(row)
            self._rows.append(row)
        self.suggestions.set_visible(bool(self._rows))

    def on_action(self) -> None:
        jid = self.address.get_text().strip()
        if jid.lower().startswith("xmpp:"):
            self.close()
            self.window.app.open_uri(jid)
            return
        if not util.valid_bare_jid(jid):
            self.error(_("“%s” is not a valid address") % jid)
            return
        self._open(jid)

    def _open(self, jid: str) -> None:
        acc = self.acc.account

        async def go():
            s = self.core.sessions.get(acc.id)
            if s is not None and s.online:
                try:
                    info = await self.core.disco.info(s, jid)
                    if info.has_identity("conference"):
                        return await self.core.muc.join(acc, jid)
                except Exception:
                    pass
            return await self.core.open_chat(acc, jid)

        def done(chat):
            self.close()
            self.window.show_chat(chat)
        self.run(go(), done)


class AddContactDialog(BaseDialog):
    def __init__(self, window, jid: str = "", name: str = ""):
        super().__init__(window, _("Add contact"), _("Add"))
        g = self.group(description=_("They get a request to share their status with you"))
        self.acc = AccountChooser(self.core, g, window.sidebar._main_account())
        self.address = entry_row(_("Address"), jid)
        g.add(self.address)
        self.name = entry_row(_("Name (optional)"), name)
        g.add(self.name)
        self.message = entry_row(_("Message (optional)"))
        g.add(self.message)

    def on_action(self) -> None:
        jid = self.address.get_text().strip()
        if not util.valid_bare_jid(jid):
            self.error(_("“%s” is not a valid address") % jid)
            return
        acc = self.acc.account

        async def go():
            s = online_session(self.core, acc)
            await self.core.roster.add_contact(s, jid, self.name.get_text().strip(), message=self.message.get_text())
            return await self.core.open_chat(acc, jid)

        def done(chat):
            self.close()
            self.window.show_chat(chat)
            compat.toast(self.window, _("Request sent to %s") % jid)
        self.run(go(), done)


# ================================================================================================ groups
class JoinGroupDialog(BaseDialog):
    def __init__(self, window, address: str = "", password: str = "", acc: AccountItem | None = None):
        super().__init__(window, _("Join group or channel"), _("Join"), height=560)
        g = self.group(description=_("Enter a group's address, or search public channels"))
        self.acc = AccountChooser(self.core, g, acc or window.sidebar._main_account())
        self.address = entry_row(_("Address or search words"), address)
        self.address.connect("entry-activated", lambda *_: self.on_action())
        search = Gtk.Button(icon_name="system-search-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                            tooltip_text=_("Search public channels"))
        search.connect("clicked", lambda *_: self._search())
        self.address.add_suffix(search)
        g.add(self.address)
        a = self.acc.account
        self.nick = entry_row(_("Your nickname"), (a.nickname or util.localpart(a.jid)) if a else "")
        g.add(self.nick)
        self.password = entry_row(_("Password (if the group has one)"), password, password=True)
        g.add(self.password)
        self.results = self.group(_("Public channels"))
        self.results.set_visible(False)
        self._rows: list[Gtk.Widget] = []

    def _search(self) -> None:
        text = self.address.get_text().strip()
        if not text:
            return
        acc = self.acc.account

        async def go():
            s = online_session(self.core, acc)
            service = self.core.disco.channel_search_service(s)
            return await (self.core.muc.search_channels(s, text, service) if service
                          else self.core.muc.search_channels(s, text))

        def done(items):
            for r in self._rows:
                self.results.remove(r)
            self._rows = []
            for it in items[:30]:
                sub = it["jid"] + (" · %d" % it["users"] if it["users"] else "")
                row = Adw.ActionRow(title=widgets.escape(it["name"] or it["jid"]), subtitle=widgets.escape(sub),
                                    activatable=True)
                if it["description"]:
                    row.set_tooltip_text(it["description"])
                row.connect("activated", lambda _r, j=it["jid"]: (self.address.set_text(j), self.on_action()))
                self.results.add(row)
                self._rows.append(row)
            if not items:
                row = Adw.ActionRow(title=_("No channels found"))
                self.results.add(row)
                self._rows.append(row)
            self.results.set_visible(True)
        self.run(go(), done)

    def on_action(self) -> None:
        jid = self.address.get_text().strip()
        if jid.lower().startswith("xmpp:"):
            from ..core import uri

            u = uri.parse_xmpp(jid)
            jid = u.jid if u else jid
        if not util.valid_bare_jid(jid):
            self._search()
            return
        acc = self.acc.account
        nick = self.nick.get_text().strip() or None
        pw = self.password.get_text() or None

        def done(chat):
            self.close()
            self.window.show_chat(chat)
        self.run(self.core.muc.join(acc, jid, nick, pw), done)


class NewGroupDialog(BaseDialog):
    def __init__(self, window):
        super().__init__(window, _("New group"), _("Create"), height=560)
        g = self.group()
        self.acc = AccountChooser(self.core, g, window.sidebar._main_account())
        self.name = entry_row(_("Name"))
        g.add(self.name)
        self.kind = Adw.ComboRow(title=_("Kind"))
        self.kind.set_model(Gtk.StringList.new([_("Private group"), _("Public channel")]))
        self.kind.connect("notify::selected", lambda *_: self._update_kind())
        g.add(self.kind)
        self.description = entry_row(_("Description (optional)"))
        g.add(self.description)
        self.invitees = entry_row(_("Invite (addresses, separated by commas)"))
        g.add(self.invitees)
        adv = self.group(_("Advanced"))
        self.address = entry_row(_("Address (optional)"))
        adv.add(self.address)
        self._update_kind()

    def _update_kind(self) -> None:
        if self.kind.get_selected() == 0:
            self.kind.set_subtitle(_("Only invited members; messages can be end-to-end encrypted"))
        else:
            self.kind.set_subtitle(_("Anyone can find and join; not encrypted"))

    def on_action(self) -> None:
        name = self.name.get_text().strip()
        if not name:
            self.error(_("Give the group a name"))
            return
        acc = self.acc.account
        preset = "private" if self.kind.get_selected() == 0 else "public"
        invitees = [j.strip() for j in self.invitees.get_text().replace(";", ",").split(",") if j.strip()]
        bad = [j for j in invitees if not util.valid_bare_jid(j)]
        if bad:
            self.error(_("“%s” is not a valid address") % bad[0])
            return

        def done(chat):
            self.close()
            self.window.show_chat(chat)
        self.run(self.core.muc.create_group(acc, name, preset, invitees, self.description.get_text().strip(),
                                            listed=preset == "public", address=self.address.get_text().strip()),
                 done)


class InviteDialog(BaseDialog):
    def __init__(self, window, chat: ChatItem):
        super().__init__(window, _("Invite to %s") % (chat.name or chat.jid), _("Invite"))
        self.chat = chat
        g = self.group()
        self.address = entry_row(_("Address"))
        g.add(self.address)
        self.reason = entry_row(_("Message (optional)"))
        g.add(self.reason)
        contacts = self.group(_("Contacts"))
        for c in sorted([c for c in self.core.contacts.for_account(chat.account_id) if c.in_roster],
                        key=lambda c: c.name.lower())[:30]:
            row = Adw.ActionRow(title=widgets.escape(c.name), subtitle=c.jid, activatable=True)
            row.add_prefix(widgets.RowAvatar(32, c.name, c.avatar, c.show))
            row.connect("activated", lambda _r, j=c.jid: self.address.set_text(j))
            contacts.add(row)
        link = self.group(_("Link"))
        lrow = Adw.ActionRow(title="xmpp:%s?join" % chat.jid, subtitle=_("Anyone with this link can ask to join"))
        if hasattr(lrow, "set_subtitle_selectable"):
            lrow.set_subtitle_selectable(True)
        copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
        copy.connect("clicked", lambda b: widgets.copy_text(b, "xmpp:%s?join" % chat.jid))
        lrow.add_suffix(copy)
        link.add(lrow)

    def on_action(self) -> None:
        jid = self.address.get_text().strip()
        if not util.valid_bare_jid(jid):
            self.error(_("“%s” is not a valid address") % jid)
            return

        def done(_r):
            self.close()
            compat.toast(self.window, _("Invited %s") % jid)
        self.run(self.core.muc.invite(self.chat, jid, self.reason.get_text()), done)


class InvitationDialog(BaseDialog):
    def __init__(self, window, acc: AccountItem, inv: dict):
        super().__init__(window, _("Invitation"), _("Join"))
        self.acc = acc
        self.inv = inv
        who = self.core.names.contact_name(acc.id, inv["from"])
        g = self.group(description=_("%(who)s invites you to the group %(room)s") % {"who": who, "room": inv["room"]})
        if inv.get("reason"):
            g.add(Adw.ActionRow(title=widgets.escape(inv["reason"]), subtitle=_("Message")))

    def on_action(self) -> None:
        def done(chat):
            self.close()
            self.window.show_chat(chat)
        self.run(self.core.muc.join(self.acc, self.inv["room"], None, self.inv.get("password") or None), done)


# ================================================================================================ pickers
class AccountPicker(BaseDialog):
    def __init__(self, window, callback):
        super().__init__(window, _("Choose an account"))
        g = self.group()
        for acc in window.core.accounts.list():
            row = Adw.ActionRow(title=widgets.escape(acc.display_name), subtitle=acc.jid, activatable=True)
            row.add_prefix(widgets.RowAvatar(32, acc.display_name, acc.avatar))
            row.connect("activated", lambda _r, a=acc: (self.close(), callback(a)))
            g.add(row)


class ChatPicker(BaseDialog):
    def __init__(self, window, title: str, callback):
        super().__init__(window, title, height=520)
        self.callback = callback
        self.entry = Gtk.SearchEntry(placeholder_text=_("Search"))
        self.entry.connect("search-changed", lambda *_: self._fill())
        g = self.group()
        g.add(self.entry)
        self.list_group = self.group()
        self._rows: list[Gtk.Widget] = []
        self._fill()

    def _fill(self) -> None:
        for r in self._rows:
            self.list_group.remove(r)
        self._rows = []
        text = self.entry.get_text().strip().lower()
        chats = [c for c in self.core.chats.items() if c.open and not c.is_request]
        if text:
            chats = [c for c in chats if text in (c.name or "").lower() or text in c.jid.lower()]
        chats.sort(key=lambda c: -c.last_activity)
        for c in chats[:40]:
            row = Adw.ActionRow(title=widgets.escape(c.name or c.jid), subtitle=c.jid, activatable=True)
            row.add_prefix(widgets.RowAvatar(32, c.name or c.jid, c.avatar))
            row.connect("activated", lambda _r, c=c: (self.close(), self.callback(c)))
            self.list_group.add(row)
            self._rows.append(row)


class QuickSwitcher:
    """Ctrl+K: type a few letters of a chat or contact, press Enter."""

    def __init__(self, window):
        self.window = window
        self.core = window.core
        self.dialog = compat.Dialog(_("Go to"), 460, 460)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12, margin_end=12,
                      margin_top=12, margin_bottom=12)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.entry = Gtk.SearchEntry(placeholder_text=_("Chats and contacts"))
        self.entry.connect("search-changed", lambda *_: self._fill())
        self.entry.connect("activate", lambda *_: self._activate(self.list.get_selected_row()))
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.entry.add_controller(keys)
        box.append(self.entry)
        self.list = Gtk.ListBox(css_classes=["boxed-list"])
        self.list.connect("row-activated", lambda _l, r: self._activate(r))
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(self.list)
        box.append(sc)
        view.set_content(box)
        self.dialog.set_child(view)
        self._fill()

    def _candidates(self):
        seen = set()
        for c in sorted(self.core.chats.items(), key=lambda c: (-c.pinned, -c.last_activity)):
            if c.is_request:
                continue
            seen.add((c.account_id, c.jid))
            yield ("chat", c, c.name or c.jid, c.jid, c.avatar)
        for c in self.core.contacts.store:
            if c.in_roster and (c.account_id, c.jid) not in seen:
                yield ("contact", c, c.name, c.jid, c.avatar)

    def _fill(self) -> None:
        widgets.clear(self.list)
        text = self.entry.get_text().strip().lower()
        n = 0
        for kind, obj, name, jid, avatar in self._candidates():
            if text and text not in name.lower() and text not in jid.lower():
                continue
            row = Adw.ActionRow(title=widgets.escape(name), subtitle=jid, activatable=True)
            row.add_prefix(widgets.RowAvatar(32, name, avatar))
            row.target = (kind, obj)
            self.list.append(row)
            n += 1
            if n >= 30:
                break
        first = self.list.get_row_at_index(0)
        if first is not None:
            self.list.select_row(first)

    def _on_key(self, _c, keyval, _code, _state) -> bool:
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            row = self.list.get_selected_row()
            i = (row.get_index() if row else -1) + (1 if keyval == Gdk.KEY_Down else -1)
            nxt = self.list.get_row_at_index(max(0, i))
            if nxt is not None:
                self.list.select_row(nxt)
            return True
        return False

    def _activate(self, row) -> None:
        if row is None:
            return
        kind, obj = row.target
        self.dialog.close()
        if kind == "chat":
            self.window.show_chat(obj)
            return
        acc = self.core.accounts.get(obj.account_id)
        widgets.run(self.window, self.core.open_chat(acc, obj.jid), done=self.window.show_chat)

    def present(self, parent) -> None:
        self.dialog.present(parent)
        self.entry.grab_focus()


# ================================================================================================ messages
class MessageDetails(BaseDialog):
    def __init__(self, view, m: MessageItem):
        super().__init__(view.window, _("Message details"), height=560)
        self.m = m
        g = self.group()

        def add(title, value, copy=False):
            if not value:
                return
            row = Adw.ActionRow(title=title, subtitle=widgets.escape(str(value)))
            row.add_css_class("property")
            if copy:
                b = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
                b.connect("clicked", lambda btn: widgets.copy_text(btn, str(value)))
                row.add_suffix(b)
            g.add(row)
        dt = GLib.DateTime.new_from_unix_local(m.timestamp // 1000)
        add(_("Sent") if m.outgoing else _("Received"), dt.format("%c") if dt else "")
        add(_("From"), m.sender_jid or m.sender)
        enc = {"omemo2": "OMEMO 2", "omemo": _("OMEMO (legacy)"), "pgp": "OpenPGP"}.get(m.encryption,
                                                                                     _("Not encrypted"))
        add(_("Encryption"), enc)
        if m.sender_device:
            add(_("Device"), str(m.sender_device))
        if m.trust:
            add(_("Device trust"), _(widgets.TRUST.get(m.trust, ("", "", m.trust))[2]))
        status = {-1: _("Failed"), 0: _("Sending"), 1: _("Sent to the server"), 2: _("Delivered"), 3: _("Read")}
        if m.outgoing:
            add(_("Status"), status.get(m.status, ""))
        if m.error:
            add(_("Error"), m.error)
        ids = self.group(_("Identifiers"))
        g = ids
        add(_("Server id"), m.stanza_id, True)
        add(_("Origin id"), m.origin_id, True)
        add(_("Message id"), m.msg_id, True)
        self.receipts = self.group(_("Receipts"))
        self.receipts.set_visible(False)
        widgets.run(self.toasts, self._load_receipts())

    async def _load_receipts(self) -> None:
        rows = await self.core.db.fetchall("SELECT * FROM receipts WHERE message_id = ?", (self.m.id,))
        for r in rows:
            parts = []
            if r["delivered_at"]:
                parts.append(_("delivered %s") % widgets.clock(r["delivered_at"], self.core.settings))
            if r["displayed_at"]:
                parts.append(_("read %s") % widgets.clock(r["displayed_at"], self.core.settings))
            self.receipts.add(Adw.ActionRow(title=widgets.escape(r["recipient"]), subtitle=", ".join(parts)))
        self.receipts.set_visible(bool(rows))


class ImageViewer:
    def __init__(self, view, att: AttachmentItem):
        self.view = view
        self.att = att
        self.dialog = compat.Dialog(att.name or _("Image"), 900, 700)
        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        for icon, tip, cb in (("document-save-symbolic", _("Save as…"), lambda: view.save_attachment(None, att)),
                              ("document-open-symbolic", _("Open with another app"),
                               lambda: widgets.open_file(view, att.local_path)),
                              ("edit-copy-symbolic", _("Copy image"), self._copy)):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.connect("clicked", lambda *_a, cb=cb: cb())
            header.pack_start(b)
        tv.add_top_bar(header)
        pic = Gtk.Picture.new_for_filename(att.local_path)
        pic.set_can_shrink(True)
        pic.set_vexpand(True)
        tv.set_content(pic)
        self.dialog.set_child(tv)

    def _copy(self) -> None:
        tex = widgets.texture(self.att.local_path, 0)      # the whole image
        if tex is not None:
            self.view.get_clipboard().set(tex)
            compat.toast(self.view, _("Copied"))

    def present(self, parent) -> None:
        self.dialog.present(parent)


# ================================================================================================ accounts
class PasswordDialog(BaseDialog):
    def __init__(self, window, acc: AccountItem):
        super().__init__(window, _("Password"), _("Connect"))
        self.acc = acc
        g = self.group(description=_("Enter the password of %s") % acc.jid)
        self.password = entry_row(_("Password"), password=True)
        self.password.connect("entry-activated", lambda *_: self.on_action())
        g.add(self.password)
        self.remember = Adw.SwitchRow(title=_("Remember"), active=acc.option("store_password") != "never")
        g.add(self.remember)
        GLib.idle_add(lambda: self.password.grab_focus() and False)

    def on_action(self) -> None:
        pw = self.password.get_text()
        if not pw:
            return
        self.close()
        widgets.run(self.window, self.core.provide_password(self.acc, pw, self.remember.get_active()),
                    error=_("Could not connect"))


class CertificateDialog(BaseDialog):
    def __init__(self, window, acc: AccountItem, info):
        super().__init__(window, _("Untrusted certificate"), _("Trust and connect"), destructive=True,
                         height=600)
        self.acc = acc
        self.info = info
        g = self.group(description=_("The server %(domain)s showed a certificate that could not be verified: "
                                     "%(error)s.\n\nThis happens with self-hosted servers, but it can also mean "
                                     "someone is intercepting your connection. Only continue if you know the "
                                     "fingerprint is right.") % {"domain": info.domain, "error": info.error or "?"})
        for title, value in ((_("Server"), "%s:%d" % (info.host, info.port)), (_("Issued to"), info.subject),
                             (_("Issued by"), info.issuer), (_("Names"), ", ".join(info.names)),
                             (_("Valid from"), info.not_before), (_("Valid until"), info.not_after)):
            if value:
                row = Adw.ActionRow(title=title, subtitle=widgets.escape(value))
                row.add_css_class("property")
                g.add(row)
        fp = Adw.ActionRow(title=_("SHA-256 fingerprint"), subtitle=info.pretty_fingerprint())
        fp.add_css_class("property")
        fp.set_subtitle_lines(0)
        g.add(fp)

    def on_action(self) -> None:
        s = self.core.session_for(self.acc)
        s.trust_certificate(self.info)
        widgets.run(self.window, self.core.accounts.save_options(self.acc))
        self.close()


class RequestsDialog(BaseDialog):
    def __init__(self, window):
        super().__init__(window, _("Requests"), height=520)
        self.g = self.group(description=_("People who want to share status with you or wrote to you without "
                                          "being a contact"))
        self._rows: list[Gtk.Widget] = []
        self._h = self.core.requests.connect("changed", lambda *_: self._fill())
        self.dialog.on_closed(lambda: self.core.requests.disconnect(self._h))
        self._fill()

    def _fill(self) -> None:
        for r in self._rows:
            self.g.remove(r)
        self._rows = []
        for req in self.core.requests.items():
            acc = self.core.accounts.get(req.account_id)
            if acc is None:
                continue
            sub = req.text or (_("Wants to share status with you") if req.kind == "subscription"
                               else _("Sent you a message"))
            row = Adw.ActionRow(title=req.jid, subtitle=widgets.escape(sub))
            row.add_prefix(widgets.RowAvatar(36, req.jid))
            if req.chat_id:
                show = Gtk.Button(icon_name="ward-chat-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                  tooltip_text=_("Show messages"))
                show.connect("clicked", lambda *_a, r=req: (self.close(), self.window.open_chat_id(r.chat_id)))
                row.add_suffix(show)
            block = Gtk.Button(icon_name="action-unavailable-symbolic", valign=Gtk.Align.CENTER,
                               css_classes=["flat"], tooltip_text=_("Block"))
            block.connect("clicked", lambda *_a, a=acc, r=req: widgets.run(
                self.toasts, self.core.decline_request(a, r.jid, block=True)))
            row.add_suffix(block)
            decline = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                 tooltip_text=_("Decline"))
            decline.connect("clicked", lambda *_a, a=acc, r=req: widgets.run(
                self.toasts, self.core.decline_request(a, r.jid)))
            row.add_suffix(decline)
            accept = Gtk.Button(icon_name="object-select-symbolic", valign=Gtk.Align.CENTER,
                                css_classes=["suggested-action", "circular"], tooltip_text=_("Accept"))
            accept.connect("clicked", lambda *_a, a=acc, r=req: widgets.run(
                self.toasts, self.core.accept_request(a, r.jid)))
            row.add_suffix(accept)
            self.g.add(row)
            self._rows.append(row)
        if not self._rows:
            row = Adw.ActionRow(title=_("No requests"))
            self.g.add(row)
            self._rows.append(row)


class ProfileDialog(BaseDialog):
    def __init__(self, window, acc: AccountItem):
        super().__init__(window, _("Profile"), height=640)
        self.acc = acc
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER)
        self.avatar = Adw.Avatar(size=96, text=acc.display_name, show_initials=True)
        self.avatar.set_custom_image(widgets.texture(acc.avatar))
        top.append(self.avatar)
        change = Gtk.Button(label=_("Change picture…"), css_classes=["pill"])
        change.connect("clicked", lambda *_: self._choose_avatar())
        top.append(change)
        g = self.group()
        g.add(top)
        info = self.group()
        self.nick = Adw.EntryRow(title=_("Name"), text=acc.nickname, show_apply_button=True)
        self.nick.connect("apply", lambda *_: self._set_nick())
        info.add(self.nick)
        addr = Adw.ActionRow(title=_("Address"), subtitle=acc.jid)
        addr.add_css_class("property")
        copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
        copy.connect("clicked", lambda b: widgets.copy_text(b, acc.jid))
        addr.add_suffix(copy)
        info.add(addr)
        share = Adw.ActionRow(title=_("Share your address"), subtitle=_("A link others can open to add you"),
                              activatable=True)
        share.add_suffix(Gtk.Image.new_from_icon_name("ward-qr-symbolic"))
        share.connect("activated", lambda *_: self._share())
        info.add(share)
        sec = self.group(_("Security"))
        dev = Adw.ActionRow(title=_("Your devices and encryption keys"), activatable=True)
        dev.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        dev.connect("activated", lambda *_: self._devices())
        sec.add(dev)
        pw = Adw.ActionRow(title=_("Change password…"), activatable=True)
        pw.connect("activated", lambda *_: ChangePasswordDialog(window, acc).present(self.toasts))
        sec.add(pw)
        danger = self.group()
        rm = Adw.ActionRow(title=_("Remove account from Ward…"), activatable=True)
        rm.add_css_class("error")
        rm.connect("activated", lambda *_: self._remove())
        danger.add(rm)
        dl = Adw.ActionRow(title=_("Delete account on the server…"), activatable=True)
        dl.add_css_class("error")
        dl.connect("activated", lambda *_: DeleteAccountDialog(window, acc, self).present(self.toasts))
        danger.add(dl)

    def _set_nick(self) -> None:
        nick = self.nick.get_text().strip()

        async def go():
            await self.core.accounts.set_option(self.acc, "nickname", nick)
            self.acc.nickname = nick
            s = self.core.sessions.get(self.acc.id)
            if s is not None and s.online:
                await self.core.roster.publish_nick(s, nick)
        self.run(go(), lambda _r: self.toasts.add_toast(Adw.Toast(title=_("Name saved"))))

    def _choose_avatar(self) -> None:
        dialog = Gtk.FileDialog(title=_("Choose a picture"))
        f = Gtk.FileFilter()
        f.set_name(_("Images"))
        f.add_mime_type("image/*")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        dialog.set_filters(store)

        def done(d, res):
            try:
                file = d.open_finish(res)
            except GLib.Error:
                return
            if file is not None and file.get_path():
                self.run(self._publish_avatar(file.get_path()), lambda _r: self.avatar.set_custom_image(
                    widgets.texture(self.acc.avatar)))
        dialog.open(self.window, None, done)

    async def _publish_avatar(self, path: str) -> None:
        from .. import aio

        def scale():
            from gi.repository import GdkPixbuf

            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 256, 256, True)
            _ok, data = pb.save_to_bufferv("png", [], [])
            return bytes(data), pb.get_width(), pb.get_height()
        data, w, h = await aio.run_in_thread(scale)
        s = online_session(self.core, self.acc)
        await self.core.roster.publish_avatar(s, data, "image/png", w, h)

    def _share(self) -> None:
        from .trust import share_dialog

        async def go():
            link = "xmpp:%s" % self.acc.jid
            s = self.core.sessions.get(self.acc.id)
            if self.core.omemo is not None and s is not None and s.online:
                try:
                    link = await self.core.omemo.verification_uri(s)
                except Exception:
                    pass
            return link
        self.run(go(), lambda link: share_dialog(self.toasts, _("Your address"), link,
                                                 _("Scan with Conversations, Monal, Dino or another Ward to add "
                                                   "you and verify your devices")))

    def _devices(self) -> None:
        from .trust import DevicesDialog

        DevicesDialog(self.window, self.acc, self.acc.jid).present(self.toasts)

    def _remove(self) -> None:
        acc = self.acc

        def go(resp):
            if resp == "remove":
                self.close()
                widgets.run(self.window, self.core.remove_account(acc))
        compat.alert(self.toasts, _("Remove %s?") % acc.jid,
                     _("Ward forgets this account and its local history. The account itself keeps existing on the "
                       "server."), [("cancel", _("Cancel"), ""), ("remove", _("Remove"), "destructive")], go)


class ChangePasswordDialog(BaseDialog):
    def __init__(self, window, acc: AccountItem):
        super().__init__(window, _("Change password"), _("Change"))
        self.acc = acc
        g = self.group(description=acc.jid)
        self.new = entry_row(_("New password"), password=True)
        g.add(self.new)
        self.repeat = entry_row(_("Repeat"), password=True)
        g.add(self.repeat)

    def on_action(self) -> None:
        pw = self.new.get_text()
        if len(pw) < 6:
            self.error(_("Use at least 6 characters"))
            return
        if pw != self.repeat.get_text():
            self.error(_("The passwords don't match"))
            return

        async def go():
            s = online_session(self.core, self.acc)
            await register.change_password(s, pw)
            s.password = pw
            mode = self.acc.option("store_password")
            if mode != "never":
                try:
                    await secrets.store(self.acc.jid, pw, mode)
                except secrets.KeyringUnavailable:
                    secrets.remember_for_session(self.acc.jid, pw)
            else:
                secrets.remember_for_session(self.acc.jid, pw)

        def done(_r):
            self.close()
            compat.toast(self.window, _("Password changed"))
        self.run(go(), done)


class DeleteAccountDialog(BaseDialog):
    def __init__(self, window, acc: AccountItem, profile=None):
        super().__init__(window, _("Delete account"), _("Delete"), destructive=True)
        self.acc = acc
        self.profile = profile
        g = self.group(description=_("This deletes %s on the server, with its contacts and stored messages. It "
                                     "cannot be undone. Type the address to confirm.") % acc.jid)
        self.confirm = entry_row(_("Address"))
        g.add(self.confirm)

    def on_action(self) -> None:
        if self.confirm.get_text().strip().lower() != self.acc.jid.lower():
            self.error(_("Type %s to confirm") % self.acc.jid)
            return

        async def go():
            s = online_session(self.core, self.acc)
            await register.delete_account(s)
            await self.core.remove_account(self.acc)

        def done(_r):
            self.close()
            if self.profile is not None:
                self.profile.close()
            compat.toast(self.window, _("Account deleted"))
        self.run(go(), done)


# ================================================================================================ small helpers
def save_file_dialog(parent: Gtk.Widget, source: str, name: str) -> None:
    dialog = Gtk.FileDialog(initial_name=name)

    def done(d, res):
        try:
            f = d.save_finish(res)
        except GLib.Error:
            return
        if f is not None and f.get_path():
            try:
                shutil.copyfile(source, f.get_path())
            except OSError as e:
                compat.toast(parent, str(e))
    root = parent.get_root()
    dialog.save(root if isinstance(root, Gtk.Window) else None, None, done)


def text_view_dialog(parent: Gtk.Widget, title: str, text: str) -> None:
    d = compat.Dialog(title, 560, 480)
    tv = Adw.ToolbarView()
    tv.add_top_bar(Adw.HeaderBar())
    view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=12,
                        bottom_margin=12, left_margin=12, right_margin=12)
    view.get_buffer().set_text(text)
    sc = Gtk.ScrolledWindow(vexpand=True)
    sc.set_child(view)
    tv.set_content(sc)
    d.set_child(tv)
    d.present(parent)



class ExtrasDialog(BaseDialog):
    """Downloads the OMEMO libraries with pip into Ward's own folder, then offers a restart."""

    def __init__(self, window):
        from .. import deps, extras

        super().__init__(window, _("Encryption support"), _("Install"), width=520, height=560)
        g = self.group(description=_("Ward encrypts messages with OMEMO. Your system has no packages for its "
                                     "libraries, so Ward can download them from the Python Package Index into "
                                     "its own folder. Nothing else on your system changes."))
        self.legacy = Adw.SwitchRow(title=_("Legacy OMEMO for today's apps"), active=True,
                                    subtitle=_("Conversations, Monal, Dino, Gajim and Siskin still use it. "
                                               "The library (oldmemo) is licensed under the AGPL-3.0."))
        g.add(self.legacy)
        self.log_view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                     top_margin=6, bottom_margin=6, left_margin=6, right_margin=6)
        sc = Gtk.ScrolledWindow(min_content_height=160, css_classes=["card"])
        sc.set_child(self.log_view)
        self.scroller = sc
        out = self.group(_("Progress"))
        out.add(sc)
        out.set_visible(False)
        self.out_group = out
        self.clean = err = getattr(window.core, "omemo_error", "")
        if err and extras.dirs():
            g.set_description(_("The encryption libraries are installed but do not load:") + "\n" + err + "\n\n" +
                              _("Installing again replaces them with a fresh copy."))
        if not deps.have("pip"):
            g.set_description(g.get_description() + "\n\n" + _("pip is missing: %s") % (
                deps.install_hint("pip") or "python3 -m ensurepip --user"))

    def _line(self, text: str) -> None:
        def add():
            b = self.log_view.get_buffer()
            b.insert(b.get_end_iter(), text + "\n")
            adj = self.scroller.get_vadjustment()
            adj.set_value(adj.get_upper())
            return False
        GLib.idle_add(add)

    def on_action(self) -> None:
        from .. import aio, extras

        if self.action_button.get_label() == _("Restart Ward"):
            self.window.app.restart()
            return
        packages = list(extras.OMEMO) + (list(extras.LEGACY_OMEMO) if self.legacy.get_active() else [])
        self.out_group.set_visible(True)

        async def go():
            await aio.run_in_thread(extras.install, packages, None, self._line, bool(self.clean))

        def done(_r):
            self.action_button.set_label(_("Restart Ward"))
            self.toasts.add_toast(Adw.Toast(title=_("Installed. Restart Ward to turn on encryption.")))
        self.run(go(), done)
