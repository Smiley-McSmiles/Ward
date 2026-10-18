"""Logging.  Off by default (warnings only, to stderr); Preferences > Advanced > Debug log turns on a log file in
the state folder.  Secrets never reach the log: passwords, tokens and key material are masked by a filter."""

from __future__ import annotations

import logging
import logging.handlers
import os
import re

from . import paths

LEVELS = {"off": logging.WARNING, "normal": logging.INFO, "verbose": logging.DEBUG}

_MASKS = [
    # SASL payloads, passwords in data forms and registration, OMEMO/OpenPGP key material
    (re.compile(r"(<auth\b[^>]*>)[^<]+(</auth>)"), r"\1***\2"),
    (re.compile(r"(<response\b[^>]*>)[^<]+(</response>)"), r"\1***\2"),
    (re.compile(r"(<password>)[^<]*(</password>)"), r"\1***\2"),
    (re.compile(r"(<field[^>]*type=['\"]text-private['\"][^>]*>\s*<value>)[^<]*(</value>)"), r"\1***\2"),
    (re.compile(r"(<(?:key|payload|ik|spk|spks|pk|signedPreKeyPublic|signedPreKeySignature|identityKey|"
                r"preKeyPublic|pubkey|data|openpgp)\b[^>]*>)[^<]{16,}(</)"), r"\1***\2"),
    (re.compile(r"(aesgcm://[^#\s]+#)[0-9a-fA-F]+"), r"\1***"),
]


def mask(text: str) -> str:
    for rx, repl in _MASKS:
        text = rx.sub(repl, text)
    return text


class _MaskFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        masked = mask(msg)
        if masked != msg:
            record.msg, record.args = masked, ()
        return True


_file_handler: logging.Handler | None = None


def setup(level: str = "off", to_file: bool | None = None) -> None:
    env = os.environ.get("WARD_LOG")
    if env:
        level = {"debug": "verbose", "info": "normal", "warning": "off"}.get(env, env)
    root = logging.getLogger()
    root.setLevel(LEVELS.get(level, logging.WARNING))
    if not any(getattr(h, "_ward", False) for h in root.handlers):
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname).1s %(name)s: %(message)s", "%H:%M:%S"))
        h.addFilter(_MaskFilter())
        h._ward = True  # type: ignore[attr-defined]
        root.addHandler(h)
    set_file_logging(level != "off" if to_file is None else to_file, level)
    # slixmpp is very chatty at DEBUG; its stanza dumps are what the XML console is for
    logging.getLogger("slixmpp").setLevel(logging.DEBUG if level == "verbose" and env == "debug" else logging.WARNING)


def set_file_logging(enabled: bool, level: str = "normal") -> None:
    global _file_handler
    root = logging.getLogger()
    if _file_handler is not None:
        root.removeHandler(_file_handler)
        _file_handler.close()
        _file_handler = None
    if enabled:
        paths.ensure(paths.state_dir())
        h = logging.handlers.RotatingFileHandler(paths.log_file(), maxBytes=5 * 1024 * 1024, backupCount=2,
                                                 encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        h.addFilter(_MaskFilter())
        _file_handler = h
        root.addHandler(h)
        root.setLevel(min(root.level, LEVELS.get(level, logging.INFO)))
