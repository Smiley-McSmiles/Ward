"""The members panel of a group: who is here, grouped by role, with what moderators and owners can do."""

from __future__ import annotations

import weakref

from gi.repository import Gio, GLib, Gtk, Pango

from ..core import colors
from ..core.models import ChatItem, OccupantItem
from ..i18n import _
from . import compat, lifetime, style, widgets

ROLE_TITLES = {"moderator": "Moderators", "participant": "Members", "visitor": "Visitors", "none": "Others"}


class MembersPanel(Gtk.Box):
    __gtype_name__ = "WardMembersPanel"

    def __init__(self, view):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, css_classes=["members-panel"])
        self.view = view
        self.core = view.core
        self.chat: ChatItem | None = None
        self._occupant: OccupantItem | None = None
        self._popover: Gtk.PopoverMenu | None = None
        self._dirty = False
        self._setting_chat = False
        head = Gtk.Box(spacing=6, margin_start=12, margin_end=6, margin_top=8, margin_bottom=4)
        self.count = Gtk.Label(xalign=0, hexpand=True, css_classes=["heading"])
        head.append(self.count)
        self.append(head)
        self.filter = Gtk.SearchEntry(placeholder_text=_("Filter"), margin_start=8, margin_end=8, margin_bottom=4)
        self.filter.connect("search-changed", lambda *_: not self._setting_chat and self.refresh())
        self.append(self.filter)
        self.list = Gtk.ListBox(css_classes=["navigation-sidebar"], selection_mode=Gtk.SelectionMode.NONE)
        self.list.connect("row-activated", self._on_activated)
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(self.list)
        self.append(sc)
        self._refresh_pending = 0
        lifetime.watch(self.core.muc, "occupants-changed", self._on_changed)
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)
        ref = weakref.ref(self)
        lifetime.when_unrooted(self, lambda: (p := ref()) is not None and p._cancel_pending())

        group = Gio.SimpleActionGroup()
        for name, cb in (("message", self._private_message), ("mention", self._mention),
                         ("kick", self._kick), ("ban", self._ban), ("voice", lambda o: self._role(o, "participant")),
                         ("mute", lambda o: self._role(o, "visitor")),
                         ("moderator", lambda o: self._role(o, "moderator")),
                         ("member", lambda o: self._affiliation(o, "member")),
                         ("admin", lambda o: self._affiliation(o, "admin")),
                         ("owner", lambda o: self._affiliation(o, "owner")),
                         ("unmember", lambda o: self._affiliation(o, "none")),
                         ("details", self._details)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, cb=cb: self._occupant is not None and cb(self._occupant))
            group.add_action(a)
        self.insert_action_group("member", group)

    def _cancel_pending(self) -> None:
        if getattr(self, "_refresh_pending", 0):
            GLib.source_remove(self._refresh_pending)
            self._refresh_pending = 0
        self._occupant = None
        pop = getattr(self, "_popover", None)
        if pop is not None:
            self._popover = None
            pop.popdown()
            pop.set_menu_model(None)
            if pop.get_parent() is not None:
                pop.unparent()
            lifetime.disconnect_all(pop)

    def _on_map(self, *_a) -> None:
        if getattr(self, "_dirty", False):
            self.refresh()

    def _on_unmap(self, *_a) -> None:
        self._cancel_pending()
        if hasattr(self, "list"):
            widgets.clear(self.list)
            self._dirty = self.chat is not None

    def set_chat(self, chat: ChatItem | None) -> None:
        self._cancel_pending()
        self.chat = chat
        self._dirty = False
        if self.filter.get_text():
            self._setting_chat = True
            try:
                self.filter.set_text("")
            finally:
                self._setting_chat = False
        self.refresh()

    def _on_changed(self, _muc, chat) -> None:
        if chat is self.chat and not self._refresh_pending:
            # presence floods (joining a big room) arrive in bursts: redraw at most a few times a second
            self._refresh_pending = GLib.timeout_add(250, self._refresh_later)

    def _refresh_later(self) -> bool:
        if not hasattr(self, "list"):
            return False
        self._refresh_pending = 0
        self.refresh()
        return False

    def refresh(self) -> None:
        widgets.clear(self.list)
        chat = self.chat
        if chat is None:
            self._dirty = False
            self.count.set_text("")
            return
        occupants = self.core.muc.sorted_occupants(chat)
        self.count.set_text(_("%d here") % len(occupants))
        if self.get_realized() and not self.get_mapped():
            self._dirty = True
            return
        self._dirty = False
        text = self.filter.get_text().strip().lower()
        if text:
            occupants = [o for o in occupants if text in o.nick.lower() or text in o.real_jid.lower()]
        role = None
        for o in occupants:
            if o.role != role:
                role = o.role
                header = Gtk.ListBoxRow(activatable=False, selectable=False)
                header.set_child(Gtk.Label(label=_(ROLE_TITLES.get(role, "Others")), xalign=0,
                                           css_classes=["role-header"]))
                self.list.append(header)
            self.list.append(self._row(o))

    def _row(self, o: OccupantItem) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.occupant = o
        box = Gtk.Box(spacing=8, margin_start=4, margin_end=4, margin_top=2, margin_bottom=2)
        avatar = widgets.Avatar(28, o.nick, o.avatar, o.show)
        box.append(avatar)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        nick = widgets.escape(o.nick) + ((" " + widgets.escape(_("(you)"))) if o.is_self else "")
        if self.core.settings["appearance.nick_colors"]:
            name.set_markup("<span foreground='%s'>%s</span>" % (colors.hex_color(o.nick, style.is_dark()), nick))
        else:
            name.set_markup(nick)
        texts.append(name)
        if o.status:
            texts.append(Gtk.Label(label=o.status, xalign=0, css_classes=["dim-label", "caption"],
                                   ellipsize=Pango.EllipsizeMode.END))
        box.append(texts)
        if o.affiliation in ("owner", "admin"):
            box.append(Gtk.Label(label=_("Owner") if o.affiliation == "owner" else _("Admin"), css_classes=["hat"],
                                 valign=Gtk.Align.CENTER))
        for hat in [h for h in o.hats.split(",") if h.strip()]:
            box.append(Gtk.Label(label=hat.strip(), css_classes=["hat"], valign=Gtk.Align.CENTER))
        row.set_child(box)
        if o.real_jid:
            row.set_tooltip_text(o.real_jid)
        return row

    def _on_activated(self, _lb, row) -> None:
        o = getattr(row, "occupant", None)
        if o is None:
            return
        self._occupant = o
        chat = self.chat
        mine_role, mine_aff = chat.our_role, chat.our_affiliation
        menu = Gio.Menu()
        sec = Gio.Menu()
        if not o.is_self:
            sec.append(_("Mention"), "member.mention")
            sec.append(_("Message privately"), "member.message")
        if o.real_jid:
            sec.append(_("Details"), "member.details")
        menu.append_section(None, sec)
        sec = Gio.Menu()
        if not o.is_self and mine_role == "moderator" and o.affiliation not in ("owner", "admin"):
            if o.role == "visitor":
                sec.append(_("Allow to write"), "member.voice")
            elif o.role == "participant":
                sec.append(_("Mute"), "member.mute")
                if mine_aff in ("owner", "admin"):
                    sec.append(_("Make moderator"), "member.moderator")
            sec.append(_("Remove from group…"), "member.kick")
        if not o.is_self and o.real_jid and mine_aff in ("owner", "admin"):
            if o.affiliation == "none":
                sec.append(_("Make member"), "member.member")
            elif o.affiliation == "member":
                sec.append(_("Remove membership"), "member.unmember")
            if mine_aff == "owner" and o.affiliation != "admin":
                sec.append(_("Make admin"), "member.admin")
            if mine_aff == "owner" and o.affiliation != "owner":
                sec.append(_("Make owner"), "member.owner")
            if o.affiliation not in ("owner",) or mine_aff == "owner":
                sec.append(_("Ban…"), "member.ban")
        menu.append_section(None, sec)
        if self._popover is not None:
            self._popover.popdown()
        pop = Gtk.PopoverMenu.new_from_model(menu)
        pop.set_parent(row)
        self._popover = pop
        self_ref = weakref.ref(self)

        def on_closed(p: Gtk.PopoverMenu) -> None:
            def cleanup() -> bool:
                panel = self_ref()
                if panel is not None and getattr(panel, "_popover", None) is p:
                    panel._popover = None
                p.set_menu_model(None)
                if p.get_parent() is not None:
                    p.unparent()
                lifetime.disconnect_all(p)
                return False

            GLib.idle_add(cleanup)

        pop.connect("closed", on_closed)
        pop.popup()

    # ------------------------------------------------------------------ actions
    def _private_message(self, o: OccupantItem) -> None:
        chat = self.chat

        async def go():
            pm = await self.core.chats.get_or_create(chat.account_id, "%s/%s" % (chat.jid, o.nick), "pm",
                                                     name="%s (%s)" % (o.nick, chat.name or chat.jid))
            self.view.window.show_chat(pm)
        widgets.run(self, go())

    def _mention(self, o: OccupantItem) -> None:
        self.view.composer._insert(o.nick + ": ")

    def _details(self, o: OccupantItem) -> None:
        acc = self.core.accounts.get(self.chat.account_id)

        async def go():
            chat = await self.core.open_chat(acc, o.real_jid)
            from .details import DetailsDialog

            DetailsDialog(self.view.window, chat).present(self)
        widgets.run(self, go())

    def _ask_reason(self, heading: str, body: str, action_label: str, cb) -> None:
        entry = Gtk.Entry(placeholder_text=_("Reason (optional)"))
        compat.alert(self, heading, body, [("cancel", _("Cancel"), ""), ("go", action_label, "destructive")],
                     lambda r: r == "go" and cb(entry.get_text()), extra=entry)

    def _kick(self, o: OccupantItem) -> None:
        chat = self.chat
        self._ask_reason(_("Remove %s?") % o.nick, _("They can join again unless you ban them."), _("Remove"),
                         lambda reason: widgets.run(self, self.core.muc.kick(chat, o.nick, reason),
                                                    error=_("Could not remove %s") % o.nick))

    def _ban(self, o: OccupantItem) -> None:
        chat = self.chat
        self._ask_reason(_("Ban %s?") % o.nick, _("They can't join again until you lift the ban."), _("Ban"),
                         lambda reason: widgets.run(self, self.core.muc.ban(chat, o.real_jid, reason),
                                                    error=_("Could not ban %s") % o.nick))

    def _role(self, o: OccupantItem, role: str) -> None:
        widgets.run(self, self.core.muc.set_role(self.chat, o.nick, role), error=_("Not allowed"))

    def _affiliation(self, o: OccupantItem, aff: str) -> None:
        widgets.run(self, self.core.muc.set_affiliation(self.chat, o.real_jid, aff), error=_("Not allowed"))

