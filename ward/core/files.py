"""Files: HTTP upload (XEP-0363), encrypted files (XEP-0454 aesgcm://), metadata (XEP-0446/0447 with an
out-of-band link XEP-0066 for older clients), downloads, thumbnails and the cache.

Encryption and decryption stream through the worker pool, so a 1 GB file never sits in memory or blocks the
interface.  In encrypted chats only the aesgcm:// link is sent, inside the encrypted message: no metadata goes
out in the clear."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import mimetypes
import os
import secrets as pysecrets
import shutil
import tempfile
import time
from xml.etree import ElementTree as ET

from gi.repository import GObject

from .. import aio, paths, util
from ..i18n import _
from . import http, images, stanzas
from . import proxy as proxymod
from .chats import insert_message_job
from .models import STATUS_FAILED, STATUS_PENDING, AttachmentItem, ChatItem, MessageItem
from .uri import build_aesgcm, parse_aesgcm

log = logging.getLogger("ward.files")

NS_UPLOAD = "urn:xmpp:http:upload:0"
TAG = 16
CHUNK = 256 * 1024
ALLOWED_PUT_HEADERS = {"authorization", "cookie", "expires"}
LARGE = 100 * 1000 * 1000


class UploadError(Exception):
    pass


def encrypt_file(src: str, dst: str, transfer: http.Transfer | None = None) -> tuple[bytes, bytes]:
    """AES-256-GCM with a 12-byte IV; the 16-byte tag is appended, as aesgcm:// expects."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key, iv = pysecrets.token_bytes(32), pysecrets.token_bytes(12)
    enc = Cipher(algorithms.AES(key), modes.GCM(iv)).encryptor()
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            chunk = fin.read(CHUNK)
            if not chunk:
                break
            fout.write(enc.update(chunk))
            if transfer is not None and transfer.cancel.is_set():
                raise http.Cancelled()
        fout.write(enc.finalize() + enc.tag)
    return key, iv


