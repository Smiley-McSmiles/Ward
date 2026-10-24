"""Details of a chat: the contact (name, sharing, devices, blocking) or the group (topic, nickname, notifications,
settings for owners, members and bans)."""

from __future__ import annotations

from gi.repository import Adw, Gtk

from ..core.models import ChatItem
from ..i18n import _
from . import compat, widgets
from .dialogs import BaseDialog, online_session

NOTIFY = (("default", "Default"), ("all", "All messages"), ("mentions", "Mentions and replies"), ("none", "None"))
ENCRYPTION = (("default", "Default (OMEMO)"), ("omemo2", "OMEMO 2"), ("pgp", "GPG/PGP"), ("off", "Off"))


def DetailsDialog(window, chat: ChatItem, page: str = ""):
    """The right dialog for a chat; page='devices' goes straight to the encryption keys."""
    from .trust import DevicesDialog

    acc = window.core.accounts.get(chat.account_id)
    if page == "devices":
        if chat.type == "groupchat":
            return GroupDevicesDialog(window, chat)
        return DevicesDialog(window, acc, chat.jid)
    if chat.type == "groupchat":
        return GroupDetails(window, chat)
    return ContactDetails(window, chat)


def _combo(title: str, choices, current: str, on_change) -> Adw.ComboRow:
    row = Adw.ComboRow(title=title)
    row.set_model(Gtk.StringList.new([_(label) for _v, label in choices]))
    values = [v for v, _l in choices]
    row.set_selected(values.index(current) if current in values else 0)
    row.connect("notify::selected", lambda r, _p: on_change(values[r.get_selected()]))
    return row


def _header(name: str, sub: str, avatar: str, show=None) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.CENTER, margin_bottom=6)
    av = widgets.Avatar(96, name, avatar, show)
    av.set_halign(Gtk.Align.CENTER)
    box.append(av)
    box.append(Gtk.Label(label=name, css_classes=["title-2"], wrap=True, justify=Gtk.Justification.CENTER))
    lbl = Gtk.Label(label=sub, css_classes=["dim-label"], selectable=True, wrap=True,
                    justify=Gtk.Justification.CENTER, can_focus=False)
    box.append(lbl)
    return box


