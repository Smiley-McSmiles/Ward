"""App-wide settings: typed values from schema.SPECS, stored as JSON in the config folder, with change signals.

    settings["appearance.style"]                    read (always a valid value)
    settings.set("appearance.style", "dark")        validate, store, emit changed::appearance.style
    settings.connect("changed::appearance.style", lambda s, key: ...)

Only values that differ from the default are written, so improving a default in a new version reaches everyone
who never touched that option."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from typing import Any

from gi.repository import GLib, GObject

from . import paths, schema, util

log = logging.getLogger("ward.settings")
FORMAT_VERSION = 1


class Settings(GObject.Object):
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_LAST | GObject.SignalFlags.DETAILED, None, (str,)),
    }

    def __init__(self, path: str | None = None):
        super().__init__()
        self.path = path or paths.settings_file()
        self._values: dict[str, Any] = {}
        self._save_source = 0
        raw = util.read_json(self.path, {}) or {}
        if not isinstance(raw, dict):
            raw = {}
        for key, spec in schema.SPECS.items():
            if key in raw:
                try:
                    self._values[key] = schema.coerce(spec, raw[key])
                    continue
                except ValueError as e:
                    log.warning("ignoring invalid setting %s=%r (%s)", key, raw[key], e)
            self._values[key] = schema.default(key)

    # ------------------------------------------------------------------ reading
    def __getitem__(self, key: str) -> Any:
        v = self._values[key]
        return list(v) if isinstance(v, list) else v

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def is_default(self, key: str) -> bool:
        return self._values[key] == schema.default(key)

    def snapshot(self) -> dict[str, Any]:
        return {k: self[k] for k in self._values}

    # ------------------------------------------------------------------ writing
    def set(self, key: str, value: Any) -> bool:
        """Store a value; returns True when it changed.  Raises KeyError / ValueError on bad input."""
        spec = schema.SPECS[key]
        v = schema.coerce(spec, value)
        if self._values.get(key) == v:
            return False
        self._values[key] = v
        self._schedule_save()
        self.emit("changed::" + key, key)
        return True

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def reset(self, keys: Iterable[str] | None = None) -> list[str]:
        keys = list(keys) if keys is not None else [k for k, s in schema.SPECS.items() if s.page is not None]
        changed = [k for k in keys if self.set(k, schema.default(k))]
        return changed

    def _schedule_save(self) -> None:
        # Coalesce bursts (a slider being dragged, the window being resized) into one write.
        if self._save_source:
            return
        try:
            self._save_source = GLib.timeout_add(400, self._save_timeout)
        except Exception:
            self.flush()

    def _save_timeout(self) -> bool:
        self._save_source = 0
        self.flush()
        return False

    def flush(self) -> None:
        if self._save_source:
            GLib.source_remove(self._save_source)
            self._save_source = 0
        data: dict[str, Any] = {"_version": FORMAT_VERSION}
        for k, v in self._values.items():
            if v != schema.default(k):
                data[k] = v
        try:
            util.write_json(self.path, data)
        except OSError as e:
            log.error("could not save settings to %s: %s", self.path, e)

    def exists_on_disk(self) -> bool:
        return os.path.exists(self.path)
