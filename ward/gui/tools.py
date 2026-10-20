"""Developer tools (Preferences > Advanced): the XML console and a service discovery browser."""

from __future__ import annotations

from xml.etree import ElementTree as ET

from gi.repository import Adw, GLib, Gtk, Pango

from ..core.models import AccountItem
from ..i18n import _
from . import widgets
from .dialogs import BaseDialog, online_session

MAX_LINES = 4000


class XmlConsole(Adw.Window):
    """Every stanza of one account, in and out (passwords and tokens masked), and a box to send raw XML."""

    def __init__(self, window):
        super().__init__(title=_("XML console"), default_width=760, default_height=640, transient_for=window)
        if window.get_application() is not None:
            self.set_application(window.get_application())
        self.core = window.core
        self.session = None
        self._handler = 0
        self.paused = False
        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        tv.add_top_bar(header)
        self.accounts = [a for a in self.core.accounts.list()]
        self.acc_drop = Gtk.DropDown.new_from_strings([a.jid for a in self.accounts] or [_("No accounts")])
        self.acc_drop.connect("notify::selected", lambda *_: self._attach())
        header.pack_start(self.acc_drop)
        pause = Gtk.ToggleButton(icon_name="media-playback-pause-symbolic", tooltip_text=_("Pause"))
        pause.connect("toggled", lambda b: setattr(self, "paused", b.get_active()))
        header.pack_end(pause)
        clear = Gtk.Button(icon_name="edit-clear-all-symbolic", tooltip_text=_("Clear"))
        clear.connect("clicked", lambda *_: self.buffer.set_text(""))
        header.pack_end(clear)
        self.filter = Gtk.SearchEntry(placeholder_text=_("Only stanzas containing…"))
        header.set_title_widget(self.filter)

        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self.view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=8,
                                 right_margin=8, top_margin=8, css_classes=["xml-console"])
        self.buffer = self.view.get_buffer()
        self.tag_in = self.buffer.create_tag("in", foreground="#1c71d8")
        self.tag_out = self.buffer.create_tag("out", foreground="#26a269")
        self.tag_head = self.buffer.create_tag("head", weight=Pango.Weight.BOLD, scale=0.85)
        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.set_child(self.view)
        self.scroller = sc
        paned.set_start_child(sc)
        send_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=8, margin_end=8,
                           margin_top=6, margin_bottom=8)
        self.input = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=6, bottom_margin=6,
                                  left_margin=6, right_margin=6, css_classes=["card"])
        isc = Gtk.ScrolledWindow(min_content_height=90)
        isc.set_child(self.input)
        send_box.append(isc)
        row = Gtk.Box(spacing=6)
        for label, snippet in (("iq", '<iq type="get" to="" id="console1">\n  <query xmlns="http://jabber.org/'
                                      'protocol/disco#info"/>\n</iq>'),
                               ("message", '<message to="" type="chat">\n  <body></body>\n</message>'),
                               ("presence", '<presence/>')):
            b = Gtk.Button(label=label, css_classes=["flat"])
            b.connect("clicked", lambda _b, s=snippet: self.input.get_buffer().set_text(s))
            row.append(b)
        row.append(Gtk.Box(hexpand=True))
        send = Gtk.Button(label=_("Send"), css_classes=["suggested-action"])
        send.connect("clicked", lambda *_: self._send())
        row.append(send)
        send_box.append(row)
        paned.set_end_child(send_box)
        paned.set_resize_end_child(False)
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(paned)
        tv.set_content(self.toasts)
        self.set_content(tv)
        self.connect("close-request", self._on_close)
        self._attach()

    def _attach(self) -> None:
        self._detach()
        i = self.acc_drop.get_selected()
        if not (0 <= i < len(self.accounts)):
            return
        s = self.core.session_for(self.accounts[i])
        self.session = s
        s.console = True
        self._handler = s.connect("xml", self._on_xml)

    def _detach(self) -> None:
        if self.session is not None and self._handler:
            self.session.disconnect(self._handler)
            self.session.console = False
        self.session = None
        self._handler = 0

    def _on_close(self, *_a) -> bool:
        self._detach()
        return False

    def _on_xml(self, _s, incoming: bool, text: str) -> None:
        if self.paused:
            return
        f = self.filter.get_text().strip()
        if f and f not in text:
            return
        end = self.buffer.get_end_iter()
        stamp = GLib.DateTime.new_now_local().format("%H:%M:%S")
        self.buffer.insert_with_tags(end, "%s %s\n" % (stamp, _("received") if incoming else _("sent")),
                                     self.tag_head)
        end = self.buffer.get_end_iter()
        self.buffer.insert_with_tags(end, text + "\n\n", self.tag_in if incoming else self.tag_out)
        if self.buffer.get_line_count() > MAX_LINES:
            start = self.buffer.get_start_iter()
            cut = self.buffer.get_iter_at_line(self.buffer.get_line_count() - MAX_LINES)
            cut = cut[1] if isinstance(cut, tuple) else cut
            self.buffer.delete(start, cut)
        adj = self.scroller.get_vadjustment()
        if adj.get_value() + adj.get_page_size() >= adj.get_upper() - 60:
            GLib.idle_add(lambda: adj.set_value(adj.get_upper()) and False)

    def _send(self) -> None:
        b = self.input.get_buffer()
        text = b.get_text(b.get_start_iter(), b.get_end_iter(), False).strip()
        if not text or self.session is None:
            return
        try:
            wrapper = ET.fromstring('<w xmlns="jabber:client">%s</w>' % text)
        except ET.ParseError as e:
            self.toasts.add_toast(Adw.Toast(title=_("Not valid XML: %s") % e))
            return
        try:
            for el in list(wrapper):
                self.session.send(el)
        except Exception as e:
            self.toasts.add_toast(Adw.Toast(title=str(e)))