class ContactDetails(BaseDialog):
    def __init__(self, window, chat: ChatItem):
        super().__init__(window, _("Contact"), height=680)
        self.chat = chat
        self.acc = self.core.accounts.get(chat.account_id)
        c = self.contact = self.core.contacts.get(chat.account_id, chat.jid)
        g = self.group()
        g.add(_header(chat.name or chat.jid, chat.jid, chat.avatar, chat.show))
        actions = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        copy = Gtk.Button(label=_("Copy address"), css_classes=["pill"])
        copy.connect("clicked", lambda b: widgets.copy_text(b, chat.jid))
        actions.append(copy)
        g.add(actions)

        info = self.group()
        if c is not None:
            presence = Adw.ActionRow(title=_("Status"), subtitle=widgets.escape(
                widgets.presence_text(c.show, c.status) if c.show != "offline" else
                (_("Last seen %s") % widgets.relative_seen(c.last_seen) if c.last_seen else _("Offline"))))
            presence.add_css_class("property")
            info.add(presence)
        name = Adw.EntryRow(title=_("Name"), text=(c.roster_name if c else "") or "", show_apply_button=True)
        name.connect("apply", lambda e: self._rename(e.get_text().strip()))
        info.add(name)
        if c is not None and c.in_roster:
            groups = Adw.EntryRow(title=_("Groups (separated by commas)"), text=", ".join(c.groups),
                                  show_apply_button=True)
            groups.connect("apply", lambda e: self._set_groups(e.get_text()))
            info.add(groups)
        self.software = Adw.ActionRow(title=_("App"), subtitle="…")
        self.software.add_css_class("property")
        self.software.set_visible(False)
        info.add(self.software)

        sharing = self.group(_("Sharing status"))
        if c is not None:
            mine = Adw.SwitchRow(title=_("They see when you are online"), active=c.sees_us)
            mine.connect("notify::active", lambda r, _p: self._share(share_mine=r.get_active()))
            sharing.add(mine)
            theirs = Adw.SwitchRow(title=_("You see when they are online"), active=c.we_see or c.ask == "subscribe",
                                   subtitle=_("Waiting for them to accept") if c.ask == "subscribe" and not c.we_see
                                   else "")
            theirs.connect("notify::active", lambda r, _p: self._share(see_theirs=r.get_active()))
            sharing.add(theirs)
        else:
            add = compat.button_row(_("Add to contacts"), "contact-new-symbolic")
            compat.row_activated(add, self._add_contact)
            sharing.add(add)

        sec = self.group(_("Privacy and encryption"))
        enc_choices = [x for x in ENCRYPTION
                       if (x[0] != "pgp" or (self.core.pgp is not None and self.core.pgp.enabled))
                       and (x[0] != "omemo2" or (self.core.omemo is not None and self.core.omemo.available))]
        current = "default" if chat.encryption in ("", "default", "omemo", "omemo-legacy") else chat.encryption
        if current not in [v for v, _l in enc_choices]:
            current = "default"
        sec.add(_combo(_("Encryption"), enc_choices, current, self._set_encryption))
        dev = Adw.ActionRow(title=_("Devices and keys"), activatable=True,
                            subtitle=_("Fingerprints and verification"))
        dev.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        dev.connect("activated", lambda *_: DetailsDialog(window, chat, "devices").present(self.toasts))
        sec.add(dev)
        sec.add(_combo(_("Notifications"), NOTIFY, chat.notify or "default", self._set_notify))

        danger = self.group()
        blocked = c is not None and c.blocked
        block = Adw.ActionRow(title=_("Unblock") if blocked else _("Block…"), activatable=True)
        if not blocked:
            block.add_css_class("error")
        block.connect("activated", lambda *_: self._block(not blocked))
        danger.add(block)
        clear = Adw.ActionRow(title=_("Clear history…"), activatable=True)
        clear.connect("activated", lambda *_: window.conversation._clear_history())
        danger.add(clear)
        forget = Adw.ActionRow(title=_("Forget encryption keys…"), activatable=True)
        forget.add_css_class("error")
        forget.connect("activated", lambda *_: self._forget_keys())
        danger.add(forget)
        if c is not None and c.in_roster:
            rm = Adw.ActionRow(title=_("Remove from contacts…"), activatable=True)
            rm.add_css_class("error")
            rm.connect("activated", lambda *_: self._remove())
            danger.add(rm)
        widgets.run(self.toasts, self._load_software(), on_error=lambda _e: None)

    async def _load_software(self) -> None:
        s = self.core.sessions.get(self.chat.account_id)
        c = self.contact
        if s is None or not s.online or c is None or not c.resources:
            return
        parts = []
        for res in list(c.resources)[:4]:
            sw = await self.core.disco.software(s, "%s/%s" % (c.jid, res))
            if sw:
                name, version, os_ = sw
                parts.append(" ".join(x for x in (name, version) if x) + (" (%s)" % os_ if os_ else ""))
        if parts:
            self.software.set_subtitle(widgets.escape(", ".join(parts)))
            self.software.set_visible(True)

    def _rename(self, name: str) -> None:
        async def go():
            s = online_session(self.core, self.acc)
            c = self.contact or await self.core.contacts.ensure(self.acc.id, self.chat.jid)
            await self.core.roster.rename(s, c, name)
            self.chat.name = self.core.names.contact_name(self.acc.id, self.chat.jid)
            await self.core.chats.save(self.chat, "name")
        self.run(go(), lambda _r: self.toasts.add_toast(Adw.Toast(title=_("Saved"))))

    def _set_groups(self, text: str) -> None:
        groups = [g.strip() for g in text.split(",") if g.strip()]

        async def go():
            await self.core.roster.set_groups(online_session(self.core, self.acc), self.contact, groups)
        self.run(go())

    def _share(self, share_mine=None, see_theirs=None) -> None:
        try:
            s = online_session(self.core, self.acc)
        except RuntimeError as e:
            self.error(e)
            return
        self.core.roster.set_sharing(s, self.chat.jid, share_mine, see_theirs)

    def _add_contact(self) -> None:
        async def go():
            await self.core.roster.add_contact(online_session(self.core, self.acc), self.chat.jid)
        self.run(go(), lambda _r: self.toasts.add_toast(Adw.Toast(title=_("Request sent"))))

    def _set_encryption(self, value: str) -> None:
        async def go():
            await self.core.chats.set_flags(self.chat, encryption=value)
            self.core.e2e.refresh(self.chat)
        self.run(go())

    def _set_notify(self, value: str) -> None:
        self.run(self.core.chats.set_flags(self.chat, notify=value))

    def _block(self, block: bool) -> None:
        jid = self.chat.jid

        async def go():
            s = online_session(self.core, self.acc)
            if block:
                await self.core.roster.block(s, jid)
            else:
                await self.core.roster.unblock(s, jid)
        if not block:
            self.run(go(), lambda _r: self.close())
            return
        compat.alert(self.toasts, _("Block %s?") % jid, _("You won't get messages, calls or requests from them, and "
                                                          "they don't see your status."),
                     [("cancel", _("Cancel"), ""), ("block", _("Block"), "destructive")],
                     lambda r: r == "block" and self.run(go(), lambda _r: self.close()))

    def _forget_keys(self) -> None:
        jid = self.chat.jid

        async def go():
            session = online_session(self.core, self.acc)
            n = await self.core.omemo.forget_contact(session, jid) if self.core.omemo is not None else 0
            if self.core.pgp is not None:
                n += await self.core.pgp.forget_contact(self.acc.id, jid)
            return n
        compat.alert(self.toasts, _("Forget encryption keys?"),
                     _("The local OMEMO devices, trust decisions and sessions for %s are deleted. The contact's "
                       "current device list is fetched again the next time you write to them.") % jid,
                     [("cancel", _("Cancel"), ""), ("forget", _("Forget"), "destructive")],
                     lambda r: r == "forget" and self.run(go(), lambda _n: self.close()))

    def _remove(self) -> None:
        jid = self.chat.jid
        check = Gtk.CheckButton(label=_("Also forget the encryption keys for this contact"))
        box = Gtk.ListBox(css_classes=["boxed-list"])
        box.append(check)

        async def go():
            session = online_session(self.core, self.acc)
            await self.core.roster.remove_contact(session, jid)
            if check.get_active() and self.core.omemo is not None:
                await self.core.omemo.forget_contact(session, jid)
                if self.core.pgp is not None:
                    await self.core.pgp.forget_contact(self.acc.id, jid)
        compat.alert(self.toasts, _("Remove %s from your contacts?") % jid,
                     _("You stop sharing your status with each other. The chat stays."),
                     [("cancel", _("Cancel"), ""), ("remove", _("Remove"), "destructive")],
                     lambda r: r == "remove" and self.run(go(), lambda _r: self.close()), extra=box)


