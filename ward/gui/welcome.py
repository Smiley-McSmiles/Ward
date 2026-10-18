"""First start and "Add account": sign in with an existing address, create a new account on a server (in-band
registration with its form and CAPTCHA), or use an invite link."""

from __future__ import annotations

import logging

from gi.repository import Adw, GLib, Gtk

from .. import APP_ID, APP_NAME, util
from ..core import register
from ..core import uri as urimod
from ..i18n import _
from . import compat, widgets
from .forms_view import FormView

log = logging.getLogger("ward.gui")

SUGGESTED = ("conversations.im", "jabber.de", "yax.im", "magicbroccoli.de", "chalec.org", "jabberfr.org")


class WelcomePage(Gtk.Box):
    __gtype_name__ = "WardWelcome"

    def __init__(self, window, dialog=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.core = window.core
        self.dialog = dialog
        self.toasts = Adw.ToastOverlay(vexpand=True)
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.append(self.toasts)
        self._acc = None
        self._acc_handlers: list[int] = []
        self.preauth = ""
        self.nav.add(self._start_page())

    # ================================================================== pages
    def _page(self, title: str, child: Gtk.Widget, tag: str, action: Gtk.Widget | None = None) -> Adw.NavigationPage:
        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        if action is not None:
            header.pack_end(action)
        tv.add_top_bar(header)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        clamp = Adw.Clamp(maximum_size=440, margin_start=12, margin_end=12, margin_top=12, margin_bottom=24)
        clamp.set_child(child)
        sc.set_child(clamp)
        tv.set_content(sc)
        return Adw.NavigationPage(title=title, child=tv, tag=tag)

    def _start_page(self) -> Adw.NavigationPage:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER, vexpand=True)
        status = Adw.StatusPage(icon_name=APP_ID, title=_("Welcome to %s") % APP_NAME,
                                description=_("Private chats and groups on the open XMPP network. Your messages "
                                              "are end-to-end encrypted, and you can use any server, or your own."))
        status.add_css_class("compact")
        box.append(status)
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        signin = Gtk.Button(label=_("Sign in"), css_classes=["pill", "suggested-action"])
        signin.connect("clicked", lambda *_: self.nav.push(self._signin_page()))
        buttons.append(signin)
        create = Gtk.Button(label=_("Create an account"), css_classes=["pill"])
        create.connect("clicked", lambda *_: self.nav.push(self._servers_page()))
        buttons.append(create)
        invite = Gtk.Button(label=_("I have an invite link"), css_classes=["flat"])
        invite.connect("clicked", lambda *_: self.nav.push(self._invite_page()))
        buttons.append(invite)
        box.append(buttons)
        return self._page(_("Welcome"), box, "start")

    # ------------------------------------------------------------------ sign in
    def _signin_page(self, jid: str = "", password: str = "") -> Adw.NavigationPage:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        g = Adw.PreferencesGroup(title=_("Sign in"),
                                 description=_("Your address looks like an email address: name@server"))
        self.jid_row = Adw.EntryRow(title=_("Address"), text=jid)
        self.jid_row.set_input_purpose(Gtk.InputPurpose.EMAIL)
        g.add(self.jid_row)
        self.pw_row = Adw.PasswordEntryRow(title=_("Password"), text=password)
        self.pw_row.connect("entry-activated", lambda *_: self._sign_in())
        g.add(self.pw_row)
        self.store_row = Adw.ComboRow(title=_("Remember password"))
        self.store_modes = ["keyring", "file", "never"]
        self.store_row.set_model(Gtk.StringList.new([_("In the system keyring"), _("In a file (less safe)"),
                                                     _("Ask every time")]))
        g.add(self.store_row)
        box.append(g)
        box.append(self._connection_group())
        self.signin_button = Gtk.Button(label=_("Sign in"), css_classes=["pill", "suggested-action"],
                                        halign=Gtk.Align.CENTER)
        self.signin_button.connect("clicked", lambda *_: self._sign_in())
        box.append(self.signin_button)
        self.progress = Gtk.Label(css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER)
        box.append(self.progress)
        GLib.idle_add(lambda: (self.jid_row if not jid else self.pw_row).grab_focus() and False)
        return self._page(_("Sign in"), box, "signin")

    def _connection_group(self) -> Adw.PreferencesGroup:
        """Host, port, TLS mode and proxy: for self-hosted servers on unusual ports, or Tor."""
        adv = Adw.PreferencesGroup()
        exp = Adw.ExpanderRow(title=_("Connection settings"), subtitle=_("Only needed for unusual servers"))
        self.host_row = Adw.EntryRow(title=_("Server address (host)"))
        exp.add_row(self.host_row)
        self.port_row = Adw.SpinRow.new_with_range(0, 65535, 1)
        self.port_row.set_title(_("Port (0 for automatic)"))
        exp.add_row(self.port_row)
        self.tls_row = Adw.ComboRow(title=_("Connection encryption"))
        self.tls_modes = ["auto", "starttls", "direct"]
        self.tls_row.set_model(Gtk.StringList.new([_("Automatic"), "STARTTLS", _("Direct TLS")]))
        exp.add_row(self.tls_row)
        self.proxy_row = Adw.ComboRow(title=_("Proxy"))
        self.proxy_modes = ["system", "none", "tor"]
        self.proxy_row.set_model(Gtk.StringList.new([_("System settings"), _("None"), _("Tor (127.0.0.1:9050)")]))
        exp.add_row(self.proxy_row)
        adv.add(exp)
        return adv

    def _connection_options(self) -> dict:
        if not hasattr(self, "host_row"):
            return {}
        opts = {}
        host = self.host_row.get_text().strip()
        if host:
            opts["host"] = host
        port = int(self.port_row.get_value())
        if port:
            opts["port"] = port
        tls = self.tls_modes[self.tls_row.get_selected()]
        if tls != "auto":
            opts["tls"] = tls
        proxy = self.proxy_modes[self.proxy_row.get_selected()]
        if proxy != "system":
            opts["proxy"] = proxy
        return opts

    def _options(self) -> dict:
        opts = self._connection_options()
        opts["store_password"] = self.store_modes[self.store_row.get_selected()]
        return opts

    def _sign_in(self) -> None:
        jid = util.bare(self.jid_row.get_text().strip())
        pw = self.pw_row.get_text()
        if not util.valid_bare_jid(jid) or "@" not in jid:
            self._error(_("Enter your full address, like alex@example.org"))
            return
        if not pw:
            self._error(_("Enter your password"))
            return
        self._busy(True, _("Connecting…"))
        opts = self._options()
        widgets.run(self, self._add_account(jid, pw, opts), on_error=self._failed)

    async def _add_account(self, jid: str, pw: str, opts: dict) -> None:
        mode = opts.pop("store_password", "keyring")
        acc = await self.core.add_account(jid, pw, mode, dict(opts, store_password=mode), connect=False)
        self._acc = acc
        self._acc_handlers = [acc.connect("notify::state", self._on_state), acc.connect("notify::step", self._on_state)]
        await self.core.connect_account(acc, pw)

    def _on_state(self, acc, _p) -> None:
        if acc is not self._acc:
            return
        if acc.state == "online" and not acc.step:
            self._done(acc)
        elif acc.state == "online":
            self.progress.set_text(_("Signed in, syncing…"))
        elif acc.state == "connecting":
            self.progress.set_text({"resolving": _("Looking up the server…"), "tls": _("Securing the connection…"),
                                    "auth": _("Signing in…")}.get(acc.step, _("Connecting…")))
        elif acc.state == "error":
            if acc.error_kind == "certificate":
                # the window shows the certificate dialog; trusting it reconnects and we keep waiting
                self.progress.set_text(_("The server's certificate needs your decision"))
                return
            err = acc.error
            self._forget_account()
            self._failed(err)

    def _forget_account(self) -> None:
        acc = self._acc
        self._acc = None
        if acc is not None:
            self._disconnect(acc)
            widgets.run(None, self.core.remove_account(acc))

    def _disconnect(self, acc) -> None:
        for h in self._acc_handlers:
            acc.disconnect(h)
        self._acc_handlers = []

    def _done(self, acc) -> None:
        self._disconnect(acc)
        self._acc = None
        self._busy(False)
        if self.dialog is not None:
            self.dialog.close()
        compat.toast(self.window, _("Signed in as %s") % acc.jid)
        self.window._accounts_changed()

    def _failed(self, e) -> None:
        self._busy(False)
        self._error(str(e))

    def _busy(self, on: bool, text: str = "") -> None:
        if hasattr(self, "signin_button"):
            self.signin_button.set_sensitive(not on)
        if hasattr(self, "progress"):
            self.progress.set_text(text)

    def _error(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=8))
        if hasattr(self, "progress"):
            self.progress.set_text("")

    # ------------------------------------------------------------------ create an account
    def _servers_page(self) -> Adw.NavigationPage:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        g = Adw.PreferencesGroup(title=_("Choose a server"),
                                 description=_("Like email, you can write to people on every other server. Pick "
                                               "one you trust; you can move later."))
        self.domain_row = Adw.EntryRow(title=_("Server, like example.org"), show_apply_button=True)
        self.domain_row.connect("apply", lambda e: self._get_form(e.get_text().strip()))
        self.domain_row.connect("entry-activated", lambda e: self._get_form(e.get_text().strip()))
        g.add(self.domain_row)
        box.append(g)
        box.append(self._connection_group())
        self.providers = Adw.PreferencesGroup(title=_("Servers that allow signing up from apps"))
        self._provider_rows: list[Gtk.Widget] = []
        box.append(self.providers)
        self._fill_providers([register.Provider(d) for d in SUGGESTED], loading=True)
        widgets.run(self, register.fetch_providers(), done=lambda ps: self._fill_providers(
            [p for p in ps if p.in_band is not False and p.category in ("", "A", "B")][:20] or
            [register.Provider(d) for d in SUGGESTED]), on_error=lambda _e: self._fill_providers(
            [register.Provider(d) for d in SUGGESTED]))
        return self._page(_("Create an account"), box, "servers")

    def _fill_providers(self, providers, loading: bool = False) -> None:
        for r in self._provider_rows:
            self.providers.remove(r)
        self._provider_rows = []
        for p in providers:
            sub = []
            if p.upload_mb:
                sub.append(_("files up to %d MB") % p.upload_mb)
            if p.archive_days:
                sub.append(_("history kept %d days") % p.archive_days)
            if p.locations:
                sub.append(", ".join(p.locations))
            row = Adw.ActionRow(title=p.domain, subtitle=" · ".join(sub), activatable=True)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r, d=p.domain: self._get_form(d))
            self.providers.add(row)
            self._provider_rows.append(row)
        if loading:
            row = Adw.ActionRow(title=_("Loading the list of servers…"))
            row.add_suffix(compat.spinner())
            self.providers.add(row)
            self._provider_rows.append(row)

    def _get_form(self, domain: str, preauth: str = "") -> None:
        domain = domain.strip().lower()
        if not domain or "@" in domain or " " in domain:
            self._error(_("Enter a server name like example.org"))
            return
        self.preauth = preauth
        self.toasts.add_toast(Adw.Toast(title=_("Asking %s…") % domain, timeout=2))
        self.reg_options = self._connection_options()
        widgets.run(self, register.get_form(domain, self.reg_options, preauth=preauth),
                    done=lambda rf: self.nav.push(self._register_page(rf)), on_error=self._form_failed)

    def _form_failed(self, e) -> None:
        cert = getattr(e, "cert", None)
        if cert is not None:
            self._error(_("The server's certificate is not trusted: %s") % (cert.error or "?"))
        else:
            self._error(str(e))

    def _register_page(self, rf: register.RegistrationForm) -> Adw.NavigationPage:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.rf = rf
        if rf.redirect_url and not rf.form and not rf.legacy_fields:
            status = Adw.StatusPage(icon_name="web-browser-symbolic", title=_("Sign up on the website"),
                                    description=_("%s only creates accounts on its website. Come back and sign in "
                                                  "when you have one.") % rf.domain)
            btn = Gtk.Button(label=_("Open website"), css_classes=["pill", "suggested-action"],
                             halign=Gtk.Align.CENTER)
            btn.connect("clicked", lambda *_: widgets.open_uri(self, rf.redirect_url))
            status.set_child(btn)
            box.append(status)
            return self._page(rf.domain, box, "register")
        g = Adw.PreferencesGroup(title=_("Your new account"), description=widgets.escape(rf.instructions)
                                 if rf.instructions and not rf.form else "")
        user = Adw.EntryRow(title=_("User name"))
        suffix = Gtk.Label(label="@" + rf.domain, css_classes=["dim-label"])
        user.add_suffix(suffix)
        g.add(user)
        pw = Adw.PasswordEntryRow(title=_("Password"))
        g.add(pw)
        pw2 = Adw.PasswordEntryRow(title=_("Repeat password"))
        g.add(pw2)
        box.append(g)
        self.form_view = None
        extra_rows = {}
        if rf.form is not None:
            self.form_view = FormView(rf.form, skip={"username", "password", "FORM_TYPE"})
            box.append(self.form_view)
        else:
            eg = Adw.PreferencesGroup()
            for name in rf.legacy_fields:
                if name in ("username", "password"):
                    continue
                r = Adw.EntryRow(title=name.title())
                eg.add(r)
                extra_rows[name] = r
            if extra_rows:
                box.append(eg)
        button = Gtk.Button(label=_("Create account"), css_classes=["pill", "suggested-action"],
                            halign=Gtk.Align.CENTER)
        box.append(button)

        def create(*_a):
            username = user.get_text().strip().lower()
            if not username:
                self._error(_("Choose a user name"))
                return
            if len(pw.get_text()) < 8:
                self._error(_("Use a password with at least 8 characters"))
                return
            if pw.get_text() != pw2.get_text():
                self._error(_("The passwords don't match"))
                return
            form = self.form_view.collect() if self.form_view is not None else None
            if self.form_view is not None:
                missing = self.form_view.missing()
                if missing:
                    self._error(_("Fill in: %s") % ", ".join(missing))
                    return
            legacy = {k: r.get_text() for k, r in extra_rows.items() if r.get_text()}
            button.set_sensitive(False)

            async def go():
                options = dict(getattr(self, "reg_options", {}))
                jid = await register.register(rf.domain, username, pw.get_text(), form, legacy, options,
                                              preauth=self.preauth)
                await self._add_account(jid, pw.get_text(), options)

            def failed(e):
                button.set_sensitive(True)
                self._failed(e)
            self.progress = Gtk.Label(css_classes=["dim-label"], wrap=True)
            box.append(self.progress)
            widgets.run(self, go(), on_error=failed)
        button.connect("clicked", create)
        return self._page(rf.domain, box, "register")

    # ------------------------------------------------------------------ invites
    def _invite_page(self) -> Adw.NavigationPage:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        g = Adw.PreferencesGroup(title=_("Invite link"),
                                 description=_("Paste the link you got, starting with xmpp: or https://"))
        entry = Adw.EntryRow(title=_("Link"), show_apply_button=True)
        entry.connect("apply", lambda e: self.use_invite(e.get_text().strip()))
        g.add(entry)
        box.append(g)
        return self._page(_("Invite"), box, "invite")

    def use_invite(self, link: str) -> None:
        u = urimod.parse_xmpp(link)
        if u is None and "#" in link:
            # landing pages put the xmpp: link after the # (https://server/invite/#xmpp:...)
            u = urimod.parse_xmpp(link.split("#", 1)[1])
        if u is None:
            self._error(_("This is not an invite link"))
            return
        if u.is_account_invite:
            self._get_form(util.domain(u.jid), u.params.get("preauth", ""))
        elif self.core.accounts.list():
            if self.dialog is not None:
                self.dialog.close()
            self.window.app.open_uri(link)
        else:
            # a contact invite: we need an account on the inviter's server first, if it allows it
            self.toasts.add_toast(Adw.Toast(title=_("Create an account first; then the invite adds the contact"),
                                            timeout=6))
            self.window.app.pending_uri = link
            self._get_form(util.domain(u.jid), u.params.get("preauth", ""))
