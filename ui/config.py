"""Persistent configuration for the MPD connection."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Keys left over from the removed backend selection and iTunes/DAAP support.
_OBSOLETE_KEYS = ("backend", "itunes", "paired_itunes")


class AppConfig:
    """Store the MPD connection settings in a JSON file."""

    def __init__(self, path: Path | str | None = None) -> None:
        if path is None:
            base = os.environ.get("APPDATA")
            if base:
                root = Path(base) / "headunit"
            else:
                root = Path.home() / ".config" / "headunit"
            root.mkdir(parents=True, exist_ok=True)
            self._path = root / "config.json"
        else:
            self._path = Path(path)
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if not self._path.exists():
            self._data = self._defaults()
            self.save()
            return
        try:
            text = self._path.read_text(encoding="utf-8")
            self._data = json.loads(text)
        except (OSError, json.JSONDecodeError):
            self._data = self._defaults()
        for key in _OBSOLETE_KEYS:
            self._data.pop(key, None)
        self._mergeDefaults(self._data, self._defaults())
        self.save()

    @classmethod
    def _mergeDefaults(cls, data: dict[str, Any], defaults: dict[str, Any]) -> None:
        """Fill in missing keys without disturbing values already configured."""
        for key, value in defaults.items():
            if key not in data:
                data[key] = value
            elif isinstance(value, dict) and isinstance(data.get(key), dict):
                cls._mergeDefaults(data[key], value)

    def save(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        finally:
            try:
                tmp.unlink(missing_ok=True)  # type: ignore
            except Exception:
                pass

    def _defaults(self) -> dict[str, Any]:
        return {
            "debug": True,
            # Volume notches across the full scale: 16 (6.25%) or 64 (1.5625%).
            "volume_steps": 16,
            "mpd": {
                "host": "127.0.0.1",
                "port": 6600,
                "password": "",
                # Socket timeout in milliseconds; lower values keep a slow or
                # unreachable server from freezing the UI for long.
                "timeout": 1000,
            },
        }

    def get(self, key: str, default: Any = None) -> Any:
        cur: Any = self._data
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur

    def set(self, key: str, value: Any) -> None:
        parts = key.split(".")
        cur = self._data
        for part in parts[:-1]:
            if part not in cur or not isinstance(cur.get(part), dict):
                cur[part] = {}
            cur = cur[part]
        cur[parts[-1]] = value
        self.save()

    def mpd(self) -> dict[str, Any]:
        return dict(self.get("mpd", self._defaults()["mpd"]))

    def mpdTimeout(self) -> float:
        """MPD socket timeout in seconds; the config file stores milliseconds."""
        try:
            milliseconds = int(self.get("mpd.timeout", 1000))
        except (TypeError, ValueError):
            milliseconds = 1000
        return max(100, milliseconds) / 1000.0

    def debugEnabled(self) -> bool:
        return bool(self.get("debug", True))

    def volumeSteps(self) -> int:
        """Volume notches across the full scale: 16 or 64 (default 16)."""
        try:
            steps = int(self.get("volume_steps", 16))
        except (TypeError, ValueError):
            return 16
        return 64 if steps == 64 else 16