class GroupDetails(BaseDialog):
    def __init__(self, window, chat: ChatItem):
        super().__init__(window, _("Group"), height=700)
        self.chat = chat
        self.acc = self.core.accounts.get(chat.account_id)
        room = self.core.muc.room_of(chat)
        g = self.group()
        g.add(_header(chat.name or chat.jid, chat.jid, chat.avatar))
        copy = Gtk.Button(label=_("Copy invite link"), css_classes=["pill"], halign=Gtk.Align.CENTER)
        copy.connect("clicked", lambda b: widgets.copy_text(b, "xmpp:%s?join" % chat.jid))
        g.add(copy)

        info = self.group()
        can_topic = chat.our_role == "moderator" or (room is not None and room.info is not None and
                                                    room.info.value("muc#roominfo_changesubject") in ("1", "true"))
        topic = Adw.EntryRow(title=_("Topic"), text=chat.subject or "", show_apply_button=True,
                             editable=bool(can_topic) and chat.joined)
        topic.connect("apply", lambda e: self.run(self.core.muc.set_subject(chat, e.get_text())))
        info.add(topic)
        nick = Adw.EntryRow(title=_("Your nickname here"), text=chat.our_nick or "", show_apply_button=True)
        nick.connect("apply", lambda e: self.run(self.core.muc.change_nick(chat, e.get_text().strip())))
        info.add(nick)
        kind = []
        if room is not None:
            kind.append(_("members only") if room.members_only else _("open to everyone"))
            kind.append(_("addresses visible") if room.non_anonymous else _("addresses hidden"))
            if room.mam:
                kind.append(_("history on the server"))
        about = Adw.ActionRow(title=_("Kind"), subtitle=", ".join(kind) or "…")
        about.add_css_class("property")
        info.add(about)
        roles = []
        if chat.our_affiliation not in ("", "none"):
            roles.append(_(chat.our_affiliation.title()))
        if chat.our_role:
            roles.append(_(chat.our_role.title()))
        if roles:
            you = Adw.ActionRow(title=_("You are"), subtitle=", ".join(roles))
            you.add_css_class("property")
            info.add(you)

        prefs = self.group(_("Settings"))
        prefs.add(_combo(_("Notifications"), NOTIFY, chat.notify or "default",
                         lambda v: self.run(self.core.chats.set_flags(chat, notify=v))))
        enc = Adw.ActionRow(title=_("Encryption"), subtitle=chat.encryption_reason or (
            _("End-to-end encrypted") if chat.encryption_state in ("omemo", "omemo-legacy") else _("Not encrypted")))
        prefs.add(enc)
        self.autojoin = Adw.SwitchRow(title=_("Join automatically"), subtitle=_("Bookmarked on your server"),
                                      active=bool(chat.options.get("autojoin", True)))
        self.autojoin.connect("notify::active", lambda r, _p: self._bookmark(r.get_active()))
        prefs.add(self.autojoin)
        if chat.encryptable:
            dev = Adw.ActionRow(title=_("Members' devices"), activatable=True)
            dev.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            dev.connect("activated", lambda *_: GroupDevicesDialog(window, chat).present(self.toasts))
            prefs.add(dev)

        if chat.our_affiliation in ("owner", "admin"):
            admin = self.group(_("Administration"))
            if chat.our_affiliation == "owner":
                conf = Adw.ActionRow(title=_("Group settings…"), activatable=True,
                                     subtitle=_("Name, description, who can join, history"))
                conf.connect("activated", lambda *_: RoomConfigDialog(window, chat).present(self.toasts))
                admin.add(conf)
            members = Adw.ActionRow(title=_("Members and bans…"), activatable=True)
            members.connect("activated", lambda *_: AffiliationsDialog(window, chat).present(self.toasts))
            admin.add(members)

        danger = self.group()
        leave = Adw.ActionRow(title=_("Leave group"), activatable=True)
        leave.add_css_class("error")
        leave.connect("activated", lambda *_: (self.close(), window.conversation._leave()))
        danger.add(leave)
        if chat.our_affiliation == "owner":
            destroy = Adw.ActionRow(title=_("Delete group for everyone…"), activatable=True)
            destroy.add_css_class("error")
            destroy.connect("activated", lambda *_: self._destroy())
            danger.add(destroy)

    def _bookmark(self, autojoin: bool) -> None:
        chat = self.chat

        async def go():
            chat.options["autojoin"] = autojoin
            await self.core.chats.save(chat, "options")
            s = online_session(self.core, self.acc)
            await self.core.muc.set_bookmark(s, chat, autojoin=autojoin)
        self.run(go())

    def _destroy(self) -> None:
        chat = self.chat
        entry = Gtk.Entry(placeholder_text=_("Reason (optional)"))

        def go(resp):
            if resp == "destroy":
                self.run(self.core.muc.destroy(chat, entry.get_text()), lambda _r: (self.close(),
                                                                                     self.window.show_chat(None)))
        compat.alert(self.toasts, _("Delete %s?") % (chat.name or chat.jid),
                     _("The group and its history are deleted on the server for everyone. This cannot be undone."),
                     [("cancel", _("Cancel"), ""), ("destroy", _("Delete"), "destructive")], go, extra=entry)


