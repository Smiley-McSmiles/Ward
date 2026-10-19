"""OMEMO end-to-end encryption (XEP-0384): OMEMO 2 with Stanza Content Encryption, plus legacy OMEMO 0.3.

Importing the OMEMO libraries (python-omemo, twomemo, x3dh, oldmemo) takes about 170 ms, most of it spent compiling
classes and XML schemas.  Ward starts that in the background while the window is built, and creates its
OmemoService once the libraries are in (Core.load_omemo).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger("ward.omemo")

_lock = threading.Lock()
_thread: threading.Thread | None = None
_module: Any = None
_error: BaseException | None = None


def preload() -> threading.Thread:
    """Import the OMEMO libraries in a background thread (once); returns the thread whose join() means they are in."""
    global _thread
    with _lock:
        if _thread is None:
            _thread = threading.Thread(target=_run, name="ward-omemo-import", daemon=True)
            _thread.start()
        return _thread


def _run() -> None:
    global _module, _error
    try:
        from . import service

        _module = service
    except BaseException as e:      # a broken install must leave a reason for the user, not kill the thread silently
        _error = e


def result() -> tuple[Any, BaseException | None]:
    """(the service module, why it did not load) once preload()'s thread has finished."""
    return _module, _error
