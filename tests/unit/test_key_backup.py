"""The passphrase-protected files used to move OMEMO identities and trust decisions between installs."""

import pytest

from ward.core.omemo import trust


def test_device_backup_roundtrip():
    payload = {"jid": "me@example.org", "store": {"identity_key": '"abc"', "sessions/me": "{}"}}
    blob = trust.seal(payload, "correct horse", "device")
    assert b"correct horse" not in blob
    assert trust.unseal(blob, "correct horse", "device") == payload


def test_backup_rejects_the_wrong_passphrase_kind_or_format():
    blob = trust.seal({"entries": []}, "pw", "trust")
    with pytest.raises(trust.BackupError):
        trust.unseal(blob, "wrong", "trust")
    with pytest.raises(trust.BackupError):
        trust.unseal(blob, "pw", "device")
    with pytest.raises(trust.BackupError):
        trust.unseal(b"not json", "pw", "trust")


def test_private_key_files_are_only_readable_by_the_user(tmp_path):
    from ward.gui import widgets

    path = tmp_path / "backup"
    path.write_bytes(b"older file with a loose mode")
    path.chmod(0o644)
    widgets.write_private(str(path), b"secret")
    assert path.read_bytes() == b"secret"
    assert path.stat().st_mode & 0o777 == 0o600
