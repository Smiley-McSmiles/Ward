"""Passwords: in the system keyring (libsecret: GNOME Keyring, KWallet, KeePassXC...) by default.

Systems without a Secret Service (a bare window manager, many BSD desktops) can choose per account to store the
password in a file readable only by the user (with a clear warning), or to be asked at every start.  The test-suite
uses WARD_SECRETS=memory."""

from __future__ import annotations

import logging
import os

from .. import APP_ID, aio, paths, util

log = logging.getLogger("ward.secrets")


class KeyringUnavailable(Exception):
    pass


_memory: dict[str, str] = {}
_schema = None


def _secret():
    import gi

    gi.require_version("Secret", "1")
    from gi.repository import Secret

    return Secret


def _get_schema():
    global _schema
    if _schema is None:
        Secret = _secret()
        _schema = Secret.Schema.new(APP_ID + ".Account", Secret.SchemaFlags.NONE,
                                    {"jid": Secret.SchemaAttributeType.STRING,
                                     "kind": Secret.SchemaAttributeType.STRING})
    return _schema


def _forced() -> str:
    return os.environ.get("WARD_SECRETS", "")


def keyring_present() -> bool:
    if _forced() == "memory":
        return False
    try:
        _secret()
        return True
    except (ImportError, ValueError):
        return False


async def _keyring_lookup(jid: str, kind: str) -> str | None:
    Secret = _secret()
    try:
        return await aio.gio_call(Secret.password_lookup, Secret.password_lookup_finish, _get_schema(),
                                  {"jid": jid, "kind": kind}, None)
    except Exception as e:  # no Secret Service on the bus, locked and dismissed...
        raise KeyringUnavailable(str(e)) from e


async def _keyring_store(jid: str, kind: str, secret: str, label: str) -> None:
    Secret = _secret()
    try:
        ok = await aio.gio_call(Secret.password_store, Secret.password_store_finish, _get_schema(),
                                {"jid": jid, "kind": kind}, Secret.COLLECTION_DEFAULT, label, secret, None)
    except Exception as e:
        raise KeyringUnavailable(str(e)) from e
    if not ok:
        raise KeyringUnavailable("the keyring refused to store the password")


async def _keyring_clear(jid: str, kind: str) -> None:
    Secret = _secret()
    try:
        await aio.gio_call(Secret.password_clear, Secret.password_clear_finish, _get_schema(),
                           {"jid": jid, "kind": kind}, None)
    except Exception as e:
        log.debug("could not clear keyring entry: %s", e)


def _file_load() -> dict:
    data = util.read_json(paths.secrets_file(), {}) or {}
    return data if isinstance(data, dict) else {}


def _key(jid: str, kind: str) -> str:
    return "%s\x00%s" % (jid, kind)


async def lookup(jid: str, mode: str, kind: str = "password") -> str | None:
    """mode is the account's store_password option: keyring | file | never."""
    if _forced() == "memory" or mode == "never":
        return _memory.get(_key(jid, kind))
    if mode == "file":
        return _file_load().get(_key(jid, kind).replace("\x00", " "))
    return await _keyring_lookup(jid, kind)


async def store(jid: str, secret: str, mode: str, kind: str = "password") -> None:
    if _forced() == "memory" or mode == "never":
        _memory[_key(jid, kind)] = secret
        return
    if mode == "file":
        data = _file_load()
        data[_key(jid, kind).replace("\x00", " ")] = secret
        util.write_json(paths.secrets_file(), data, mode=0o600)
        return
    await _keyring_store(jid, kind, secret, "Ward: %s" % jid if kind == "password" else "Ward: %s (%s)"
                         % (jid, kind))


async def clear(jid: str, kind: str = "password") -> None:
    """Forget a secret everywhere it might be."""
    _memory.pop(_key(jid, kind), None)
    data = _file_load()
    if data.pop(_key(jid, kind).replace("\x00", " "), None) is not None:
        util.write_json(paths.secrets_file(), data, mode=0o600)
    if keyring_present():
        await _keyring_clear(jid, kind)


def remember_for_session(jid: str, secret: str, kind: str = "password") -> None:
    _memory[_key(jid, kind)] = secret
