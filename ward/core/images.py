"""Images: size probing, metadata stripping, resizing for upload, thumbnails.

Pillow is used when installed; otherwise GdkPixbuf (part of every GTK installation) does the work, so photos are
always stripped of their location before they are sent.  JPEG metadata is removed losslessly when the photo needs
no rotation."""

from __future__ import annotations

import io
import logging
import os
import struct
import tempfile

log = logging.getLogger("ward.images")

QUALITY_SIDE = {"balanced": 2560, "small": 1280}


def _pil():
    try:
        from PIL import Image, ImageOps

        return Image, ImageOps
    except ImportError:
        return None, None


def _pixbuf():
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    return GdkPixbuf


def probe(path: str) -> tuple[str, int, int]:
    """(mime, width, height); zeros when it is not an image we can read."""
    Image, _ = _pil()
    if Image is not None:
        try:
            with Image.open(path) as im:
                mime = Image.MIME.get(im.format or "", "")
                w, h = im.size
                orient = (im.getexif() or {}).get(0x0112, 1)
                if orient in (5, 6, 7, 8):
                    w, h = h, w
                return mime, w, h
        except Exception:
            return "", 0, 0
    try:
        GdkPixbuf = _pixbuf()
        fmt, w, h = GdkPixbuf.Pixbuf.get_file_info(path)
        if fmt is None:
            return "", 0, 0
        mimes = fmt.get_mime_types()
        return (mimes[0] if mimes else ""), w, h
    except Exception:
        return "", 0, 0


def jpeg_orientation(data: bytes) -> int:
    """EXIF orientation of a JPEG (1 = upright)."""
    i = 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker, length = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\x00\x00":
            tiff = data[i + 10:i + 2 + length]
            if len(tiff) < 8:
                return 1
            endian = "<" if tiff[:2] == b"II" else ">"
            off = struct.unpack(endian + "I", tiff[4:8])[0]
            if off + 2 > len(tiff):
                return 1
            n = struct.unpack(endian + "H", tiff[off:off + 2])[0]
            for k in range(n):
                e = off + 2 + 12 * k
                if e + 12 > len(tiff):
                    break
                tag, _typ, _cnt = struct.unpack(endian + "HHI", tiff[e:e + 8])
                if tag == 0x0112:
                    return struct.unpack(endian + "H", tiff[e + 8:e + 10])[0]
            return 1
        if marker == 0xDA:
            break
        i += 2 + length
    return 1


def strip_jpeg(data: bytes) -> bytes:
    """Remove EXIF/XMP (APP1), IPTC (APP13) and comments from a JPEG without re-encoding it.  Keeps JFIF and
    the colour profile."""
    if data[:2] != b"\xff\xd8":
        return data
    out = bytearray(b"\xff\xd8")
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return data  # not what we expected: leave it alone (the caller re-encodes instead)
        marker = data[i + 1]
        if marker == 0xDA:   # start of scan: the rest is image data
            out += data[i:]
            return bytes(out)
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        seg = data[i:i + 2 + length]
        if marker not in (0xE1, 0xED, 0xFE):
            out += seg
        i += 2 + length
    return data


def prepare_for_upload(path: str, quality: str = "balanced", strip: bool = True) -> tuple[str, bool]:
    """Returns (path to upload, is_temporary).  Applies rotation, removes metadata, scales down for the
    "balanced" and "small" presets."""
    mime, w, h = probe(path)
    if not mime.startswith("image/") or mime in ("image/gif", "image/svg+xml"):
        return path, False
    max_side = QUALITY_SIDE.get(quality, 0)
    needs_scale = max_side and max(w, h) > max_side
    with open(path, "rb") as fh:
        data = fh.read()
    if mime == "image/jpeg" and not needs_scale:
        if not strip:
            return path, False
        if jpeg_orientation(data) == 1:
            fd, tmp = tempfile.mkstemp(suffix=".jpg")
            with os.fdopen(fd, "wb") as out:
                out.write(strip_jpeg(data))
            return tmp, True
    if not needs_scale and not strip:
        return path, False
    if not needs_scale and mime not in ("image/jpeg", "image/webp", "image/tiff", "image/heic", "image/heif"):
        return path, False   # PNG and friends rarely carry location data
    return _reencode(path, mime, max_side or max(w, h)), True


def _reencode(path: str, mime: str, max_side: int) -> str:
    Image, ImageOps = _pil()
    keep_png = mime == "image/png"
    suffix = ".png" if keep_png else ".jpg"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    if Image is not None:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((max_side, max_side))
            if keep_png:
                im.save(tmp, "PNG", optimize=True)
            else:
                im.convert("RGB").save(tmp, "JPEG", quality=85, optimize=True)
        return tmp
    GdkPixbuf = _pixbuf()
    pb = GdkPixbuf.Pixbuf.new_from_file(path)
    pb = pb.apply_embedded_orientation() or pb
    w, h = pb.get_width(), pb.get_height()
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        pb = pb.scale_simple(max(1, int(w * scale)), max(1, int(h * scale)), GdkPixbuf.InterpType.BILINEAR)
    if keep_png:
        pb.savev(tmp, "png", [], [])
    else:
        if pb.get_has_alpha():
            pb = pb.composite_color_simple(pb.get_width(), pb.get_height(), GdkPixbuf.InterpType.NEAREST, 255, 8,
                                           0xFFFFFF, 0xFFFFFF)
        pb.savev(tmp, "jpeg", ["quality"], ["85"])   # GdkPixbuf never writes the original metadata back
    return tmp


def thumbnail(path: str, max_side: int = 400, fmt: str = "jpeg") -> bytes | None:
    """A small re-encoded copy (also strips everything but pixels: safe to show from strangers)."""
    Image, ImageOps = _pil()
    try:
        if Image is not None:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((max_side, max_side))
                buf = io.BytesIO()
                if fmt == "png" or im.mode in ("RGBA", "LA", "P"):
                    im.save(buf, "PNG", optimize=True)
                else:
                    im.convert("RGB").save(buf, "JPEG", quality=80)
                return buf.getvalue()
        GdkPixbuf = _pixbuf()
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, max_side, max_side, True)
        pb = pb.apply_embedded_orientation() or pb
        ok, data = pb.save_to_bufferv("png" if pb.get_has_alpha() else "jpeg", [], [])
        return bytes(data) if ok else None
    except Exception as e:
        log.debug("no thumbnail for %s: %s", path, e)
        return None
