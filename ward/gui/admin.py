"""Server administration for accounts that are admins: overview numbers, users, invites and every ad-hoc command
the server offers (XEP-0050/0133), each rendered as a native form."""

from __future__ import annotations

from gi.repository import Adw, Gtk

from ..core.models import AccountItem
from ..i18n import _
from . import compat, widgets
from .dialogs import BaseDialog, online_session
from .forms_view import FormView

STAT_NAMES = {"registeredusersnum": "Registered users", "onlineusersnum": "Online users",
              "activeusersnum": "Active users", "uptime": "Uptime"}


class AdminDialog(BaseDialog):
    def __init__(self, window, acc: AccountItem):
        super().__init__(window, _("Administration of %s") % acc.jid.split("@")[-1], width=560, height=680)
        self.acc = acc
        self.stats = self.group(_("Overview"))
        self.stats_rows: list[Gtk.Widget] = []
        inv = self.group(_("Invites"), _("Links that let someone create an account on this server"))
        create = compat.button_row(_("Create an account invite"), "contact-new-symbolic")
        compat.row_activated(create, lambda: self._invite(False))
        inv.add(create)
        self.users = self.group(_("Users"))
        self.user_rows: list[Gtk.Widget] = []
        self.commands = self.group(_("Commands"), _("Everything the server lets administrators do"))
        self.command_rows: list[Gtk.Widget] = []
        self.run(self._load())

    async def _load(self) -> None:
        s = online_session(self.core, self.acc)
        stats = await self.core.admin.stats(s)
        for key, value in stats.items():
            row = Adw.ActionRow(title=_(STAT_NAMES.get(key, key)), subtitle=widgets.escape(value))
            row.add_css_class("property")
            self.stats.add(row)
            self.stats_rows.append(row)
        if not stats:
            self.stats.set_description(_("The server shares no numbers, or this account is not an admin"))
        users, kind = await self.core.admin.users(s)
        self.users.set_description(_("Online now") if kind == "online" else "")
        for jid in users[:200]:
            row = Adw.ActionRow(title=jid)
            self.users.add(row)
            self.user_rows.append(row)
        if not users:
            self.users.set_visible(False)
        for jid, node, name in await self.core.admin.list_commands(s):
            row = Adw.ActionRow(title=widgets.escape(name or node), subtitle=node, activatable=True)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r, j=jid, n=node, t=name: CommandDialog(
                self.window, self.acc, j, n, t).present(self.toasts))
            self.commands.add(row)
            self.command_rows.append(row)
        if not self.command_rows:
            self.commands.set_description(_("No commands are available to this account"))

    def _invite(self, contact: bool) -> None:
        from .trust import share_dialog

        async def go():
            return await self.core.admin.create_invite(online_session(self.core, self.acc), contact)
        self.run(go(), lambda link: share_dialog(self.toasts, _("Invite"), link,
                                                 _("Send this link to the person you invite. It works once.")))


class CommandDialog(BaseDialog):
    """Runs one ad-hoc command, step by step."""

    def __init__(self, window, acc: AccountItem, jid: str, node: str, title: str):
        super().__init__(window, title or node, _("Run"), width=560, height=640)
        self.acc = acc
        self.step = None
        self.form_view = None
        self.content_group = self.group()
        self.content_group.add(compat.spinner())
        self.run(self._start(jid, node), self._show)

    async def _start(self, jid: str, node: str):
        return await self.core.admin.execute(online_session(self.core, self.acc), jid, node)

    def _show(self, step) -> None:
        self.step = step
        self.page.remove(self.content_group)
        self.content_group = self.group()
        for n in step.notes:
            row = Adw.ActionRow(title=widgets.escape(n.text))
            row.set_title_lines(0)
            if n.type == "error":
                row.add_css_class("error")
            self.content_group.add(row)
        self.form_view = None
        if step.form is not None:
            self.form_view = FormView(step.form)
            self.content_group.add(self.form_view)
        if step.finished:
            self.action_button.set_label(_("Done"))
            if not step.notes and step.form is None:
                self.content_group.add(Adw.ActionRow(title=_("Done")))
        else:
            label = {"next": _("Next"), "complete": _("Finish"), "execute": _("Run")}.get(
                step.default_action or "execute", _("Run"))
            self.action_button.set_label(label)

    def on_action(self) -> None:
        step = self.step
        if step is None:
            return
        if step.finished:
            self.close()
            return
        form = self.form_view.collect() if self.form_view is not None else None

        async def go():
            return await self.core.admin.advance(online_session(self.core, self.acc), step,
                                                 step.default_action or "execute", form)
        self.run(go(), self._show)
