"""Entry point for the head-unit application."""

import os
import sys

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

import debuglog
from players.base import PlayerBackend
from players.mock import MockPlayerBackend
from ui import icons
from ui.config import AppConfig
from ui.state import AppState
from ui.theme import Palette
from ui.window import MainWindow

_log = debuglog.getLogger("main")


def createBackend(config: AppConfig | None = None) -> PlayerBackend:
    """Create the MPD backend, or the mock backend for --mock/smoke runs."""
    if "--mock" in sys.argv:
        _log.info("using mock backend (--mock)")
        return MockPlayerBackend()
    config = config or AppConfig()
    try:
        from players.mpd import MpdPlayerBackend

        mpd_cfg = config.mpd()
        timeout = config.mpdTimeout()
        _log.info(
            "starting MPD backend at %s:%s (timeout %.0f ms)",
            mpd_cfg.get("host", "127.0.0.1"),
            mpd_cfg.get("port", 6600),
            timeout * 1000,
        )
        return MpdPlayerBackend(
            host=mpd_cfg.get("host", "127.0.0.1"),
            port=int(mpd_cfg.get("port", 6600)),
            password=mpd_cfg.get("password", ""),
            timeout=timeout,
        )
    except (OSError, Exception) as exc:  # noqa: BLE001 - fall back on any failure
        _log.warning("MPD unavailable, using mock backend: %s", exc)
        print(f"MPD unavailable ({exc}); using mock backend", file=sys.stderr)
        return MockPlayerBackend()


def sendKey(window: MainWindow, key: Qt.Key) -> None:
    """Deliver a key press to the window, as the knobs would."""
    press = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(window, press)


def smokeDrive(window: MainWindow, app: QApplication) -> None:
    """Exercise key routing end to end, then quit."""
    sendKey(window, Qt.Key.Key_2)
    sendKey(window, Qt.Key.Key_Return)
    sendKey(window, Qt.Key.Key_Return)
    sendKey(window, Qt.Key.Key_E)
    sendKey(window, Qt.Key.Key_Q)
    sendKey(window, Qt.Key.Key_W)
    sendKey(window, Qt.Key.Key_O)
    sendKey(window, Qt.Key.Key_P)
    sendKey(window, Qt.Key.Key_5)
    sendKey(window, Qt.Key.Key_Down)
    sendKey(window, Qt.Key.Key_6)
    sendKey(window, Qt.Key.Key_0)
    sendKey(window, Qt.Key.Key_0)
    sendKey(window, Qt.Key.Key_0)
    sendKey(window, Qt.Key.Key_Asterisk)
    sendKey(window, Qt.Key.Key_Minus)
    sendKey(window, Qt.Key.Key_Equal)
    sendKey(window, Qt.Key.Key_X)
    QTimer.singleShot(300, app.quit)


def main() -> int:
    config = AppConfig()
    debuglog.setup(
        enabled=config.debugEnabled(), console="--debug" in sys.argv
    )
    _log.info("headunit starting (pid %d)", os.getpid())
    _log.info("log file: %s", debuglog.logPath())

    app = QApplication(sys.argv)
    app.setStyleSheet(f"background-color: {Palette.Background.name()};")
    icons.loadFont()

    backend = createBackend(config)
    state = AppState()
    window = MainWindow(backend, state=state, config=config)
    window.show()

    # Watch for GUI-thread stalls: the timer only fires while the event loop
    # is healthy, so a missed beat means something is blocking it.
    debuglog.startStallWatchdog()
    heartbeat = QTimer()
    heartbeat.setInterval(200)
    heartbeat.timeout.connect(debuglog.beat)
    heartbeat.start()

    if "--smoke" in sys.argv:
        QTimer.singleShot(100, lambda: smokeDrive(window, app))

    _log.info("entering event loop")
    try:
        return app.exec()
    finally:
        _log.info("event loop finished")
        debuglog.beat()


if __name__ == "__main__":
    sys.exit(main())
