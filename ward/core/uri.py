"""Parsing and building the links Ward handles: xmpp: URIs (RFC 5122, XEP-0147 query actions, XEP-0379/0401
invites, OMEMO verification links), aesgcm:// file links (XEP-0454) and geo: locations (RFC 5870)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

_FP_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass
class XmppUri:
    jid: str
    action: str = ""                                  # "" | message | join | roster | subscribe | register |
    params: dict[str, str] = field(default_factory=dict)  # command | disco | invite | ...
    omemo: dict[int, str] = field(default_factory=dict)   # device id -> fingerprint (hex), from omemo-sid-N=...

    @property
    def is_invite(self) -> bool:
        return "preauth" in self.params

    @property
    def is_account_invite(self) -> bool:
        """An invite to create an account (XEP-0401): register action, or roster with ibr=y."""
        return self.is_invite and (self.action == "register" or self.params.get("ibr") in ("y", "yes", "1"))


def parse_xmpp(uri: str) -> XmppUri | None:
    s = (uri or "").strip()
    if not s.lower().startswith("xmpp:"):
        return None
    s = s[5:]
    if s.startswith("//"):
        # xmpp://authority/jid form: the authority is the account to use; we let the user pick instead
        s = s[2:]
        s = s.split("/", 1)[1] if "/" in s else s
    s = s.split("#", 1)[0]
    path, _, query = s.partition("?")
    jid = unquote(path)
    if not jid or any(c.isspace() for c in jid):
        return None
    out = XmppUri(jid=jid)
    if query:
        parts = query.split(";")
        first = parts[0]
        if "=" in first:
            rest = parts
        else:
            out.action = unquote(first).lower()
            rest = parts[1:]
        for p in rest:
            if not p:
                continue
            k, _, v = p.partition("=")
            k, v = unquote(k), unquote(v)
            m = re.match(r"^omemo-sid-(\d+)$", k)
            if m:
                if _FP_RE.match(v):
                    out.omemo[int(m.group(1))] = v.lower()
                continue
            out.params[k] = v
    return out


def _q(s: str) -> str:
    return quote(s, safe="@!$&'()*+,=:")


def build_xmpp(jid: str, action: str = "", params: dict[str, str] | None = None,
               omemo: dict[int, str] | None = None) -> str:
    out = "xmpp:" + quote(jid, safe="@/!$&'()*+,=:")
    items: list[str] = []
    for k, v in (params or {}).items():
        items.append("%s=%s" % (_q(k), _q(v)))
    for dev, fp in sorted((omemo or {}).items()):
        items.append("omemo-sid-%d=%s" % (dev, fp.lower()))
    if action or items:
        out += "?" + ";".join(([action] if action else []) + items)
    return out


# --------------------------------------------------------------------------------------------- aesgcm (XEP-0454)
@dataclass
class AesgcmLink:
    https_url: str
    iv: bytes
    key: bytes


def parse_aesgcm(uri: str) -> AesgcmLink | None:
    """aesgcm://host/path#<iv><key> in hex.  The IV is 12 bytes (the standard) or 16 (older Conversations)."""
    if not uri or not uri.lower().startswith("aesgcm://"):
        return None
    base, sep, frag = uri[9:].partition("#")
    if not sep or not re.fullmatch(r"(?:[0-9a-fA-F]{2})+", frag or ""):
        return None
    raw = bytes.fromhex(frag)
    if len(raw) == 44:
        iv, key = raw[:12], raw[12:]
    elif len(raw) == 48:
        iv, key = raw[:16], raw[16:]
    else:
        return None
    return AesgcmLink("https://" + base, iv, key)


def build_aesgcm(https_url: str, iv: bytes, key: bytes) -> str:
    if not https_url.startswith("https://"):
        raise ValueError("aesgcm links need an https URL")
    return "aesgcm://" + https_url[8:] + "#" + (iv + key).hex()


# ---------------------------------------------------------------------------------------------- geo (RFC 5870)
@dataclass
class GeoUri:
    lat: float
    lon: float
    alt: float | None = None
    uncertainty: float | None = None


def parse_geo(uri: str) -> GeoUri | None:
    m = re.match(r"^geo:(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)(?:,(-?\d+(?:\.\d+)?))?((?:;[^;]*)*)$",
                 (uri or "").strip(), re.IGNORECASE)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    unc = None
    for p in (m.group(4) or "").split(";"):
        if p.lower().startswith("u="):
            try:
                unc = float(p[2:])
            except ValueError:
                pass
    return GeoUri(lat, lon, float(m.group(3)) if m.group(3) else None, unc)
