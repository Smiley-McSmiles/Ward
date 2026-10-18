"""Any XEP-0004 data form as native rows: registration (with CAPTCHA images), room settings and admin commands
all use this.  collect() returns the form with the user's values."""

from __future__ import annotations

from gi.repository import Adw, Gdk, GLib, Gtk

from ..core import forms
from ..i18n import _
from . import widgets


class FormView(Gtk.Box):
    __gtype_name__ = "WardFormView"

    def __init__(self, form: forms.Form, skip: set[str] | None = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.form = form
        self.skip = skip or set()
        self.getters: dict[str, callable] = {}
        group = Adw.PreferencesGroup(title=widgets.escape(form.title))
        if form.instructions:
            group.set_description(widgets.escape("\n".join(form.instructions)))
        self.append(group)
        for f in form.fields:
            if f.type == "hidden" or f.var in self.skip:
                continue
            if f.type == "fixed":
                # a fixed field starts a new section, as in Gajim and Conversations
                text = f.value or f.label
                if text:
                    group = Adw.PreferencesGroup(title=widgets.escape(text) if len(text) < 60 else "",
                                                 description="" if len(text) < 60 else widgets.escape(text))
                    self.append(group)
                continue
            for m in f.media:
                pic = self._media(m)
                if pic is not None:
                    group.add(pic)
            row = self._row(f)
            if row is not None:
                group.add(row)
        if form.reported and form.items:
            self.append(self._table(form))

    def _title(self, f: forms.Field) -> str:
        t = f.label or f.var
        return widgets.escape(t + (" *" if f.required else ""))

    def _row(self, f: forms.Field) -> Gtk.Widget | None:
        title = self._title(f)
        if f.type == "boolean":
            row = Adw.SwitchRow(title=title, active=f.bool_value)
            self.getters[f.var] = lambda r=row: ["1" if r.get_active() else "0"]
        elif f.type == "list-single" and f.options and not f.open_list:
            row = Adw.ComboRow(title=title)
            labels = [label or value for value, label in f.options]
            row.set_model(Gtk.StringList.new(labels))
            values = [v for v, _l in f.options]
            if f.value in values:
                row.set_selected(values.index(f.value))
            self.getters[f.var] = lambda r=row, vs=values: [vs[r.get_selected()]] if vs else []
        elif f.type == "list-multi" and f.options:
            row = Adw.ExpanderRow(title=title)
            checks = []
            for value, label in f.options:
                c = Adw.SwitchRow(title=widgets.escape(label or value), active=value in f.values)
                row.add_row(c)
                checks.append((value, c))
            self.getters[f.var] = lambda cs=checks: [v for v, c in cs if c.get_active()]
        elif f.type in ("text-multi", "jid-multi"):
            row = Adw.ExpanderRow(title=title, subtitle=_("One per line") if f.type == "jid-multi" else "")
            view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=6, bottom_margin=6, left_margin=8,
                                right_margin=8)
            view.get_buffer().set_text("\n".join(f.values))
            view.set_size_request(-1, 90)
            holder = Gtk.ListBoxRow(activatable=False)
            holder.set_child(view)
            row.add_row(holder)
            row.set_expanded(bool(f.values) or f.required)

            def get(v=view):
                b = v.get_buffer()
                return [x for x in b.get_text(b.get_start_iter(), b.get_end_iter(), False).splitlines() if x.strip()]
            self.getters[f.var] = get
        else:
            row = Adw.PasswordEntryRow(title=title) if f.type == "text-private" else Adw.EntryRow(title=title)
            row.set_text(f.value)
            self.getters[f.var] = lambda r=row: [r.get_text()] if r.get_text() else []
        if f.desc:
            row.set_tooltip_text(f.desc)
            if isinstance(row, (Adw.ActionRow, Adw.ExpanderRow)) and not row.get_subtitle():
                row.set_subtitle(widgets.escape(f.desc))
        return row

    @staticmethod
    def _media(m: forms.Media) -> Gtk.Widget | None:
        if not m.data:
            return None
        try:
            tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(m.data))
        except GLib.Error:
            return None
        pic = Gtk.Picture.new_for_paintable(tex)
        pic.set_can_shrink(False)
        pic.add_css_class("captcha")
        holder = Gtk.ListBoxRow(activatable=False)
        holder.set_child(pic)
        return holder

    @staticmethod
    def _table(form: forms.Form) -> Gtk.Widget:
        grid = Gtk.Grid(column_spacing=12, row_spacing=4, css_classes=["message-table"])
        for c, f in enumerate(form.reported):
            grid.attach(Gtk.Label(label=f.label or f.var, css_classes=["header"], xalign=0), c, 0, 1, 1)
        for r, item in enumerate(form.items, start=1):
            for c, f in enumerate(form.reported):
                grid.attach(Gtk.Label(label=", ".join(item.get(f.var, [])), xalign=0, selectable=True), c, r, 1, 1)
        sc = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        sc.set_child(grid)
        return sc

    def collect(self) -> forms.Form:
        out = forms.Form(type="submit", title=self.form.title)
        for f in self.form.fields:
            if f.type == "fixed":
                continue
            values = self.getters[f.var]() if f.var in self.getters else list(f.values)
            out.fields.append(forms.Field(var=f.var, type=f.type, label=f.label, required=f.required,
                                          values=values, options=list(f.options)))
        return out

    def missing(self) -> list[str]:
        return [f.label or f.var for f in self.collect().missing_required() if f.var not in self.skip]