class RoomConfigDialog(BaseDialog):
    def __init__(self, window, chat: ChatItem):
        super().__init__(window, _("Group settings"), _("Save"), width=560, height=680)
        self.chat = chat
        self.form_view = None
        self.page_group = self.group()
        self.page_group.add(compat.spinner())
        self.run(self._load())

    async def _load(self) -> None:
        from .forms_view import FormView

        s = online_session(self.core, self.core.accounts.get(self.chat.account_id))
        form = await self.core.muc.get_config(s, self.chat.jid)
        self.page.remove(self.page_group)
        self.form_view = FormView(form)
        g = self.group()
        g.add(self.form_view)

    def on_action(self) -> None:
        if self.form_view is None:
            return

        async def go():
            s = online_session(self.core, self.core.accounts.get(self.chat.account_id))
            await self.core.muc.submit_config(s, self.chat.jid, self.form_view.collect())
        self.run(go(), lambda _r: (self.close(), compat.toast(self.window, _("Group settings saved"))))


class AffiliationsDialog(BaseDialog):
    def __init__(self, window, chat: ChatItem):
        super().__init__(window, _("Members and bans"), width=520, height=640)
        self.chat = chat
        add = self.group(_("Add"))
        self.jid = Adw.EntryRow(title=_("Address"))
        add.add(self.jid)
        self.aff = Adw.ComboRow(title=_("As"))
        self.affs = ["member", "admin", "owner", "outcast"]
        self.aff.set_model(Gtk.StringList.new([_("Member"), _("Admin"), _("Owner"), _("Banned")]))
        add.add(self.aff)
        btn = compat.button_row(_("Add"), "list-add-symbolic")
        compat.row_activated(btn, self._add)
        add.add(btn)
        self.groups: dict[str, Adw.PreferencesGroup] = {}
        for aff, title in (("owner", _("Owners")), ("admin", _("Admins")), ("member", _("Members")),
                           ("outcast", _("Banned"))):
            self.groups[aff] = self.group(title)
        self._rows: list[tuple[Adw.PreferencesGroup, Gtk.Widget]] = []
        self.refresh()

    def refresh(self) -> None:
        async def go():
            out = {}
            for aff in self.groups:
                try:
                    out[aff] = await self.core.muc.affiliation_list(self.chat, aff)
                except Exception:
                    out[aff] = None
            return out

        def done(lists):
            for g, r in self._rows:
                g.remove(r)
            self._rows = []
            for aff, items in lists.items():
                g = self.groups[aff]
                if items is None:
                    g.set_description(_("You can't see this list"))
                    continue
                for jid, nick in items:
                    row = Adw.ActionRow(title=jid, subtitle=widgets.escape(nick))
                    rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                    tooltip_text=_("Remove"))
                    rm.connect("clicked", lambda *_a, j=jid: self.run(self.core.muc.set_affiliation(
                        self.chat, j, "none"), lambda _r: self.refresh()))
                    row.add_suffix(rm)
                    g.add(row)
                    self._rows.append((g, row))
        self.run(go(), done)

    def _add(self) -> None:
        jid = self.jid.get_text().strip()
        if not jid:
            return
        aff = self.affs[self.aff.get_selected()]
        self.run(self.core.muc.set_affiliation(self.chat, jid, aff), lambda _r: (self.jid.set_text(""),
                                                                                 self.refresh()))


class GroupDevicesDialog(BaseDialog):
    """Encrypted groups: every member's devices, one member at a time."""

    def __init__(self, window, chat: ChatItem):
        super().__init__(window, _("Members' devices"), height=560)
        self.chat = chat
        acc = self.core.accounts.get(chat.account_id)
        g = self.group(description=_("Messages in this group are encrypted for each member's trusted devices"))
        members = sorted(self.core.muc.encryption_recipients(chat))
        for jid in members:
            row = Adw.ActionRow(title=widgets.escape(self.core.names.contact_name(acc.id, jid)), subtitle=jid,
                                activatable=True)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r, j=jid: self._open(acc, j))
            g.add(row)
        if not members:
            g.add(Adw.ActionRow(title=_("No members with visible addresses")))

    def _open(self, acc, jid: str) -> None:
        from .trust import DevicesDialog

        DevicesDialog(self.window, acc, jid).present(self.toasts)

