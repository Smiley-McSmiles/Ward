import json

import pytest

from ward.core.pgp import gnupg
from ward.core.pgp.service import build_signcrypt, parse_signcrypt

pytestmark = pytest.mark.skipif(gnupg.find_gpg(use_gpgman=False) is None, reason="gpg not installed")


@pytest.fixture
def homes(tmp_path):
    made = []

    def make(name):
        d = tmp_path / name
        d.mkdir(mode=0o700)
        g = gnupg.GnuPG(home=str(d), use_gpgman=False)
        made.append(g)
        return g

    yield make
    for g in made:
        g.kill_agent()


def test_keys_encrypt_sign_roundtrip(homes):
    alice, bob = homes("a"), homes("b")
    fa = alice.generate("xmpp:alice@example.org", passphrase="")
    fb = bob.generate("xmpp:bob@example.org", passphrase="")
    assert len(fa) == 40
    keys = alice.list_keys(secret=True)
    assert [k.fingerprint for k in keys] == [fa] and keys[0].xmpp_jids == ["alice@example.org"]
    # exchange public keys (binary, as XEP-0373 publishes them; armored, as GPGMan exports them)
    assert bob.import_keys(alice.export_public(fa)) == [fa]
    assert alice.import_keys(bob.export_public(fb, armor=True)) == [fb]
    data = alice.encrypt_sign(b"hello bob", [fb, fa], fa)
    res = bob.decrypt_verify(data)
    assert res.plaintext == b"hello bob" and res.signature_ok and res.signer == fa
    # alice can read her own message (her other devices too)
    assert alice.decrypt_verify(data).plaintext == b"hello bob"
    with pytest.raises(gnupg.GpgError):
        bob.import_keys(b"not a key")


def test_shares_gpgmans_keyring(homes, tmp_path, monkeypatch):
    shared = homes("gpgman-home")
    fp = shared.generate("xmpp:me@example.org", passphrase="")
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / ".config" / "gpgman"
    cfg.mkdir(parents=True)
    (cfg / "settings.json").write_text(json.dumps({"gpg_binary": gnupg.find_gpg(use_gpgman=False),
                                                   "gnupg_home": shared.home}))
    ward = gnupg.GnuPG(use_gpgman=True)
    assert ward.home == shared.home and ward.source == "gpgman"
    assert [k.fingerprint for k in ward.list_keys(secret=True)] == [fp]
    # turning the option off ignores GPGMan's folder
    assert gnupg.GnuPG(use_gpgman=False).home is None
    # a broken GPGMan config is ignored, not fatal
    (cfg / "settings.json").write_text("{nope")
    assert gnupg.gpgman_settings() == (None, None)


def test_gpgman_export_imports(homes, tmp_path):
    """A secret key exported by GPGMan (armored .asc) becomes usable in Ward."""
    gpgman, ward = homes("g"), homes("w")
    fp = gpgman.generate("Me <me@example.org>", passphrase="")
    _c, armored, _e = gpgman._run(["--pinentry-mode", "loopback", "--passphrase", "", "--armor",
                                   "--export-secret-keys", fp])
    f = tmp_path / "me-secret.asc"
    f.write_bytes(armored)
    assert ward.import_file(str(f)) == [fp]
    assert ward.get_key(fp, secret=True) is not None


def test_export_secret_key(homes):
    """An exported secret key imports together with its private part and keeps the fingerprint."""
    old, new = homes("old"), homes("new")
    fp = old.generate("xmpp:me@example.org", passphrase="")
    blob = old.export_secret(fp)
    assert b"BEGIN PGP PRIVATE KEY BLOCK" in blob
    assert new.import_keys(blob) == [fp]
    assert new.get_key(fp, secret=True) is not None


def test_export_protected_secret_key(homes):
    """A protected key exports with its passphrase in loopback mode (no pinentry window)."""
    old, new = homes("old-protected"), homes("new-protected")
    fp = old.generate("xmpp:me@example.org", passphrase="hunter2")
    blob = old.export_secret(fp, passphrase="hunter2")
    assert new.import_keys(blob) == [fp]


def test_signcrypt():
    from xml.etree import ElementTree as ET

    body = ET.Element("{jabber:client}body")
    body.text = "hi"
    data = build_signcrypt([body], ["bob@example.org"])
    content = parse_signcrypt(data, {"bob@example.org"})
    assert content[0].text == "hi"
    with pytest.raises(ValueError):
        parse_signcrypt(data, {"mallory@example.org"})
    with pytest.raises(ValueError):
        parse_signcrypt(b"<!DOCTYPE x [<!ENTITY a 'b'>]><x/>", {"bob@example.org"})
