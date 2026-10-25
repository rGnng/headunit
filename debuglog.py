"""Debug logging for the head unit.

Every module logs through :func:`getLogger`.  Records go to a rotating file
under the application configuration directory so intermittent hangs can be
diagnosed after the fact; ``--debug`` mirrors them to stderr as well.

:func:`startStallWatchdog` watches the GUI thread: the event loop calls
:func:`beat` periodically, and a background thread dumps the main thread's
stack when those beats stop arriving.  That turns an otherwise silent hang
into a timestamped traceback in the log.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUPS = 3

_configured = False
_logPath: Path | None = None
_beatLock = threading.Lock()
_lastBeat = time.monotonic()
_watchdog: threading.Thread | None = None


def logDirectory() -> Path:
    """Directory holding the log file; matches where config.json lives."""
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / ".config"
    return root / "headunit"


def logPath() -> Path:
    global _logPath
    if _logPath is None:
        _logPath = logDirectory() / "headunit.log"
    return _logPath


def setup(
    enabled: bool = True,
    console: bool = False,
    level: int = logging.DEBUG,
) -> Path:
    """Install the log handlers once and return the log file path."""
    global _configured
    root = logging.getLogger("headunit")
    if _configured:
        return logPath()

    root.setLevel(level)
    root.propagate = False
    formatter = logging.Formatter(FORMAT, DATEFMT)

    if enabled:
        try:
            path = logPath()
            path.parent.mkdir(parents=True, exist_ok=True)
            handler: logging.Handler = RotatingFileHandler(
                path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
            )
        except OSError:
            # No writable location; fall back to the console so logs survive.
            console = True
        else:
            handler.setFormatter(formatter)
            handler.setLevel(level)
            root.addHandler(handler)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.setLevel(level)
        root.addHandler(stream)

    sys.excepthook = _excepthook
    threading.excepthook = _threadExcepthook
    _configured = True
    return logPath()


def getLogger(name: str) -> logging.Logger:
    """Return a logger namespaced under ``headunit``."""
    return logging.getLogger(f"headunit.{name}")


def _excepthook(excType, excValue, excTraceback) -> None:
    getLogger("crash").critical(
        "uncaught exception in main thread",
        exc_info=(excType, excValue, excTraceback),
    )
    sys.__excepthook__(excType, excValue, excTraceback)


def _threadExcepthook(args) -> None:
    if args.exc_type is SystemExit:
        return
    getLogger("crash").critical(
        "uncaught exception in thread %s",
        args.thread.name if args.thread is not None else "?",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


# ---------------------------------------------------------------------- #
# GUI-thread stall detection
# ---------------------------------------------------------------------- #
def beat() -> None:
    """Record that the GUI thread is still responsive."""
    global _lastBeat
    with _beatLock:
        _lastBeat = time.monotonic()


def startStallWatchdog(
    threshold: float = 2.0, interval: float = 0.25, repeat: float = 5.0
) -> None:
    """Log the main thread's stack when it stops calling :func:`beat`."""
    global _watchdog
    if _watchdog is not None:
        return
    log = getLogger("watchdog")

    def run() -> None:
        lastReport = 0.0
        while True:
            time.sleep(interval)
            now = time.monotonic()
            with _beatLock:
                stalled = now - _lastBeat
            if stalled < threshold or now - lastReport < repeat:
                continue
            lastReport = now
            frame = sys._current_frames().get(threading.main_thread().ident)
            stack = "".join(traceback.format_stack(frame)) if frame else "<unavailable>"
            log.warning("GUI thread stalled for %.1fs; stack:\n%s", stalled, stack)

    _watchdog = threading.Thread(target=run, name="stall-watchdog", daemon=True)
    _watchdog.start()
