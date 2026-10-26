"""Which incoming attachments are fetched automatically, and the unified size limit."""

from __future__ import annotations

import json
from types import SimpleNamespace

from ward import schema
from ward.core import accounts, files
from ward.core.files import FileService
from ward.core.models import AttachmentItem


class FakeAccount:
    def __init__(self, **options):
        self.options = options

    def option(self, key):
        return self.options.get(key, schema.account_default(key))


def core_for(account):
    return SimpleNamespace(accounts=SimpleNamespace(get=lambda _id: account),
                           settings={"privacy.stranger_media": True},
                           roster=SimpleNamespace(contacts=SimpleNamespace(get=lambda *_a: None)))


def run_auto(monkeypatch, run, account, chat, msg):
    calls = []

    async def fake_download(_self, _chat, _msg, att, max_bytes=None):
        calls.append((att.id, max_bytes))

    pending = []
    monkeypatch.setattr(FileService, "download", fake_download)
    monkeypatch.setattr(files.aio, "spawn", pending.append)
    run(FileService(core_for(account)).auto_download(chat, msg))
    for coro in pending:
        run(coro)
    return calls


def test_one_auto_download_limit_defaults_to_one_mb() -> None:
    spec = schema.ACCOUNT_SPECS["autodownload_mb"]
    assert spec.type == "int" and spec.default == 1 and spec.unit == "MB"
    assert "autodownload_files_mb" not in schema.ACCOUNT_SPECS


def test_auto_download_uses_one_limit_for_all_kinds(monkeypatch, run, tmp_path) -> None:
    account = FakeAccount(autodownload_mb=10)
    chat = SimpleNamespace(account_id=1, is_request=False, type="chat", jid="bob@example.org")
    existing = tmp_path / "already.txt"
    existing.write_text("here")
    msg = SimpleNamespace(outgoing=True, attachments=[
        AttachmentItem(id=1, mime="application/pdf", size=9_000_000),
        AttachmentItem(id=2, mime="image/jpeg", size=11_000_000),
        AttachmentItem(id=3, mime="audio/ogg", size=1_000_000),
        AttachmentItem(id=4, mime="video/mp4", size=5_000_000),
        AttachmentItem(id=5, mime="text/plain", size=100, local_path=str(existing)),
        AttachmentItem(id=6, mime="application/zip", size=100, state="downloading"),
    ])

    calls = run_auto(monkeypatch, run, account, chat, msg)
    assert calls == [(1, 10_000_000), (3, 10_000_000), (4, 10_000_000)]


def test_auto_download_zero_turns_it_off(monkeypatch, run) -> None:
    account = FakeAccount(autodownload_mb=0)
    chat = SimpleNamespace(account_id=1, is_request=False, type="chat", jid="bob@example.org")
    msg = SimpleNamespace(outgoing=False, attachments=[
        AttachmentItem(id=1, mime="application/pdf", size=100),
        AttachmentItem(id=2, mime="image/jpeg", size=100),
    ])
    assert run_auto(monkeypatch, run, account, chat, msg) == []


def test_legacy_file_limit_is_migrated_to_the_unified_option() -> None:
    service = accounts.Accounts(None)

    def item(options):
        row = {"id": 1, "jid": "alice@example.org", "label": "", "color": "", "enabled": 1, "resource": "",
               "options": json.dumps(options)}
        return service._item(row)

    assert item({"autodownload_files_mb": 10}).option("autodownload_mb") == 10
    both = item({"autodownload_mb": 4, "autodownload_files_mb": 10})
    assert both.option("autodownload_mb") == 4
    assert "autodownload_files_mb" not in both.options
