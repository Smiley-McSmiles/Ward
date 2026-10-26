"""Which incoming attachments are fetched automatically, and the size limit for each kind."""

from __future__ import annotations

from types import SimpleNamespace

from ward import schema
from ward.core import files
from ward.core.files import FileService
from ward.core.models import AttachmentItem


class FakeAccount:
    def __init__(self, **options):
        self.options = options

    def option(self, key):
        return self.options.get(key, schema.account_default(key))


def test_auto_download_files_limit_defaults_to_one_mb() -> None:
    spec = schema.ACCOUNT_SPECS["autodownload_files_mb"]
    assert spec.type == "int" and spec.default == 1 and spec.unit == "MB"


def test_auto_download_uses_media_and_file_limits(monkeypatch, run) -> None:
    account = FakeAccount(autodownload_mb=10, autodownload_files_mb=1)
    core = SimpleNamespace(accounts=SimpleNamespace(get=lambda _id: account),
                           settings={"privacy.stranger_media": True},
                           roster=SimpleNamespace(contacts=SimpleNamespace(get=lambda *_a: None)))
    chat = SimpleNamespace(account_id=1, is_request=False, type="chat", jid="bob@example.org")
    msg = SimpleNamespace(attachments=[
        AttachmentItem(id=1, mime="application/pdf", size=900_000),
        AttachmentItem(id=2, mime="application/pdf", size=1_100_000),
        AttachmentItem(id=3, mime="image/jpeg", size=5_000_000),
        AttachmentItem(id=4, mime="audio/ogg", size=6_000_000),
        AttachmentItem(id=5, mime="application/octet-stream", size=2_000_000),
    ])

    calls = []

    async def fake_download(_self, _chat, _msg, att, max_bytes=None):
        calls.append((att.id, max_bytes))

    pending = []
    monkeypatch.setattr(FileService, "download", fake_download)
    monkeypatch.setattr(files.aio, "spawn", pending.append)

    run(FileService(core).auto_download(chat, msg))
    for coro in pending:
        run(coro)

    assert calls == [(1, 1_000_000), (3, 10_000_000), (4, 10_000_000)]


def test_auto_download_files_off_leaves_media_alone(monkeypatch, run) -> None:
    account = FakeAccount(autodownload_mb=10, autodownload_files_mb=0)
    core = SimpleNamespace(accounts=SimpleNamespace(get=lambda _id: account),
                           settings={"privacy.stranger_media": True},
                           roster=SimpleNamespace(contacts=SimpleNamespace(get=lambda *_a: None)))
    chat = SimpleNamespace(account_id=1, is_request=False, type="chat", jid="bob@example.org")
    msg = SimpleNamespace(attachments=[
        AttachmentItem(id=1, mime="application/pdf", size=900_000),
        AttachmentItem(id=2, mime="image/jpeg", size=5_000_000),
    ])

    calls = []

    async def fake_download(_self, _chat, _msg, att, max_bytes=None):
        calls.append((att.id, max_bytes))

    pending = []
    monkeypatch.setattr(FileService, "download", fake_download)
    monkeypatch.setattr(files.aio, "spawn", pending.append)

    run(FileService(core).auto_download(chat, msg))
    for coro in pending:
        run(coro)

    assert calls == [(2, 10_000_000)]
