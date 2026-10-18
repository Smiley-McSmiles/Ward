"""Encryption keys: a contact's (or our own) OMEMO devices and OpenPGP keys, with fingerprints, verification by
QR code or link, and trust decisions; the trust export and import."""

from __future__ import annotations

import logging

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from ..core import uri as urimod
from ..core.models import AccountItem, DeviceItem
from ..i18n import _
from . import compat, widgets
from .dialogs import BaseDialog, online_session

log = logging.getLogger("ward.gui")


def qr_texture(text: str, scale: int = 6) -> Gdk.Texture | None:
    """A QR code as a texture (segno, when installed)."""
    try:
        import io

        import segno
    except ImportError:
        return None
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=scale, border=2)
    try:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(buf.getvalue()))
    except GLib.Error:
        return None


def share_dialog(parent: Gtk.Widget, title: str, link: str, description: str = "") -> None:
    d = compat.Dialog(title, 420, -1)
    tv = Adw.ToolbarView()
    tv.add_top_bar(Adw.HeaderBar())
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=24, margin_end=24, margin_top=12,
                  margin_bottom=24)
    tex = qr_texture(link)
    if tex is not None:
        pic = Gtk.Picture.new_for_paintable(tex)
        pic.set_size_request(240, 240)
        pic.set_can_shrink(False)
        pic.add_css_class("qr-code")
        pic.set_halign(Gtk.Align.CENTER)
        box.append(pic)
    else:
        box.append(widgets.label(_("Install “segno” to show a QR code"), ["dim-label"], 0.5, wrap=True))
    if description:
        box.append(widgets.label(description, ["dim-label"], 0.5, wrap=True))
    lbl = Gtk.Label(label=link, selectable=True, wrap=True, css_classes=["fingerprint", "caption"])
    lbl.set_wrap_mode(Pango.WrapMode.CHAR)
    box.append(lbl)
    copy = Gtk.Button(label=_("Copy link"), css_classes=["pill"], halign=Gtk.Align.CENTER)
    copy.connect("clicked", lambda b: widgets.copy_text(b, link))
    box.append(copy)
    tv.set_content(box)
    d.set_child(tv)
    d.present(parent)


TRUST_CHOICES = (("verified", "VERIFIED", "Verified", "You compared the fingerprint"),
                 ("trusted", "TRUSTED", "Trusted", "Messages go to this device"),
                 ("distrusted", "DISTRUSTED", "Distrusted", "This device gets no messages"))


