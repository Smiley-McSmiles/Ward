"""Preferences, generated from the settings schema: one row per option, grouped and searchable, with "Advanced"
expanders.  Accounts get a sub-page each (connection, security, OpenPGP key, privacy, history, files)."""

from __future__ import annotations

import logging
import os
from collections import OrderedDict

from gi.repository import Adw, GLib, Gtk

from .. import deps, paths, schema, util
from ..core.models import AccountItem
from ..i18n import _
from . import compat, widgets

log = logging.getLogger("ward.gui")

RECONNECT_KEYS = {"host", "port", "tls", "resource", "priority", "proxy", "proxy_host", "proxy_port", "proxy_user",
                  "allow_plain", "client_cert"}


# ================================================================================================ rows
def spec_row(spec: schema.Spec, value, on_change) -> Gtk.Widget | None:
    """One preference row of any type; on_change(new_value) is called with a coerced value."""
    title = _(spec.title) if spec.title else ""
    subtitle = _(spec.subtitle) if spec.subtitle else ""
    if spec.type == "bool":
        row = Adw.SwitchRow(title=title, subtitle=subtitle, active=bool(value))
        row.connect("notify::active", lambda r, _p: on_change(r.get_active()))
        return row
    if spec.type == "choice":
        row = Adw.ComboRow(title=title, subtitle=subtitle)
        row.set_model(Gtk.StringList.new([_(label) for _c, label in spec.choices]))
        values = [c for c, _l in spec.choices]
        if value in values:
            row.set_selected(values.index(value))
        row.connect("notify::selected", lambda r, _p: on_change(values[r.get_selected()]))
        return row
    if spec.type == "int":
        row = Adw.SpinRow.new_with_range(spec.min if spec.min is not None else 0,
                                         spec.max if spec.max is not None else 100000, spec.step or 1)
        row.set_title(title)
        unit = _(spec.unit) if spec.unit else ""
        row.set_subtitle(" · ".join(x for x in (subtitle, unit) if x))
        row.set_value(int(value or 0))
        row.connect("notify::value", lambda r, _p: on_change(int(r.get_value())))
        return row
    if spec.type == "font":
        row = Adw.ActionRow(title=title, subtitle=subtitle or _("Default"))
        btn = Gtk.FontDialogButton(valign=Gtk.Align.CENTER, level=Gtk.FontLevel.FAMILY)
        btn.set_dialog(Gtk.FontDialog())
        if value:
            from gi.repository import Pango

            btn.set_font_desc(Pango.FontDescription.from_string(value))
        reset = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                           tooltip_text=_("Default"))

        def font_set(b, _p):
            desc = b.get_font_desc()
            fam = desc.get_family() if desc is not None else ""
            row.set_subtitle(fam or _("Default"))
            on_change(fam or "")
        btn.connect("notify::font-desc", font_set)
        reset.connect("clicked", lambda *_: (row.set_subtitle(_("Default")), on_change("")))
        row.add_suffix(btn)
        row.add_suffix(reset)
        if value:
            row.set_subtitle(value)
        return row
    if spec.type in ("str", "list", "time_range"):
        text = ", ".join(value) if isinstance(value, list) else str(value or "")
        if spec.type == "list" and value and all(len(v) <= 4 for v in value):
            text = " ".join(value)     # emoji lists read better with spaces
        row = Adw.EntryRow(title=title, text=text, show_apply_button=True)
        if subtitle:
            row.set_tooltip_text(subtitle)

        def apply(r):
            t = r.get_text().strip()
            if spec.type == "list":
                parts = [p.strip() for p in (t.split(",") if "," in t else t.split())]
                v = [p for p in parts if p]
            else:
                v = t
            try:
                v = schema.coerce(spec, v)
            except ValueError as e:
                r.add_css_class("error")
                compat.toast(r, str(e))
                return
            r.remove_css_class("error")
            on_change(v)
        row.connect("apply", apply)
        return row
    return None


def build_groups(page: Adw.PreferencesPage, specs: list[tuple[str, schema.Spec]], get, set_,
                 rows: dict | None = None) -> dict:
    """Groups in schema order, advanced options in an expander at the end of their group."""
    groups: OrderedDict[str, list] = OrderedDict()
    for key, spec in specs:
        groups.setdefault(spec.group, []).append((key, spec))
    out = {}
    for gname, items in groups.items():
        g = Adw.PreferencesGroup(title=_(gname) if gname else "")
        page.add(g)
        out[gname] = g
        advanced = None
        for key, spec in items:
            row = spec_row(spec, get(key), lambda v, k=key: set_(k, v))
            if row is None:
                continue
            if rows is not None:
                rows[key] = row
            if spec.advanced:
                if advanced is None:
                    advanced = Adw.ExpanderRow(title=_("Advanced"))
                advanced.add_row(row)
            else:
                g.add(row)
        if advanced is not None:
            g.add(advanced)
    return out


