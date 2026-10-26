"""Persistent interface state.

The head unit remembers where the user left off: the cursor of every
section, the sort order of the browsers and the view that was open. The
data is a small JSON document stored in the user's configuration directory
so that it survives a restart of the application.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer

# How long the store waits for more changes before writing to disk.
SAVE_DELAY_MS = 1500


def defaultStatePath() -> Path:
    """Return the per-user location of the state file."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "headunit" / "state.json"


def _asTuple(value: Any) -> Any:
    """Turn the JSON lists back into the tuples used as cursor keys."""
    if isinstance(value, list):
        return tuple(_asTuple(item) for item in value)
    return value


class AppState(QObject):
    """A small JSON-backed store for the interface state.

    Changes are written to disk a moment after they happen, so a quick run
    of the knob never turns into a burst of disk writes. A failed write is
    ignored: the interface must keep working even when the state cannot be
    stored.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        parent: QObject | None = None,
        persistent: bool = True,
    ) -> None:
        super().__init__(parent)
        self._path = Path(path) if path is not None else defaultStatePath()
        self._persistent = persistent
        self._data: dict[str, Any] = {}
        self._dirty = False

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SAVE_DELAY_MS)
        self._timer.timeout.connect(self.save)

        self.load()

    # -- file -------------------------------------------------------------- #
    def load(self) -> None:
        """Read the state file; a missing or broken file starts fresh."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if isinstance(data, dict):
            self._data = data

    def save(self) -> None:
        """Write the state file when something changed."""
        if not self._dirty or not self._persistent:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._path.with_name(self._path.name + ".tmp")
            temp.write_text(
                json.dumps(self._data, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            temp.replace(self._path)
        except OSError:
            return
        self._dirty = False

    def flush(self) -> None:
        """Write any pending change right away."""
        self._timer.stop()
        self.save()

    # -- values ------------------------------------------------------------ #
    def get(self, key: str, default: Any = None) -> Any:
        """Return a stored value."""
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Store a value and schedule a write."""
        if self._data.get(key) == value:
            return
        self._data[key] = value
        self._dirty = True
        if self._persistent:
            self._timer.start()

    def getString(self, key: str, default: str = "") -> str:
        """Return a stored string."""
        value = self._data.get(key, default)
        return value if isinstance(value, str) else default

    def getInt(self, key: str, default: int = 0) -> int:
        """Return a stored whole number."""
        value = self._data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            return default
        return value

    def getKey(self, key: str) -> tuple | None:
        """Return a stored cursor identity key."""
        value = self._data.get(key)
        return _asTuple(value) if value is not None else None

    def setKey(self, key: str, value: object) -> None:
        """Store a cursor identity key."""
        self.set(key, list(value) if isinstance(value, tuple) else value)