class DiscoBrowser(BaseDialog):
    """What a server, a component or a contact offers: identities, features and items (XEP-0030)."""

    def __init__(self, window, acc: AccountItem):
        super().__init__(window, _("Service discovery"), width=600, height=680)
        self.acc = acc
        g = self.group()
        self.jid_row = Adw.EntryRow(title=_("Address"), text=acc.jid.split("@")[-1], show_apply_button=True)
        self.jid_row.connect("apply", lambda *_: self.browse())
        g.add(self.jid_row)
        self.node_row = Adw.EntryRow(title=_("Node (optional)"), show_apply_button=True)
        self.node_row.connect("apply", lambda *_: self.browse())
        g.add(self.node_row)
        self.history: list[tuple[str, str]] = []
        back = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text=_("Back"))
        back.connect("clicked", lambda *_: self._back())
        self.header.pack_start(back)
        self.result_groups: list[Adw.PreferencesGroup] = []
        self.browse()

    def _back(self) -> None:
        if len(self.history) > 1:
            self.history.pop()
            jid, node = self.history.pop()
            self.jid_row.set_text(jid)
            self.node_row.set_text(node)
            self.browse()

    def browse(self) -> None:
        jid = self.jid_row.get_text().strip()
        node = self.node_row.get_text().strip()
        self.history.append((jid, node))

        async def go():
            s = online_session(self.core, self.acc)
            info = await self.core.disco.info(s, jid, node)
            try:
                items = await self.core.disco.items(s, jid, node)
            except Exception:
                items = []
            return info, items
        self.run(go(), self._show)

    def _show(self, res) -> None:
        info, items = res
        for g in self.result_groups:
            self.page.remove(g)
        self.result_groups = []
        ident = self.group(_("Identities"))
        self.result_groups.append(ident)
        for cat, typ, name in info.identities:
            ident.add(Adw.ActionRow(title=widgets.escape(name or "%s/%s" % (cat, typ)), subtitle="%s/%s" % (cat, typ)))
        if items:
            ig = self.group(_("Items"))
            self.result_groups.append(ig)
            for jid, node, name in items[:300]:
                row = Adw.ActionRow(title=widgets.escape(name or jid), subtitle=widgets.escape(
                    jid + (" · " + node if node else "")), activatable=True)
                row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
                row.connect("activated", lambda _r, j=jid, n=node: (self.jid_row.set_text(j), self.node_row.set_text(
                    n), self.browse()))
                ig.add(row)
        fg = self.group(_("Features"))
        self.result_groups.append(fg)
        exp = Adw.ExpanderRow(title=_("%d features") % len(info.features))
        for f in sorted(info.features):
            r = Adw.ActionRow(title=widgets.escape(f))
            r.set_title_selectable(True)
            exp.add_row(r)
        fg.add(exp)
        for form in info.forms:
            from .forms_view import FormView

            g = self.group(_("Extended information"))
            self.result_groups.append(g)
            g.add(FormView(form))