def decrypt_file(src: str, dst: str, key: bytes, iv: bytes) -> None:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    size = os.path.getsize(src)
    if size < TAG:
        raise ValueError("encrypted file too short")
    with open(src, "rb") as fin:
        fin.seek(size - TAG)
        tag = fin.read(TAG)
        fin.seek(0)
        dec = Cipher(algorithms.AES(key), modes.GCM(iv, tag)).decryptor()
        remaining = size - TAG
        with open(dst + ".part", "wb") as fout:
            while remaining > 0:
                chunk = fin.read(min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                fout.write(dec.update(chunk))
            fout.write(dec.finalize())   # raises InvalidTag when the file was tampered with
    os.replace(dst + ".part", dst)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(name: str) -> str:
    name = os.path.basename(name or "").strip().replace("\x00", "")
    name = "".join(c for c in name if c not in '/\\:*?"<>|' and ord(c) >= 32)
    return name[:120] or "file"


class FileService(GObject.Object):
    __gsignals__ = {"progress": (GObject.SignalFlags.RUN_LAST, None, (object,))}

    def __init__(self, core):
        super().__init__()
        self.core = core
        self.transfers: dict[int, http.Transfer] = {}

    # ------------------------------------------------------------------ small helpers
    @staticmethod
    def guess_mime(name: str) -> str:
        base = name.split("?", 1)[0].split("#", 1)[0]
        mime, _enc = mimetypes.guess_type(base)
        if mime:
            return mime
        ext = os.path.splitext(base)[1].lower()
        return {".opus": "audio/ogg", ".oga": "audio/ogg", ".m4a": "audio/mp4", ".webp": "image/webp",
                ".heic": "image/heic", ".mkv": "video/x-matroska"}.get(ext, "application/octet-stream")

    async def store_thumbnail(self, message_id: int, index: int, data: bytes) -> None:
        """A thumbnail that came with the message (XEP-0264): re-encoded before it is shown."""
        d = paths.ensure(paths.thumbnails_cache())
        raw = os.path.join(d, "in-%d-%d" % (message_id, index))
        with open(raw, "wb") as fh:
            fh.write(data[: 64 * 1024])
        clean = await aio.run_in_thread(images.thumbnail, raw, 256)
        os.unlink(raw)
        if not clean:
            return
        p = os.path.join(d, "%d-%d.img" % (message_id, index))
        with open(p, "wb") as fh:
            fh.write(clean)
        rows = await self.core.db.fetchall("SELECT id FROM attachments WHERE message_id = ? ORDER BY id",
                                           (message_id,))
        if index < len(rows):
            await self.core.db.execute("UPDATE attachments SET thumbnail = ? WHERE id = ?", (p, rows[index]["id"]))

    def forget(self, local_paths: list[str]) -> None:
        cache = os.path.realpath(paths.cache_dir())
        for p in local_paths:
            if p and os.path.realpath(p).startswith(cache + os.sep):
                with contextlib.suppress(OSError):
                    os.unlink(p)

    async def _proxy(self, account, url: str) -> proxymod.Proxy | None:
        prox = proxymod.from_options({**account.options, "proxy": account.option("proxy")})
        if prox == "system":
            return await proxymod.system_proxy(url)
        return prox if isinstance(prox, proxymod.Proxy) else None

    # ------------------------------------------------------------------ uploading
    async def request_slot(self, session, name: str, size: int, mime: str) -> tuple[str, dict, str]:
        from .disco import DiscoService

        svc = DiscoService.upload_service(session)
        if svc is None:
            raise UploadError(_("Your server does not support sending files"))
        jid, limit = svc
        if limit and size > limit:
            raise UploadError(_("The file is too big: your server accepts up to %s") % util.human_size(limit))
        req = ET.Element("{%s}request" % NS_UPLOAD, {"filename": name, "size": str(size), "content-type": mime})
        res = await session.iq("get", jid, req)
        slot = res.find("{%s}slot" % NS_UPLOAD)
        put = slot.find("{%s}put" % NS_UPLOAD) if slot is not None else None
        get = slot.find("{%s}get" % NS_UPLOAD) if slot is not None else None
        if put is None or get is None:
            raise UploadError(_("The server sent an invalid upload slot"))
        headers = {}
        for h in put.findall("{%s}header" % NS_UPLOAD):
            n = (h.get("name") or "").strip()
            if n.lower() in ALLOWED_PUT_HEADERS and h.text:
                headers[n] = h.text.replace("\n", " ").strip()
        return put.get("url", ""), headers, get.get("url", "")

    async def send_file(self, chat: ChatItem, path: str, caption: str = "") -> MessageItem:
        session = self.core.messaging.session_for(chat)
        acc = session.account
        await self.core.chats.open_messages(chat)
        name = safe_name(os.path.basename(path))
        mime = self.guess_mime(name)
        strip = self.core.settings["privacy.strip_metadata"]
        upload_path, temporary = await aio.run_in_thread(images.prepare_for_upload, path,
                                                         acc.option("image_quality"), strip)
        if temporary and upload_path.endswith(".jpg") and not name.lower().endswith((".jpg", ".jpeg")):
            name = os.path.splitext(name)[0] + ".jpg"
            mime = "image/jpeg"
        size = os.path.getsize(upload_path)
        if size > LARGE and not acc.option("allow_large_uploads"):
            raise UploadError(_("Files larger than 100 MB are turned off for this account (Preferences > Accounts)"))
        img_mime, w, h = await aio.run_in_thread(images.probe, upload_path)
        # keep our own copy, so the message keeps working after the original moves
        local = os.path.join(paths.ensure(paths.files_cache()), "%s-%s" % (util.random_id(8), name))
        await aio.run_in_thread(shutil.copyfile, upload_path, local)
        thumb_path = ""
        if img_mime:
            data = await aio.run_in_thread(images.thumbnail, local, 400)
            if data:
                thumb_path = local + ".thumb"
                with open(thumb_path, "wb") as fh:
                    fh.write(data)
        digest = await aio.run_in_thread(sha256_file, local)
        msg_id = util.random_id(18)
        values = {"chat_id": chat.id, "origin_id": msg_id, "msg_id": msg_id, "direction": 1,
                  "timestamp": int(time.time() * 1000), "sender": self.core.my_nick(chat) if chat.is_group
                  else acc.jid, "sender_jid": acc.jid, "body": caption, "is_markdown": 1,
                  "kind": "file" if not caption else "text", "status": STATUS_PENDING, "extra": {}}
        att = {"url": "", "name": name, "mime": img_mime or mime, "size": size, "width": w or None,
               "height": h or None, "sha256": digest, "local_path": local, "thumbnail": thumb_path,
               "state": "uploading"}
        mid = await self.core.db.run(lambda conn: insert_message_job(conn, values, [att]))
        msg = await self.core.chats.reload_message(chat, mid)
        self.core.chats.show_message(chat, msg)
        if temporary:
            with contextlib.suppress(OSError):
                os.unlink(upload_path)
        aio.spawn(self._upload_and_send(session, chat, msg))
        return msg

    async def _upload_and_send(self, session, chat: ChatItem, msg: MessageItem) -> None:
        att = msg.attachments[0]
        transfer = http.Transfer(total=att.size)
        self.transfers[att.id] = transfer
        loop = aio.get_loop()

        def progress(done, total):
            loop.call_soon_threadsafe(self._progress, att, done, total)

        transfer.on_progress = progress
        tmp = None
        try:
            state, _reason = self.core.e2e.describe(chat)
            encrypt = state in ("omemo", "omemo-legacy", "pgp")
            src = att.local_path
            key = iv = b""
            if encrypt:
                fd, tmp = tempfile.mkstemp(prefix="ward-up-")
                os.close(fd)
                key, iv = await aio.run_in_thread(encrypt_file, src, tmp, transfer)
                src = tmp
            size = os.path.getsize(src)
            transfer.total = size
            put, headers, get = await self.request_slot(session, att.name, size,
                                                        "application/octet-stream" if encrypt else att.mime)
            headers["Content-Type"] = "application/octet-stream" if encrypt else att.mime
            prox = await self._proxy(session.account, put)
            allow_http = bool(os.environ.get("WARD_ALLOW_HTTP"))
            resp = await aio.run_in_thread(lambda: http.request("PUT", put, headers, body_file=src, prox=prox,
                                                                timeout=60, transfer=transfer, allow_http=allow_http))
            if resp.status not in (200, 201):
                raise UploadError(_("Upload failed (HTTP %d)") % resp.status)
            url = build_aesgcm(get, iv, key) if encrypt else get
            att.update(url=url, state="done")
            await self.core.db.execute("UPDATE attachments SET url = ?, state = 'done', aes_key = ?, aes_iv = ? "
                                       "WHERE id = ?", (url, key.hex(), iv.hex(), att.id))
            el = self._file_message(session, chat, msg, att, url, encrypt)
            await self.core.messaging._send_element(session, chat, el, msg)
        except http.Cancelled:
            att.update(state="failed", error=_("Cancelled"))
            await self.core.db.execute("UPDATE attachments SET state = 'failed' WHERE id = ?", (att.id,))
            await self.core.chats.update_message_fields(msg.id, status=STATUS_FAILED, error=_("Cancelled"))
            msg.update(status=STATUS_FAILED, error=_("Cancelled"))
        except Exception as e:
            log.info("upload failed: %s", e)
            att.update(state="failed", error=str(e))
            await self.core.db.execute("UPDATE attachments SET state = 'failed' WHERE id = ?", (att.id,))
            await self.core.chats.update_message_fields(msg.id, status=STATUS_FAILED, error=str(e))
            msg.update(status=STATUS_FAILED, error=str(e))
        finally:
            self.transfers.pop(att.id, None)
            if tmp:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    def _file_message(self, session, chat: ChatItem, msg: MessageItem, att: AttachmentItem, url: str,
                      encrypted: bool) -> ET.Element:
        group = chat.type == "groupchat"
        el = session.new_message(chat.jid, "groupchat" if group else "chat", msg.msg_id)
        body = url if not msg.body else "%s\n%s" % (msg.body, url)
        stanzas.set_body(el, url if not msg.body else body)
        stanzas.add_origin_id(el, msg.msg_id)
        if not group:
            stanzas.add_receipt_request(el)
        stanzas.add_markable(el)
        stanzas.add_hint(el, "store")
        if not encrypted:
            stanzas.add_oob(el, url)
            thumb = None
            if att.thumbnail and os.path.exists(att.thumbnail):
                small = images.thumbnail(att.thumbnail, 64)
                thumb = small if small and len(small) < 6000 else None
            stanzas.add_file_sharing(el, stanzas.FileInfo(url=url, name=att.name, mime=att.mime, size=att.size,
                                                          width=att.width or None, height=att.height or None,
                                                          sha256=att.sha256, thumbnail=thumb))
        return el

    def _progress(self, att: AttachmentItem, done: int, total: int) -> None:
        att.progress = done / total if total else 0.0
        self.emit("progress", att)

    def cancel(self, att: AttachmentItem) -> None:
        t = self.transfers.get(att.id)
        if t is not None:
            t.cancel.set()

    # ------------------------------------------------------------------ downloading
    async def download(self, chat: ChatItem, msg: MessageItem, att: AttachmentItem, max_bytes: int | None = None
                       ) -> str | None:
        if att.local_path and os.path.exists(att.local_path):
            return att.local_path
        if att.state == "downloading":
            return None
        acc = self.core.accounts.get(chat.account_id)
        link = parse_aesgcm(att.url)
        https_url = link.https_url if link else att.url
        key = bytes.fromhex(att.aes_key) if att.aes_key else (link.key if link else b"")
        iv = bytes.fromhex(att.aes_iv) if att.aes_iv else (link.iv if link else b"")
        d = paths.ensure(paths.files_cache())
        dest = os.path.join(d, "%d-%s" % (att.id, safe_name(att.name or https_url.rsplit("/", 1)[-1])))
        transfer = http.Transfer(total=att.size or 0)
        loop = aio.get_loop()
        transfer.on_progress = lambda done, total: loop.call_soon_threadsafe(self._progress, att, done, total)
        self.transfers[att.id] = transfer
        att.update(state="downloading", error="")
        tmp = dest + ".enc" if key else dest
        try:
            prox = await self._proxy(acc, https_url) if acc else None
            allow_http = bool(os.environ.get("WARD_ALLOW_HTTP"))
            resp = await aio.run_in_thread(lambda: http.request("GET", https_url, prox=prox, timeout=60,
                                                                max_bytes=max_bytes, out_file=tmp, transfer=transfer,
                                                                allow_http=allow_http))
            if resp.status != 200:
                raise http.HttpError(_("Download failed (HTTP %d)") % resp.status, resp.status)
            if key:
                await aio.run_in_thread(decrypt_file, tmp, dest, key, iv)
                os.unlink(tmp)
            if att.sha256:
                got = await aio.run_in_thread(sha256_file, dest)
                if got != att.sha256:
                    os.unlink(dest)
                    raise ValueError(_("The file does not match its checksum"))
            mime = att.mime
            img_mime, w, h = await aio.run_in_thread(images.probe, dest)
            if img_mime:
                mime = img_mime
            thumb = ""
            if img_mime:
                data = await aio.run_in_thread(images.thumbnail, dest, 400)
                if data:
                    thumb = dest + ".thumb"
                    with open(thumb, "wb") as fh:
                        fh.write(data)
            size = os.path.getsize(dest)
            await self.core.db.execute("UPDATE attachments SET local_path = ?, state = 'done', mime = ?, size = ?, "
                                       "width = ?, height = ?, thumbnail = CASE WHEN ? != '' THEN ? ELSE thumbnail "
                                       "END WHERE id = ?", (dest, mime, size, w or att.width, h or att.height, thumb,
                                                            thumb, att.id))
            att.update(local_path=dest, state="done", mime=mime, size=size, width=w or att.width,
                       height=h or att.height, thumbnail=thumb or att.thumbnail)
            self._sync_attachment(chat, msg, att)
            aio.spawn(self.enforce_limits())
            return dest
        except Exception as e:
            log.info("download of %s failed: %s", https_url, e)
            att.update(state="failed", error=str(e))
            self._sync_attachment(chat, msg, att)
            for p in (tmp, dest + ".part", tmp + ".part"):
                with contextlib.suppress(OSError):
                    os.unlink(p)
            return None
        finally:
            self.transfers.pop(att.id, None)

    def _sync_attachment(self, chat: ChatItem, msg: MessageItem, att: AttachmentItem) -> None:
        """An auto-download may have started before the chat loaded its own copy of the message."""
        ml = self.core.chats.lists.get(chat.id)
        cur = ml.get(msg.id) if ml is not None else None
        if cur is None or cur is msg:
            msg.attachments_rev += 1
            return
        for other in cur.attachments:
            if other.id == att.id:
                other.update(local_path=att.local_path, state=att.state, mime=att.mime, size=att.size,
                             width=att.width, height=att.height, thumbnail=att.thumbnail, error=att.error)
                cur.attachments_rev += 1
                break

    async def auto_download(self, chat: ChatItem, msg: MessageItem) -> None:
        """Media and other files from contacts, each up to the account's limit; nothing from strangers."""
        if not msg.attachments:
            return
        acc = self.core.accounts.get(chat.account_id)
        if acc is None:
            return
        limit = int(acc.option("autodownload_mb") or 0) * 1000 * 1000
        if limit <= 0:
            return
        if chat.is_request and not self.core.settings["privacy.stranger_media"]:
            return
        if chat.type == "chat" and not msg.outgoing:
            c = self.core.roster.contacts.get(chat.account_id, chat.jid)
            if (c is None or not c.in_roster) and not self.core.settings["privacy.stranger_media"]:
                return
        for att in msg.attachments:
            if att.local_path and os.path.exists(att.local_path):
                continue
            if att.state in ("downloading", "uploading"):
                continue
            if att.size and att.size > limit:
                continue
            aio.spawn(self.download(chat, msg, att, max_bytes=limit))

    # ------------------------------------------------------------------ cache
    @staticmethod
    def _usage(directory: str) -> tuple[int, list[tuple[float, int, str]]]:
        total, files = 0, []
        for root, _dirs, names in os.walk(directory):
            for n in names:
                p = os.path.join(root, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                total += st.st_size
                files.append((st.st_atime, st.st_size, p))
        return total, files

    async def cache_usage(self) -> int:
        total, _files = await aio.run_in_thread(self._usage, paths.cache_dir())
        return total

    async def clear_cache(self) -> None:
        def job():
            for d in (paths.files_cache(), paths.thumbnails_cache(), paths.previews_cache()):
                shutil.rmtree(d, ignore_errors=True)
        await aio.run_in_thread(job)
        await self.core.db.execute("UPDATE attachments SET local_path = '', thumbnail = '', state = 'remote' "
                                   "WHERE state = 'done' AND url != ''")

    async def enforce_limits(self) -> None:
        limit = int(self.core.settings["storage.cache_limit"] or 0)
        days = int(self.core.settings["storage.delete_files_after"] or 0)
        total, files = await aio.run_in_thread(self._usage, paths.files_cache())
        now = time.time()
        files.sort()
        removed = []
        for atime, size, p in files:
            too_old = days and now - atime > days * 86400
            if too_old or (limit and total > limit):
                with contextlib.suppress(OSError):
                    os.unlink(p)
                    total -= size
                    removed.append(p)
        if removed:
            await self.core.db.executemany("UPDATE attachments SET local_path = '', state = 'remote' WHERE "
                                           "local_path = ? AND url != ''", [(p,) for p in removed])
