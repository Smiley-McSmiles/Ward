"""GnuPG engine: runs the gpg program (always in the worker pool, never on the main thread).

Ward shares its keyring with GPGMan (https://github.com/Smiley-McSmiles/GPGMan): when "Share GPGMan's keyring" is
on, the gpg binary and GnuPG home chosen in GPGMan (~/.config/gpgman/settings.json) are used, so a key generated
or imported in GPGMan can be picked in Ward right away, and keys Ward imports show up in GPGMan.  Keys exported by
GPGMan (ASCII-armored .asc files, public or secret) import directly, too.

Passphrases are asked by gpg-agent's own pinentry, so Ward never sees them; only the test-suite uses passphrase-less
keys.  Ward decides itself which keys to trust (its own trust table, like OMEMO), so gpg runs with
--trust-model always and never refuses a key because of web-of-trust settings."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field


def gpgman_settings_file() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    # GPGMan itself always uses ~/.config/gpgman; honour XDG_CONFIG_HOME too for sandboxed setups
    for cand in (os.path.join(os.path.expanduser("~"), ".config", "gpgman", "settings.json"),
                 os.path.join(base, "gpgman", "settings.json")):
        if os.path.isfile(cand):
            return cand
    return os.path.join(os.path.expanduser("~"), ".config", "gpgman", "settings.json")


def gpgman_settings() -> tuple[str | None, str | None]:
    """(gpg_binary, gnupg_home) as configured in GPGMan, each None when unset or unusable."""
    try:
        with open(gpgman_settings_file(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    binary = data.get("gpg_binary")
    home = data.get("gnupg_home")
    if not (isinstance(binary, str) and os.path.isfile(binary) and os.access(binary, os.X_OK)):
        binary = None
    if not (isinstance(home, str) and home.strip()):
        home = None
    else:
        home = os.path.abspath(os.path.expanduser(home))
    return binary, home


def gpgman_installed() -> str | None:
    return shutil.which("gpgman")


def find_gpg(preferred: str = "", use_gpgman: bool = True) -> str | None:
    """The gpg program to use: an explicit setting, then GPGMan's choice, then gpg/gpg2 on PATH."""
    if preferred and os.path.isfile(preferred) and os.access(preferred, os.X_OK):
        return preferred
    if use_gpgman:
        binary, _home = gpgman_settings()
        if binary:
            return binary
    for name in ("gpg", "gpg2"):
        p = shutil.which(name)
        if p:
            return p
    for p in ("/usr/local/bin/gpg", "/usr/pkg/bin/gpg2", "/usr/pkg/bin/gpg", "/usr/bin/gpg"):
        if os.access(p, os.X_OK):
            return p
    return None


class GpgError(Exception):
    pass


@dataclass
class Key:
    fingerprint: str
    uids: list[str] = field(default_factory=list)
    created: int = 0
    expires: int = 0
    secret: bool = False
    can_encrypt: bool = False
    can_sign: bool = False
    revoked: bool = False
    expired: bool = False
    algo: str = ""

    @property
    def xmpp_jids(self) -> list[str]:
        """JIDs from user ids like 'xmpp:alice@example.org' (XEP-0373) or 'Alice <alice@example.org>'."""
        out = []
        for u in self.uids:
            m = re.search(r"xmpp:([^\s>]+)", u)
            if m:
                out.append(m.group(1).lower())
        return out

    @property
    def usable(self) -> bool:
        return not self.revoked and not self.expired

    @property
    def name(self) -> str:
        return self.uids[0] if self.uids else self.fingerprint[-16:]


@dataclass
class Decrypted:
    plaintext: bytes
    signer: str = ""           # primary key fingerprint of a valid signature, "" when unsigned or bad
    signature_ok: bool = False
    key_missing: bool = False  # signed with a key we do not have


ALGOS = {"1": "RSA", "16": "ElGamal", "17": "DSA", "18": "ECDH", "19": "ECDSA", "22": "EdDSA"}


