"""XEP-0392 Consistent Color Generation (version 0.8): a person gets the same colour in Ward as in Conversations
and Dino.  The hue comes from SHA-1 of the nickname or JID; the colour is HSLuv with saturation 100 and a
lightness that we raise on dark backgrounds so names stay readable (the XEP allows adapting lightness)."""

from __future__ import annotations

import hashlib
import math

_M = ((3.240969941904521, -1.537383177570093, -0.498610760293),
      (-0.96924363628087, 1.87596750150772, 0.041555057407175),
      (0.055630079696993, -0.20397695888897, 1.056971514242878))
_REF_U = 0.19783000664283
_REF_V = 0.46831999493879
_KAPPA = 903.2962962
_EPSILON = 0.0088564516


def hue_angle(text: str) -> float:
    digest = hashlib.sha1(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:2], "little") / 65536 * 360


def _bounds(lightness: float):
    out = []
    sub1 = ((lightness + 16) ** 3) / 1560896
    sub2 = sub1 if sub1 > _EPSILON else lightness / _KAPPA
    for m1, m2, m3 in _M:
        for t in (0, 1):
            top1 = (284517 * m1 - 94839 * m3) * sub2
            top2 = (838422 * m3 + 769860 * m2 + 731718 * m1) * lightness * sub2 - 769860 * t * lightness
            bottom = (632260 * m3 - 126452 * m2) * sub2 + 126452 * t
            out.append((top1 / bottom, top2 / bottom))
    return out


def _max_chroma(lightness: float, hue: float) -> float:
    hrad = hue / 360 * math.pi * 2
    best = math.inf
    for slope, intercept in _bounds(lightness):
        denom = math.sin(hrad) - slope * math.cos(hrad)
        if denom == 0:
            continue
        length = intercept / denom
        if length >= 0:
            best = min(best, length)
    return best


def _from_linear(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def hsluv_to_rgb(hue: float, saturation: float, lightness: float) -> tuple[float, float, float]:
    if lightness > 99.9999999:
        return (1.0, 1.0, 1.0)
    if lightness < 1e-8:
        return (0.0, 0.0, 0.0)
    chroma = _max_chroma(lightness, hue) / 100 * saturation
    hrad = hue / 360 * 2 * math.pi
    u, v = math.cos(hrad) * chroma, math.sin(hrad) * chroma
    y = ((lightness + 16) / 116) ** 3 if lightness > 8 else lightness / _KAPPA
    var_u = u / (13 * lightness) + _REF_U
    var_v = v / (13 * lightness) + _REF_V
    x = 0 - (9 * y * var_u) / ((var_u - 4) * var_v - var_u * var_v)
    z = (9 * y - 15 * var_v * y - var_v * x) / (3 * var_v)
    rgb = tuple(max(0.0, min(1.0, _from_linear(r0 * x + r1 * y + r2 * z))) for r0, r1, r2 in _M)
    return rgb  # type: ignore[return-value]


def rgb(text: str, dark: bool = False) -> tuple[float, float, float]:
    return hsluv_to_rgb(hue_angle(text), 100, 68 if dark else 50)


def hex_color(text: str, dark: bool = False) -> str:
    r, g, b = rgb(text, dark)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def initials(name: str) -> str:
    """One or two letters for avatar placeholders."""
    words = [w for w in (name or "").replace("@", " ").replace(".", " ").replace("_", " ").split() if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][0].upper()
    return (words[0][0] + words[1][0]).upper()
