"""Trust levels, fingerprints, verification links and the trust-list backup.

Stored trust level names (python-omemo "custom trust levels"):
    VERIFIED    fingerprint confirmed in person or by QR code            -> encrypt
    TRUSTED     approved by the user without verification                -> encrypt
    BLIND       trusted automatically by blind trust before verification -> encrypt
    UNDECIDED   new, waiting for the user                                -> no
    DISTRUSTED  never                                                    -> no (and its messages are flagged)
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

VERIFIED, TRUSTED, BLIND, UNDECIDED, DISTRUSTED = "VERIFIED", "TRUSTED", "BLIND", "UNDECIDED", "DISTRUSTED"
ENCRYPT_TO = {VERIFIED, TRUSTED, BLIND}

# what the interface shows
UI = {VERIFIED: "verified", TRUSTED: "trusted", BLIND: "trusted", UNDECIDED: "undecided", DISTRUSTED: "distrusted"}


def fingerprint(identity_key: bytes) -> str:
    """64 hex characters of the Curve25519 identity key: the format Conversations, Gajim and Dino show."""
    import xeddsa

    return xeddsa.ed25519_pub_to_curve25519_pub(identity_key).hex()


def groups(fp: str, size: int = 8) -> list[str]:
    return [fp[i:i + size] for i in range(0, len(fp), size)]


def pretty(fp: str) -> str:
    return " ".join(groups(fp))


# --------------------------------------------------------------------------------------------- backups
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
MAGIC = "ward-backup-1"


def _kdf(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(passphrase.encode("utf-8"))


def seal(payload: dict, passphrase: str, kind: str) -> bytes:
    """A passphrase-protected file: scrypt key derivation, AES-256-GCM, JSON inside."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, nonce = os.urandom(16), os.urandom(12)
    data = json.dumps(payload).encode()
    ct = AESGCM(_kdf(passphrase, salt)).encrypt(nonce, data, (MAGIC + kind).encode())
    return json.dumps({"format": MAGIC, "kind": kind, "salt": base64.b64encode(salt).decode(),
                       "nonce": base64.b64encode(nonce).decode(), "data": base64.b64encode(ct).decode()},
                      indent=1).encode()


class BackupError(Exception):
    pass


def unseal(blob: bytes, passphrase: str, kind: str) -> dict:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        outer = json.loads(blob)
        if outer.get("format") != MAGIC or outer.get("kind") != kind:
            raise BackupError("this is not a %s file" % kind)
        salt = base64.b64decode(outer["salt"])
        nonce = base64.b64decode(outer["nonce"])
        ct = base64.b64decode(outer["data"])
    except (ValueError, KeyError, TypeError) as e:
        raise BackupError("the file is damaged") from e
    try:
        data = AESGCM(_kdf(passphrase, salt)).decrypt(nonce, ct, (MAGIC + kind).encode())
    except InvalidTag:
        raise BackupError("wrong passphrase") from None
    return json.loads(data)


@dataclass
class TrustEntry:
    jid: str
    fingerprint: str
    level: str


def export_trust(entries: list[TrustEntry], passphrase: str) -> bytes:
    """Only decisions worth keeping: verified and distrusted keys."""
    items = [{"jid": e.jid, "fp": e.fingerprint, "level": e.level} for e in entries
             if e.level in (VERIFIED, DISTRUSTED)]
    return seal({"entries": items}, passphrase, "trust")


def import_trust(blob: bytes, passphrase: str) -> list[TrustEntry]:
    data = unseal(blob, passphrase, "trust")
    out = []
    for d in data.get("entries", []):
        if d.get("level") in (VERIFIED, DISTRUSTED) and isinstance(d.get("fp"), str) and len(d["fp"]) == 64:
            out.append(TrustEntry(str(d.get("jid", "")).lower(), d["fp"].lower(), d["level"]))
    return out