# ================================================================================================ dialog
class Preferences:
    def __init__(self, window, page: str = ""):
        self.window = window
        self.core = window.core
        self.settings = self.core.settings
        self.d = compat.preferences_dialog()
        self.pages: dict[str, Adw.PreferencesPage] = {}
        for key, title, icon in schema.PAGES:
            p = Adw.PreferencesPage(title=_(title), icon_name=icon, name=key)
            self.pages[key] = p
            specs = [(k, s) for k, s in schema.SPECS.items() if s.page == key]
            groups = build_groups(p, specs, lambda k: self.settings[k], self._set)
            extra = getattr(self, "_page_" + key, None)
            if extra is not None:
                extra(p, groups)
            self.d.add(p)
        if page in self.pages:
            self.d.set_visible_page_name(page)

    def present(self, parent: Gtk.Widget) -> None:
        compat.present_preferences(self.d, parent)

    def _set(self, key: str, value) -> None:
        try:
            self.settings[key] = value
        except ValueError as e:
            compat.toast(self.d, str(e))

    # ------------------------------------------------------------------ extra rows per page
    def _page_general(self, page, groups) -> None:
        if paths.in_flatpak():
            return
        g = groups.get("Running")
        if g is not None and not self.window.app.background_supported():
            g.set_description(_("Your desktop may stop apps running in the background"))

    def _page_accounts(self, page, groups) -> None:
        self.accounts_group = Adw.PreferencesGroup(title=_("Accounts"))
        page.add(self.accounts_group)
        add = Gtk.Button(icon_name="list-add-symbolic", css_classes=["flat"], tooltip_text=_("Add account"))
        add.connect("clicked", lambda *_: self.window.show_welcome(True))
        self.accounts_group.set_header_suffix(add)
        self._account_rows: list[Gtk.Widget] = []
        self._fill_accounts()
        h = self.core.accounts.store.connect("items-changed", lambda *_: self._fill_accounts())
        self.d.connect("destroy", lambda *_: self.core.accounts.store.disconnect(h))

    def _fill_accounts(self) -> None:
        for r in self._account_rows:
            self.accounts_group.remove(r)
        self._account_rows = []
        for acc in self.core.accounts.list():
            row = Adw.ActionRow(title=widgets.escape(acc.display_name), subtitle=acc.jid, activatable=True)
            row.add_prefix(widgets.Avatar(36, acc.display_name, acc.avatar,
                                          acc.show if acc.state == "online" else "offline"))
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r, a=acc: compat.prefs_push(self.d, AccountPage(self, a).page))
            self.accounts_group.add(row)
            self._account_rows.append(row)
        if not self._account_rows:
            row = Adw.ActionRow(title=_("No accounts"))
            self.accounts_group.add(row)
            self._account_rows.append(row)

    def _page_encryption(self, page, groups) -> None:
        if self.core.omemo is None or not deps.legacy_omemo_available():
            enc = groups.get("Encryption")
            if enc is not None:
                row = Adw.ActionRow(title=_("Install encryption support…") if self.core.omemo is None
                                    else _("Add legacy OMEMO for older apps…"), activatable=True,
                                    subtitle=_("Downloads the OMEMO libraries into Ward's own folder"))
                row.add_suffix(Gtk.Image.new_from_icon_name("folder-download-symbolic"))
                row.connect("activated", lambda *_: self.window.app.install_encryption(self.d))
                enc.add(row)
        g = Adw.PreferencesGroup(title=_("Your devices"))
        page.add(g)
        for acc in self.core.accounts.list():
            row = Adw.ActionRow(title=widgets.escape(acc.display_name), subtitle=_("Fingerprints, QR code, trust "
                                                                                    "export"), activatable=True)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r, a=acc: self._own_devices(a))
            g.add(row)
        pgp = groups.get("OpenPGP")
        if pgp is not None:
            from ..core.pgp import gnupg

            binary = gnupg.find_gpg(self.settings["pgp.gpg_binary"], self.settings["pgp.use_gpgman"])
            _gm_bin, gm_home = gnupg.gpgman_settings()
            lines = [_("gpg: %s") % (binary or _("not found"))]
            if os.path.isfile(gnupg.gpgman_settings_file()):
                lines.append(_("GPGMan settings found: keyring %s") % (gm_home or "~/.gnupg"))
            elif gnupg.gpgman_installed():
                lines.append(_("GPGMan is installed"))
            else:
                lines.append(_("GPGMan (github.com/Smiley-McSmiles/GPGMan) manages GnuPG keys and shares them with "
                               "Ward"))
            pgp.set_description("\n".join(widgets.escape(x) for x in lines))
            if gnupg.gpgman_installed() and not paths.in_flatpak():
                btn = Gtk.Button(label=_("Open GPGMan"), css_classes=["flat"], valign=Gtk.Align.CENTER)
                btn.connect("clicked", lambda *_: _spawn([gnupg.gpgman_installed()]))
                pgp.set_header_suffix(btn)

    def _own_devices(self, acc: AccountItem) -> None:
        from .trust import DevicesDialog

        DevicesDialog(self.window, acc, acc.jid).present(self.d)

    def _page_storage(self, page, groups) -> None:
        g = Adw.PreferencesGroup(title=_("Usage"))
        page.add(g)
        cache = Adw.ActionRow(title=_("Cache"), subtitle="…")
        clear = Gtk.Button(label=_("Clear"), valign=Gtk.Align.CENTER)
        cache.add_suffix(clear)
        g.add(cache)
        db = Adw.ActionRow(title=_("Message database"), subtitle="…")
        compact = Gtk.Button(label=_("Compact"), valign=Gtk.Align.CENTER)
        db.add_suffix(compact)
        g.add(db)
        folder = Adw.ActionRow(title=_("Data folder"), subtitle=paths.data_dir(), activatable=True)
        folder.add_suffix(Gtk.Image.new_from_icon_name("folder-open-symbolic"))
        folder.connect("activated", lambda *_: widgets.open_file(self.window, paths.data_dir()))
        g.add(folder)

        async def sizes():
            usage = await self.core.files.cache_usage()
            size = await self.core.db.size()
            cache.set_subtitle(util.human_size(usage))
            db.set_subtitle(util.human_size(size))

        widgets.run(self.window, sizes())

        async def do_clear():
            await self.core.files.clear_cache()
            await sizes()

        async def do_compact():
            await self.core.db.compact()
            await sizes()
        clear.connect("clicked", lambda *_: widgets.run(self.window, do_clear()))
        compact.connect("clicked", lambda *_: widgets.run(self.window, do_compact()))

    def _page_advanced(self, page, groups) -> None:
        g = Adw.PreferencesGroup(title=_("About this installation"))
        page.add(g)
        cfg = Adw.ActionRow(title=_("Config folder"), subtitle=paths.config_dir(), activatable=True)
        cfg.add_suffix(Gtk.Image.new_from_icon_name("folder-open-symbolic"))
        cfg.connect("activated", lambda *_: widgets.open_file(self.window, paths.ensure(paths.config_dir())))
        g.add(cfg)
        rep = Adw.ActionRow(title=_("Libraries and optional features"), activatable=True,
                            subtitle=_("What is installed, and how to install what is missing"))
        rep.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        rep.connect("activated", lambda *_: _report_dialog(self.d))
        g.add(rep)
        log_row = Adw.ActionRow(title=_("Log file"), subtitle=paths.log_file())
        log_row.add_css_class("property")
        g.add(log_row)