class GnuPG:
    def __init__(self, binary: str | None = None, home: str | None = None, use_gpgman: bool = True):
        self.binary = find_gpg(binary or "", use_gpgman)
        _b, gpgman_home = gpgman_settings() if use_gpgman else (None, None)
        self.home = home or gpgman_home or None
        self.source = "gpgman" if (use_gpgman and gpgman_home and not home) else ("custom" if home else "default")

    @property
    def available(self) -> bool:
        return self.binary is not None

    def _run(self, args: list[str], data: bytes | None = None, check: bool = True, timeout: float = 120,
             status: bool = False) -> tuple[int, bytes, str]:
        if self.binary is None:
            raise GpgError("gpg is not installed")
        cmd = [self.binary, "--batch", "--no-tty", "--yes"]
        if self.home:
            cmd += ["--homedir", self.home]
        if status:
            cmd += ["--status-fd", "2"]
        cmd += args
        env = dict(os.environ)
        env.setdefault("LC_ALL", "C")
        try:
            p = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise GpgError(str(e)) from e
        err = p.stderr.decode("utf-8", "replace")
        if check and p.returncode != 0:
            msg = [x for x in err.splitlines() if x and not x.startswith("[GNUPG:]")]
            raise GpgError(msg[-1] if msg else "gpg failed (%d)" % p.returncode)
        return p.returncode, p.stdout, err

    def version(self) -> str:
        _c, out, _e = self._run(["--version"], check=False)
        first = out.decode("utf-8", "replace").splitlines()[:1]
        return first[0] if first else ""

    # ------------------------------------------------------------------ keys
    def list_keys(self, secret: bool = False) -> list[Key]:
        args = ["--with-colons", "--fixed-list-mode", "--with-fingerprint", "--list-secret-keys" if secret
                else "--list-keys"]
        code, out, _e = self._run(args, check=False)
        if code not in (0, 2):
            return []
        secrets_ = set()
        if not secret:
            secrets_ = {k.fingerprint for k in self.list_keys(secret=True)}
        keys: list[Key] = []
        cur: Key | None = None
        in_sub = False
        for line in out.decode("utf-8", "replace").splitlines():
            f = line.split(":")
            rec = f[0]
            if rec in ("pub", "sec"):
                cur = Key("", secret=rec == "sec")
                validity = f[1] if len(f) > 1 else ""
                cur.revoked = validity == "r"
                cur.expired = validity == "e"
                cur.algo = ALGOS.get(f[3] if len(f) > 3 else "", "")
                cur.created = int(f[5]) if len(f) > 5 and f[5].isdigit() else 0
                cur.expires = int(f[6]) if len(f) > 6 and f[6].isdigit() else 0
                caps = f[11] if len(f) > 11 else ""
                cur.can_encrypt = "E" in caps
                cur.can_sign = "S" in caps
                keys.append(cur)
                in_sub = False
            elif rec in ("sub", "ssb"):
                in_sub = True
            elif rec == "fpr" and cur is not None and not in_sub and not cur.fingerprint:
                cur.fingerprint = f[9].upper() if len(f) > 9 else ""
            elif rec == "uid" and cur is not None:
                uid = f[9] if len(f) > 9 else ""
                uid = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), uid)
                if uid and uid not in cur.uids:
                    cur.uids.append(uid)
        for k in keys:
            if k.fingerprint in secrets_:
                k.secret = True
        return [k for k in keys if k.fingerprint]

    def get_key(self, fingerprint: str, secret: bool = False) -> Key | None:
        fp = fingerprint.upper()
        for k in self.list_keys(secret):
            if k.fingerprint == fp:
                return k
        return None

    def import_keys(self, data: bytes) -> list[str]:
        """Import armored or binary keys (public or secret, e.g. a GPGMan .asc export).  Returns fingerprints."""
        _c, _out, err = self._run(["--import", "--import-options", "import-clean"], data, check=False, status=True)
        fps = []
        for line in err.splitlines():
            m = re.match(r"\[GNUPG:\] IMPORT_OK \d+ ([0-9A-Fa-f]{40,64})", line)
            if m and m.group(1).upper() not in fps:
                fps.append(m.group(1).upper())
        if not fps:
            reasons = [x for x in err.splitlines() if x and not x.startswith("[GNUPG:]")]
            raise GpgError(reasons[-1] if reasons else "no key found in the data")
        return fps

    def import_file(self, path: str) -> list[str]:
        with open(path, "rb") as fh:
            return self.import_keys(fh.read())

    def export_public(self, fingerprint: str, armor: bool = False) -> bytes:
        args = (["--armor"] if armor else []) + ["--export-options", "export-minimal", "--export", fingerprint]
        _c, out, _e = self._run(args)
        if not out:
            raise GpgError("no such key: %s" % fingerprint)
        return out

    def export_secret(self, fingerprint: str, armor: bool = True, passphrase: str | None = None) -> bytes:
        """The secret key (with its subkeys), for a backup or moving to another computer.

        With a passphrase, gpg runs in loopback mode so it never opens its own pinentry window; without one it
        may ask for the key's passphrase through gpg-agent, as every other secret-key operation in Ward does."""
        args = ["--armor"] if armor else []
        data = None
        if passphrase:
            args += ["--pinentry-mode", "loopback", "--passphrase-fd", "0"]
            data = passphrase.encode("utf-8")
        args += ["--export-secret-keys", fingerprint]
        _c, out, _e = self._run(args, data=data)
        if not out:
            raise GpgError("no such key: %s" % fingerprint)
        return out

    def generate(self, uid: str, expire: str = "never", passphrase: str | None = None) -> str:
        """A new Ed25519/Cv25519 key.  gpg-agent asks for the passphrase unless one is given (tests use '').
        Returns its fingerprint."""
        extra = ["--pinentry-mode", "loopback", "--passphrase", passphrase] if passphrase is not None else []
        _c, _out, err = self._run([*extra, "--quick-generate-key", uid, "default", "default", expire], status=True,
                                  timeout=600)
        for line in err.splitlines():
            m = re.match(r"\[GNUPG:\] KEY_CREATED \w ([0-9A-F]{40,64})", line)
            if m:
                return m.group(1)
        keys = [k for k in self.list_keys(True) if uid in k.uids]
        if keys:
            return keys[-1].fingerprint
        raise GpgError("key generation failed")

    # ------------------------------------------------------------------ crypto
    def encrypt_sign(self, data: bytes, recipients: list[str], signer: str) -> bytes:
        args = ["--trust-model", "always", "--sign", "--encrypt", "--local-user", signer]
        for r in recipients:
            args += ["--recipient", r]
        _c, out, _e = self._run([*args, "--output", "-"], data, status=True)
        return out

    def decrypt_verify(self, data: bytes) -> Decrypted:
        code, out, err = self._run(["--trust-model", "always", "--decrypt", "--output", "-"], data, check=False,
                                   status=True)
        res = Decrypted(out)
        decrypted = False
        for line in err.splitlines():
            if line.startswith("[GNUPG:] DECRYPTION_OKAY"):
                decrypted = True
            elif line.startswith("[GNUPG:] VALIDSIG "):
                parts = line.split()
                # VALIDSIG <sig fpr> ... <primary key fpr> is the last field (when present)
                res.signer = (parts[-1] if len(parts) >= 12 else parts[2]).upper()
                res.signature_ok = True
            elif line.startswith("[GNUPG:] NO_PUBKEY") or line.startswith("[GNUPG:] ERRSIG"):
                res.key_missing = True
            elif line.startswith("[GNUPG:] BADSIG"):
                res.signature_ok = False
                res.signer = ""
        if not decrypted:
            reasons = [x for x in err.splitlines() if x and not x.startswith("[GNUPG:]")]
            raise GpgError(reasons[-1] if reasons else "decryption failed (%d)" % code)
        return res

    def kill_agent(self) -> None:
        conf = shutil.which("gpgconf", path=os.path.dirname(self.binary or "")) or shutil.which("gpgconf")
        if conf:
            args = [conf] + (["--homedir", self.home] if self.home else []) + ["--kill", "gpg-agent"]
            subprocess.run(args, capture_output=True, timeout=10)


def temporary_home() -> str:
    d = tempfile.mkdtemp(prefix="ward-gpg-")
    os.chmod(d, 0o700)
    return d
