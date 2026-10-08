"""Pulse settings: config.json in the app's data folder.

The file is created with the defaults on first start and rewritten atomically
(temp file + fsync + rename), so a crash or SIGKILL never leaves it half written.
"""
from __future__ import annotations

import json
import os
import threading

CONFIG_FILE = "config.json"

# name: (default, min, max)
FIELDS: dict[str, tuple[int, int, int]] = {
    "interval_s": (30, 10, 300),           # seconds between samples
    "retention_raw_h": (48, 1, 168),       # full-detail samples, in hours
    "retention_5m_d": (30, 1, 365),        # 5-minute averages, in days
    "retention_1h_d": (730, 7, 3650),      # hourly averages, in days
    "max_db_mb": (200, 10, 4000),          # database size limit, in MB
}


class ConfigError(ValueError):
    pass


def defaults() -> dict[str, int]:
    return {name: spec[0] for name, spec in FIELDS.items()}


def validate(values: dict) -> dict[str, int]:
    """Returns the known fields as ints within range; raises ConfigError otherwise."""
    out: dict[str, int] = {}
    for name, (_, lo, hi) in FIELDS.items():
        if name not in values:
            continue
        raw = values[name]
        try:
            if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
                raise ValueError
            number = float(raw)
        except ValueError:
            raise ConfigError(f"{name}: expected a whole number") from None
        if not number.is_integer():
            raise ConfigError(f"{name}: expected a whole number")
        v = int(number)
        if not lo <= v <= hi:
            raise ConfigError(f"{name}: must be between {lo} and {hi}")
        out[name] = v
    return out


class Config:
    def __init__(self, data_dir: str) -> None:
        self._path = os.path.join(data_dir, CONFIG_FILE)
        self._lock = threading.Lock()
        self._values = defaults()
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as f:
                stored = json.load(f)
        except FileNotFoundError:
            self._save()
            return
        except (OSError, ValueError):
            # unreadable file: keep the defaults and rewrite it
            self._save()
            return
        if not isinstance(stored, dict):
            self._save()
            return
        for name in FIELDS:
            try:
                self._values.update(validate({name: stored[name]}))
            except (KeyError, ConfigError):
                pass  # missing or invalid field: keep its default

    def _save(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._values, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    def get(self, name: str) -> int:
        with self._lock:
            return self._values[name]

    def as_dict(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)

    def update(self, values: dict) -> dict[str, int]:
        """Validates and saves the given fields; returns the full settings."""
        changes = validate(values)
        with self._lock:
            self._values.update(changes)
            self._save()
            return dict(self._values)