def _report_dialog(parent) -> None:
    from .dialogs import text_view_dialog

    text_view_dialog(parent, _("Libraries"), deps.report())


def _spawn(argv: list[str]) -> None:
    try:
        GLib.spawn_async(argv, flags=GLib.SpawnFlags.SEARCH_PATH)
    except GLib.Error as e:
        log.info("could not start %s: %s", argv[0], e)


# ================================================================================================ accounts
class AccountPage:
    def __init__(self, prefs: Preferences, acc: AccountItem):
        self.prefs = prefs
        self.core = prefs.core
        self.acc = acc
        self.window = prefs.window
        page = Adw.PreferencesPage()
        self.content = page
        top = Adw.PreferencesGroup()
        page.add(top)
        state = Adw.ActionRow(title=widgets.escape(acc.display_name), subtitle=self._state_text())
        state.add_prefix(widgets.Avatar(48, acc.display_name, acc.avatar))
        self.state_row = state
        self.connect_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.connect_button.connect("clicked", lambda *_: self._toggle_connection())
        state.add_suffix(self.connect_button)
        top.add(state)
        self._h = acc.connect("notify::state", lambda *_: self._update_state())
        self._update_state()

        specs = [(k, s) for k, s in schema.ACCOUNT_SPECS.items() if s.type != "json" and k not in ("pgp_key",)]
        self.rows: dict[str, Gtk.Widget] = {}
        groups = build_groups(page, specs, acc.option, self._set, self.rows)
        self._proxy_rows(acc.option("proxy"))
        conn = groups.get("Connection")
        if conn is not None:
            rc = compat.button_row(_("Reconnect now"), "view-refresh-symbolic")
            compat.row_activated(rc, lambda: widgets.run(self.window, self.core.reconnect(acc)))
            conn.add(rc)
        sec = groups.get("Security")
        if sec is not None:
            self._pins(sec)
        if self.core.pgp is not None and self.core.settings["pgp.enabled"]:
            self._pgp_group(page)
        actions = Adw.PreferencesGroup()
        page.add(actions)
        for title, cb, css in ((_("Profile…"), lambda: self.window.show_profile(acc), ""),
                               (_("Your devices and keys…"), lambda: prefs._own_devices(acc), ""),
                               (_("Server administration…"), self._admin, ""),
                               (_("Remove account from Ward…"), self._remove, "error")):
            r = Adw.ActionRow(title=title, activatable=True)
            if css:
                r.add_css_class(css)
            r.connect("activated", lambda *_a, cb=cb: cb())
            actions.add(r)
        self.page = compat.subpage(acc.jid, page)

    def _state_text(self) -> str:
        from .sidebar import Sidebar

        return widgets.escape(Sidebar._account_state_text(self.acc))

    def _update_state(self) -> None:
        self.state_row.set_subtitle(self._state_text())
        online = self.acc.state in ("online", "connecting")
        self.connect_button.set_label(_("Disconnect") if online else _("Connect"))

    def _toggle_connection(self) -> None:
        if self.acc.state in ("online", "connecting"):
            widgets.run(self.window, self.core.set_presence(self.acc, "offline"))
        else:
            widgets.run(self.window, self.core.set_presence(self.acc, self.acc.show if self.acc.show != "offline"
                                                            else "available"))

    def _proxy_rows(self, proxy: str) -> None:
        for k in ("proxy_host", "proxy_port", "proxy_user"):
            if k in self.rows:
                self.rows[k].set_visible(proxy in ("socks5", "http"))

    def _set(self, key: str, value) -> None:
        acc = self.acc
        if key == "proxy":
            self._proxy_rows(value)

        async def go():
            if key == "enabled":
                await self.core.set_enabled(acc, bool(value))
                return
            await self.core.accounts.set_option(acc, key, value)
            if key == "store_password":
                from ..core import secrets

                s = self.core.sessions.get(acc.id)
                if value == "never":
                    await secrets.clear(acc.jid)
                elif s is not None and s.password:
                    await secrets.store(acc.jid, s.password, value)
        widgets.run(self.prefs.d, go())
        if key in RECONNECT_KEYS:
            compat.toast(self.prefs.d, _("Reconnect to use the new connection settings"))

    def _pins(self, group: Adw.PreferencesGroup) -> None:
        pins = [p for p in (self.acc.option("pinned_certs") or []) if isinstance(p, dict)]
        if not pins:
            return
        exp = Adw.ExpanderRow(title=_("Trusted certificates"), subtitle=_("Certificates you accepted manually"))
        for p in pins:
            fp = p.get("sha256", "")
            r = Adw.ActionRow(title=widgets.escape(p.get("subject") or p.get("domain") or "?"),
                              subtitle=":".join(fp[i:i + 2] for i in range(0, min(len(fp), 32), 2)) + "…")
            rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                            tooltip_text=_("Stop trusting"))
            rm.connect("clicked", lambda *_a, fp=fp, r=r: self._unpin(fp, exp, r))
            r.add_suffix(rm)
            exp.add_row(r)
        group.add(exp)

    def _unpin(self, fp: str, exp, row) -> None:
        pins = [p for p in (self.acc.option("pinned_certs") or []) if isinstance(p, dict) and p.get("sha256") != fp]
        self.acc.options["pinned_certs"] = pins
        widgets.run(self.prefs.d, self.core.accounts.save_options(self.acc))
        exp.remove(row)

    # ------------------------------------------------------------------ OpenPGP
    def _pgp_group(self, page: Adw.PreferencesPage) -> None:
        g = Adw.PreferencesGroup(title=_("OpenPGP key"),
                                 description=_("The key others use to encrypt to you with OpenPGP. Keys come from "
                                               "the GnuPG keyring that GPGMan shares, or a file GPGMan exported."))
        page.add(g)
        self.pgp_g = g
        self.key_row = Adw.ComboRow(title=_("Key"))
        self.key_row.set_model(Gtk.StringList.new([_("Loading…")]))
        self.key_row.set_sensitive(False)
        g.add(self.key_row)
        self.keys: list = []
        gen = compat.button_row(_("Create a new key"), "list-add-symbolic")
        compat.row_activated(gen, self._generate)
        g.add(gen)
        imp = compat.button_row(_("Import a key file (.asc from GPGMan)…"), "document-open-symbolic")
        compat.row_activated(imp, self._import)
        g.add(imp)
        widgets.run(self.prefs.d, self._load_keys(), on_error=lambda e: self.key_row.set_subtitle(str(e)))

    async def _load_keys(self) -> None:
        pgp = self.core.pgp
        if not pgp.available:
            self.key_row.set_model(Gtk.StringList.new([_("gpg is not installed")]))
            return
        self.keys = await pgp.secret_keys()
        current = pgp.own_key(self.acc.id)
        labels = [_("None")] + ["%s · %s" % (k.name, k.fingerprint[-16:]) for k in self.keys]
        self.key_row.set_model(Gtk.StringList.new(labels))
        sel = 0
        for i, k in enumerate(self.keys, start=1):
            if k.fingerprint.upper() == current:
                sel = i
        self.key_row.set_selected(sel)
        self.key_row.set_sensitive(True)
        if not getattr(self, "_key_handler", 0):
            self._key_handler = self.key_row.connect("notify::selected", lambda r, _p: self._choose(r.get_selected()))
        matching = [k for k in self.keys if self.acc.jid.lower() in k.xmpp_jids]
        self.key_row.set_subtitle(_("Keys with “xmpp:%s” in the user id work best with other apps") % self.acc.jid
                                  if self.keys and not matching else "")

    def _choose(self, index: int) -> None:
        fp = self.keys[index - 1].fingerprint if 0 < index <= len(self.keys) else ""
        widgets.run(self.prefs.d, self.core.pgp.choose_key(self.acc, fp),
                    done=lambda _r: compat.toast(self.prefs.d, _("Key published") if fp else _("No key")))

    def _generate(self) -> None:
        def go(resp):
            if resp != "create":
                return
            compat.toast(self.prefs.d, _("Creating a key; gpg may ask for a passphrase"))
            widgets.run(self.prefs.d, self.core.pgp.generate_key(self.acc), done=lambda _fp: widgets.run(
                self.prefs.d, self._load_keys()))
        compat.alert(self.prefs.d, _("Create an OpenPGP key?"),
                     _("A new key for xmpp:%s is created in the shared GnuPG keyring, so GPGMan shows it too.")
                     % self.acc.jid, [("cancel", _("Cancel"), ""), ("create", _("Create"), "suggested")], go)

    def _import(self) -> None:
        dialog = Gtk.FileDialog(title=_("Import an OpenPGP key"))
        f = Gtk.FileFilter()
        f.set_name(_("OpenPGP keys"))
        for pat in ("*.asc", "*.gpg", "*.pgp", "*.key"):
            f.add_pattern(pat)
        from gi.repository import Gio

        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        dialog.set_filters(store)
        exports = os.path.expanduser("~/GPGMan")
        if os.path.isdir(exports):
            dialog.set_initial_folder(Gio.File.new_for_path(exports))

        def done(d, res):
            try:
                file = d.open_finish(res)
            except GLib.Error:
                return
            if file is None or not file.get_path():
                return
            widgets.run(self.prefs.d, self.core.pgp.import_file(file.get_path()),
                        done=lambda fps: (compat.toast(self.prefs.d, _("Imported %d keys") % len(fps)),
                                          widgets.run(self.prefs.d, self._load_keys())))
        dialog.open(self.window, None, done)

    # ------------------------------------------------------------------ actions
    def _admin(self) -> None:
        from .admin import AdminDialog

        AdminDialog(self.window, self.acc).present(self.prefs.d)

    def _remove(self) -> None:
        acc = self.acc

        def go(resp):
            if resp == "remove":
                compat.prefs_pop(self.prefs.d)
                widgets.run(self.window, self.core.remove_account(acc))
        compat.alert(self.prefs.d, _("Remove %s?") % acc.jid,
                     _("Ward forgets this account and its local history. The account keeps existing on the server."),
                     [("cancel", _("Cancel"), ""), ("remove", _("Remove"), "destructive")], go)
