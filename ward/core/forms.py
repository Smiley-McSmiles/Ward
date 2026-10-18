"""XEP-0004 Data Forms as plain Python objects, parsed from and serialised to ElementTree.

Registration (XEP-0077), room configuration (XEP-0045), ad-hoc commands (XEP-0050) and server administration
(XEP-0133) all send these forms; the interface renders any of them as native preference rows.  We parse the XML
ourselves instead of using slixmpp's Form class, whose API differs between slixmpp versions.  CAPTCHA images
(XEP-0221 media with XEP-0231 bits of binary) are collected too."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

NS = "jabber:x:data"
NS_MEDIA = "urn:xmpp:media-element"
NS_BOB = "urn:xmpp:bob"
NS_VALIDATE = "http://jabber.org/protocol/xdata-validate"

FIELD_TYPES = ("boolean", "fixed", "hidden", "jid-multi", "jid-single", "list-multi", "list-single", "text-multi",
               "text-private", "text-single")


@dataclass
class Media:
    mime: str
    uri: str
    data: bytes | None = None         # filled from a <data xmlns='urn:xmpp:bob'/> in the same stanza
    width: int = 0
    height: int = 0


@dataclass
class Field:
    var: str = ""
    type: str = "text-single"
    label: str = ""
    desc: str = ""
    required: bool = False
    values: list[str] = field(default_factory=list)
    options: list[tuple[str, str]] = field(default_factory=list)  # (value, label)
    media: list[Media] = field(default_factory=list)
    open_list: bool = False           # xdata-validate <open/>: free text allowed in a list-single

    @property
    def value(self) -> str:
        return self.values[0] if self.values else ""

    @value.setter
    def value(self, v: str) -> None:
        self.values = [v] if v != "" else []

    @property
    def bool_value(self) -> bool:
        return self.value.lower() in ("1", "true")

    @property
    def multi(self) -> bool:
        return self.type in ("jid-multi", "list-multi", "text-multi")


@dataclass
class Form:
    type: str = "form"               # form | submit | cancel | result
    title: str = ""
    instructions: list[str] = field(default_factory=list)
    fields: list[Field] = field(default_factory=list)
    reported: list[Field] = field(default_factory=list)
    items: list[dict[str, list[str]]] = field(default_factory=list)

    @property
    def form_type(self) -> str:
        f = self.get("FORM_TYPE")
        return f.value if f else ""

    def get(self, var: str) -> Field | None:
        for f in self.fields:
            if f.var == var:
                return f
        return None

    def value(self, var: str, default: str = "") -> str:
        f = self.get(var)
        return f.value if f and f.values else default

    def values(self, var: str) -> list[str]:
        f = self.get(var)
        return list(f.values) if f else []

    def set(self, var: str, value) -> None:
        f = self.get(var)
        if f is None:
            f = Field(var=var)
            self.fields.append(f)
        if isinstance(value, bool):
            f.values = ["1" if value else "0"]
        elif isinstance(value, (list, tuple)):
            f.values = [str(v) for v in value]
        else:
            f.values = [str(value)] if value is not None and str(value) != "" else []

    def missing_required(self) -> list[Field]:
        return [f for f in self.fields if f.required and f.type not in ("fixed", "hidden", "boolean")
                and not any(v.strip() for v in f.values)]


def _text(el: ET.Element | None) -> str:
    return (el.text or "") if el is not None else ""


def _parse_field(el: ET.Element, bob: dict[str, tuple[str, bytes]]) -> Field:
    f = Field(var=el.get("var", ""), type=el.get("type") or "text-single", label=el.get("label", ""))
    if f.type not in FIELD_TYPES:
        f.type = "text-single"
    f.desc = _text(el.find("{%s}desc" % NS))
    f.required = el.find("{%s}required" % NS) is not None
    f.values = [_text(v) for v in el.findall("{%s}value" % NS)]
    for opt in el.findall("{%s}option" % NS):
        v = _text(opt.find("{%s}value" % NS))
        f.options.append((v, opt.get("label") or v))
    val = el.find("{%s}validate" % NS_VALIDATE)
    if val is not None and val.find("{%s}open" % NS_VALIDATE) is not None:
        f.open_list = True
    media = el.find("{%s}media" % NS_MEDIA)
    if media is not None:
        for u in media.findall("{%s}uri" % NS_MEDIA):
            uri = _text(u).strip()
            m = Media(mime=u.get("type", ""), uri=uri, width=int(media.get("width") or 0),
                      height=int(media.get("height") or 0))
            if uri.startswith("cid:") and uri[4:] in bob:
                m.data = bob[uri[4:]][1]
            f.media.append(m)
    return f


def bob_data(stanza: ET.Element) -> dict[str, tuple[str, bytes]]:
    out = {}
    for d in stanza.iter("{%s}data" % NS_BOB):
        try:
            out[d.get("cid", "")] = (d.get("type", ""), base64.b64decode((d.text or "").strip()))
        except (ValueError, TypeError):
            continue
    return out


def parse(el: ET.Element, stanza: ET.Element | None = None) -> Form:
    """Parse an <x xmlns='jabber:x:data'/> element.  Pass the whole stanza to resolve CAPTCHA cid: images."""
    bob = bob_data(stanza) if stanza is not None else {}
    form = Form(type=el.get("type", "form"), title=_text(el.find("{%s}title" % NS)))
    form.instructions = [_text(i) for i in el.findall("{%s}instructions" % NS) if _text(i)]
    form.fields = [_parse_field(f, bob) for f in el.findall("{%s}field" % NS)]
    rep = el.find("{%s}reported" % NS)
    if rep is not None:
        form.reported = [_parse_field(f, bob) for f in rep.findall("{%s}field" % NS)]
    for item in el.findall("{%s}item" % NS):
        row: dict[str, list[str]] = {}
        for f in item.findall("{%s}field" % NS):
            row[f.get("var", "")] = [_text(v) for v in f.findall("{%s}value" % NS)]
        form.items.append(row)
    return form


def find(parent: ET.Element, stanza: ET.Element | None = None) -> Form | None:
    el = parent.find("{%s}x" % NS)
    return parse(el, stanza) if el is not None else None


def to_element(form: Form, type_: str = "submit", include_all: bool = False) -> ET.Element:
    """Serialise for sending.  A submitted form carries var and values only (fixed fields are dropped)."""
    x = ET.Element("{%s}x" % NS, {"type": type_})
    if type_ in ("form", "result"):
        if form.title:
            ET.SubElement(x, "{%s}title" % NS).text = form.title
        for i in form.instructions:
            ET.SubElement(x, "{%s}instructions" % NS).text = i
    if type_ == "cancel":
        return x
    for f in form.fields:
        if type_ == "submit" and (f.type == "fixed" or not f.var):
            continue
        attrs = {"var": f.var} if f.var else {}
        if type_ != "submit" or include_all or f.var == "FORM_TYPE":
            if f.type != "text-single" or type_ != "submit":
                attrs["type"] = f.type
            if f.label and type_ != "submit":
                attrs["label"] = f.label
        fe = ET.SubElement(x, "{%s}field" % NS, attrs)
        values = f.values
        if f.type == "boolean" and values:
            values = ["1" if f.bool_value else "0"]
        for v in values:
            ET.SubElement(fe, "{%s}value" % NS).text = v
        if type_ != "submit":
            for value, label in f.options:
                o = ET.SubElement(fe, "{%s}option" % NS, {"label": label} if label else {})
                ET.SubElement(o, "{%s}value" % NS).text = value
    return x


def simple(form_type: str, values: dict, type_: str = "submit") -> ET.Element:
    """Build a submit form from a dict, e.g. publish-options or MAM queries."""
    f = Form(type=type_)
    f.fields.append(Field(var="FORM_TYPE", type="hidden", values=[form_type]))
    for k, v in values.items():
        f.set(k, v)
    return to_element(f, type_)