class DevicesDialog(BaseDialog):
    """Devices and keys of one address.  For our own address: this device's fingerprint and QR code, and our
    other devices (which can be removed)."""

    def __init__(self, window, acc: AccountItem, jid: str):
        name = window.core.names.contact_name(acc.id, jid) if jid != acc.jid else _("Your devices")
        super().__init__(window, name, height=640, width=560)
        self.acc = acc
        self.jid = jid
        self.own = jid == acc.jid
        self.omemo_group = self.group(_("OMEMO devices"),
                                      _("Compare fingerprints with what the other person's app shows, in person "
                                        "or on a call, then mark the device as verified"))
        self.omemo_rows: list[Gtk.Widget] = []
        omemo_ok = self.core.omemo is not None and self.core.omemo.available
        if not omemo_ok:
            self.omemo_group.set_description(_("Encryption support is not installed yet"))
            install = Adw.ButtonRow(title=_("Install encryption support…")) if hasattr(Adw, "ButtonRow") else \
                Adw.ActionRow(title=_("Install encryption support…"), activatable=True)
            install.connect("activated", lambda *_: window.app.install_encryption(self.dialog))
            self.omemo_group.add(install)
        self.verify_group = self.group(_("Verify with a link"))
        self.verify_group.set_visible(omemo_ok)
        entry = Adw.EntryRow(title=_("Paste an xmpp: link with fingerprints, or scan one"), show_apply_button=True)
        entry.connect("apply", lambda e: self._verify_link(e.get_text()))
        self.verify_group.add(entry)
        if self.own:
            share = Adw.ActionRow(title=_("Show this device's QR code"), activatable=True,
                                  subtitle=_("Scan it with your other devices to verify this one"))
            share.add_suffix(Gtk.Image.new_from_icon_name("ward-qr-symbolic"))
            share.connect("activated", lambda *_: self._share())
            self.verify_group.add(share)
        self.pgp_group = None
        if self.core.pgp is not None and self.core.pgp.enabled:
            self.pgp_group = self.group(_("OpenPGP keys"))
            self.pgp_rows: list[Gtk.Widget] = []
            if not self.own:
                imp = Gtk.Button(icon_name="document-open-symbolic", css_classes=["flat"],
                                 tooltip_text=_("Import their key from a file (for example exported from GPGMan)"))
                imp.connect("clicked", lambda *_: self._import_pgp())
                self.pgp_group.set_header_suffix(imp)
        if self.own and omemo_ok:
            tools = self.group(_("Manage"))
            rm = Adw.ActionRow(title=_("Remove inactive devices"), activatable=True,
                               subtitle=_("Devices that have not been used for a long time"))
            rm.connect("activated", lambda *_: self._remove_inactive())
            tools.add(rm)
            exp = Adw.ActionRow(title=_("Export trust decisions…"), activatable=True,
                                subtitle=_("A passphrase-protected file to move them to another computer"))
            exp.connect("activated", lambda *_: self._export())
            tools.add(exp)
            imp = Adw.ActionRow(title=_("Import trust decisions…"), activatable=True)
            imp.connect("activated", lambda *_: self._import())
            tools.add(imp)
        self.refresh()

    # ------------------------------------------------------------------ loading
    def refresh(self) -> None:
        widgets.run(self.toasts, self._load(), on_error=lambda e: self.omemo_group.set_description(str(e)))

    async def _load(self) -> None:
        for r in self.omemo_rows:
            self.omemo_group.remove(r)
        self.omemo_rows = []
        if self.core.omemo is not None and self.core.omemo.available:
            s = self.core.sessions.get(self.acc.id)
            if s is not None and s.online and self.core.omemo.ready(s):
                devices = await self.core.omemo.devices(s, self.jid)
                for d in sorted(devices, key=lambda d: (not d.this_device, d.inactive, d.device_id)):
                    row = self._device_row(d, s)
                    self.omemo_group.add(row)
                    self.omemo_rows.append(row)
                if not devices:
                    row = Adw.ActionRow(title=_("No OMEMO devices"),
                                        subtitle=_("Their app doesn't support OMEMO, or they never used it"))
                    self.omemo_group.add(row)
                    self.omemo_rows.append(row)
            else:
                row = Adw.ActionRow(title=_("Connect to see devices"))
                self.omemo_group.add(row)
                self.omemo_rows.append(row)
        if self.pgp_group is not None:
            for r in self.pgp_rows:
                self.pgp_group.remove(r)
            self.pgp_rows = []
            if self.own:
                fp = self.core.pgp.own_key(self.acc.id)
                row = Adw.ActionRow(title=_("Your key") if fp else _("No key chosen"),
                                    subtitle=_("Choose or create one in Preferences > Accounts"))
                if fp:
                    row.set_subtitle(" ".join(fp[i:i + 4] for i in range(0, len(fp), 4)))
                    row.add_css_class("property")
                self.pgp_group.add(row)
                self.pgp_rows.append(row)
            else:
                keys = await self.core.pgp.keys(self.acc.id, self.jid)
                for k in keys:
                    row = self._pgp_row(k)
                    self.pgp_group.add(row)
                    self.pgp_rows.append(row)
                if not keys:
                    row = Adw.ActionRow(title=_("No OpenPGP key"),
                                        subtitle=_("They have not published one; you can import a key file"))
                    self.pgp_group.add(row)
                    self.pgp_rows.append(row)

    def _device_row(self, d: DeviceItem, session) -> Gtk.Widget:
        title = d.label or (_("This device") if d.this_device else _("Device %d") % d.device_id)
        sub = [d.versions] if d.versions else []
        if d.this_device:
            sub.append(_("this device"))
        elif d.inactive:
            sub.append(_("inactive"))
        if d.last_used:
            sub.append(_("last used %s") % widgets.relative_seen(d.last_used))
        row = Adw.ExpanderRow(title=widgets.escape(title), subtitle=widgets.escape(" · ".join(sub)))
        if not d.this_device:
            row.add_suffix(widgets.trust_badge(d.trust))
        fp_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12, margin_end=12,
                         margin_top=8, margin_bottom=8)
        fp_box.append(widgets.fingerprint_widget(d.fingerprint))
        if not d.this_device:
            buttons = Gtk.Box(spacing=6, halign=Gtk.Align.END)
            for key, level, label, tip in TRUST_CHOICES:
                b = Gtk.ToggleButton(label=_(label), active=d.trust == key, tooltip_text=_(tip))
                b.connect("clicked", lambda _b, lv=level, dev=d: self._set_trust(session, dev, lv))
                buttons.append(b)
            if self.own:
                rm = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text=_("Remove this device"),
                                css_classes=["flat"])
                rm.connect("clicked", lambda *_a, dev=d: self._remove_device(session, dev))
                buttons.append(rm)
            fp_box.append(buttons)
        else:
            lbl = Adw.EntryRow(title=_("Name of this device"), text=d.label, show_apply_button=True)
            lbl.connect("apply", lambda e: widgets.run(self.toasts, self.core.omemo.set_label(session, e.get_text())))
            lb = Gtk.ListBox(css_classes=["boxed-list"])
            lb.append(lbl)
            fp_box.append(lb)
        holder = Gtk.ListBoxRow(activatable=False)
        holder.set_child(fp_box)
        row.add_row(holder)
        row.set_expanded(d.this_device or d.trust == "undecided")
        return row

    def _pgp_row(self, k: DeviceItem) -> Gtk.Widget:
        row = Adw.ExpanderRow(title=widgets.escape(k.label or _("OpenPGP key")),
                              subtitle=_("published") if k.active else _("not published (imported)"))
        row.add_suffix(widgets.trust_badge(k.trust))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12, margin_end=12, margin_top=8,
                      margin_bottom=8)
        box.append(widgets.fingerprint_widget(k.fingerprint, 4))
        buttons = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        for key, _lv, label, tip in TRUST_CHOICES:
            b = Gtk.ToggleButton(label=_(label), active=k.trust == key, tooltip_text=_(tip))
            b.connect("clicked", lambda _b, t=key, fp=k.fingerprint: self._set_pgp_trust(fp, t))
            buttons.append(b)
        box.append(buttons)
        holder = Gtk.ListBoxRow(activatable=False)
        holder.set_child(box)
        row.add_row(holder)
        return row

    # ------------------------------------------------------------------ actions
    def _set_trust(self, session, d: DeviceItem, level: str) -> None:
        async def go():
            await self.core.omemo.set_trust(session, self.jid, d.device_id, level)
            for chat in self.core.chats.for_account(self.acc.id):
                self.core.e2e.refresh(chat)
        widgets.run(self.toasts, go(), done=lambda _r: self.refresh())

    def _set_pgp_trust(self, fp: str, trust: str) -> None:
        widgets.run(self.toasts, self.core.pgp.set_trust(self.acc.id, self.jid, fp, trust),
                    done=lambda _r: self.refresh())

    def _remove_device(self, session, d: DeviceItem) -> None:
        compat.alert(self.toasts, _("Remove this device?"),
                     _("It stops getting messages. If it is still in use, it adds itself again when it goes online."),
                     [("cancel", _("Cancel"), ""), ("remove", _("Remove"), "destructive")],
                     lambda r: r == "remove" and widgets.run(self.toasts, self.core.omemo.remove_own_device(
                         session, d.device_id), done=lambda _r: self.refresh()))

    def _remove_inactive(self) -> None:
        async def go():
            s = online_session(self.core, self.acc)
            return await self.core.omemo.remove_inactive_own_devices(s)
        widgets.run(self.toasts, go(), done=lambda n: (self.toasts.add_toast(Adw.Toast(
            title=_("Removed %d devices") % n)), self.refresh()))

    def _verify_link(self, text: str) -> None:
        u = urimod.parse_xmpp(text.strip())
        if u is None or not u.omemo:
            self.error(_("This link has no fingerprints"))
            return

        async def go():
            s = online_session(self.core, self.acc)
            return await self.core.omemo.verify_uri(s, u)

        def done(res):
            ok, bad = res
            if bad:
                self.error(_("%d fingerprints did not match! Someone may be pretending to be this person.") % bad)
            else:
                self.toasts.add_toast(Adw.Toast(title=_("Verified %d devices") % ok))
            self.refresh()
        widgets.run(self.toasts, go(), done=done)

    def _share(self) -> None:
        async def go():
            s = online_session(self.core, self.acc)
            return await self.core.omemo.verification_uri(s)
        widgets.run(self.toasts, go(), done=lambda link: share_dialog(self.toasts, _("Verify this device"), link))

    def _import_pgp(self) -> None:
        dialog = Gtk.FileDialog(title=_("Import an OpenPGP key"))

        def done(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            path = f.get_path() if f is not None else None
            if not path:
                return
            with open(path, "rb") as fh:
                data = fh.read()
            widgets.run(self.toasts, self.core.pgp.add_contact_key(self.acc.id, self.jid, data),
                        done=lambda fps: (self.toasts.add_toast(Adw.Toast(title=_("Imported %d keys") % len(fps))),
                                          self.refresh()))
        dialog.open(self.window, None, done)

    def _export(self) -> None:
        pw = Adw.PasswordEntryRow(title=_("Passphrase"))
        box = Gtk.ListBox(css_classes=["boxed-list"])
        box.append(pw)

        def go(resp):
            if resp != "export" or not pw.get_text():
                return

            async def job():
                return await self.core.omemo.export_trust(pw.get_text())

            def save(blob):
                dialog = Gtk.FileDialog(initial_name="ward-trust.wardtrust")

                def done(d, res):
                    try:
                        f = d.save_finish(res)
                    except GLib.Error:
                        return
                    if f is not None and f.get_path():
                        with open(f.get_path(), "wb") as fh:
                            fh.write(blob)
                        self.toasts.add_toast(Adw.Toast(title=_("Exported")))
                dialog.save(self.window, None, done)
            widgets.run(self.toasts, job(), done=save)
        compat.alert(self.toasts, _("Export trust decisions"), _("Choose a passphrase for the file"),
                     [("cancel", _("Cancel"), ""), ("export", _("Export"), "suggested")], go, extra=box)

    def _import(self) -> None:
        dialog = Gtk.FileDialog(title=_("Import trust decisions"))

        def opened(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            if f is None or not f.get_path():
                return
            with open(f.get_path(), "rb") as fh:
                blob = fh.read()
            pw = Adw.PasswordEntryRow(title=_("Passphrase"))
            box = Gtk.ListBox(css_classes=["boxed-list"])
            box.append(pw)
            compat.alert(self.toasts, _("Import trust decisions"), _("Enter the file's passphrase"),
                         [("cancel", _("Cancel"), ""), ("import", _("Import"), "suggested")],
                         lambda r: r == "import" and widgets.run(
                             self.toasts, self.core.omemo.import_trust(blob, pw.get_text()),
                             done=lambda n: (self.toasts.add_toast(Adw.Toast(title=_("Imported %d decisions") % n)),
                                             self.refresh())), extra=box)
        dialog.open(self.window, None, opened)
