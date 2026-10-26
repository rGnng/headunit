"""Main window: layout, knob key mapping, focus management."""

from __future__ import annotations

import time

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QKeyEvent, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import QMainWindow, QStackedWidget, QVBoxLayout, QWidget

from debuglog import getLogger
from players.base import Album, PlayerBackend, RepeatMode
from ui.config import AppConfig
from ui.pages import (
    AlbumsPage,
    ArtistsPage,
    GenresPage,
    NowPlayingPage,
    Page,
    PlaylistsPage,
    QueuePage,
    SettingsPage,
    SongsPage,
)
from ui.state import AppState
from ui.theme import Palette
from ui.widgets import CachingOverlay, ConnectionOverlay, StatusBar, VolumeOverlay

# Volume knob (small):  left turn -> '-', right turn -> '=', press -> 'x'
# Main knob (large):    left turn -> Up,  right turn -> Down, press -> Enter
VOLUME_STEP = 5

_log = getLogger("ui.window")

# Number keys 0-6 select a section directly. Key 0 opens Now Playing,
# or toggles to Up Next when Now Playing is already visible.
SECTION_KEYS = {
    Qt.Key.Key_0: 0,
    Qt.Key.Key_1: 1,
    Qt.Key.Key_2: 2,
    Qt.Key.Key_3: 3,
    Qt.Key.Key_4: 4,
    Qt.Key.Key_5: 5,
    Qt.Key.Key_6: 6,
}

_REPEAT_LABELS = {
    RepeatMode.Off: "Off",
    RepeatMode.All: "All",
    RepeatMode.One: "One",
}


class MainWindow(QMainWindow):
    """800x480 head-unit window operated exclusively by the two knobs."""

    def __init__(
        self,
        backend: PlayerBackend,
        parent: QWidget | None = None,
        state: AppState | None = None,
        config: AppConfig | None = None,
    ) -> None:
        super().__init__(parent)
        self.backend = backend
        self.state = state or AppState()
        self.config = config or AppConfig()
        self._enterLongTriggered = False
        self._enterPending = False
        self._enterTimer = QTimer(self)
        self._enterTimer.setSingleShot(True)
        self._enterTimer.setInterval(650)
        self._enterTimer.timeout.connect(self._onEnterLongPress)
        self._transportPendingKey: Qt.Key | None = None
        self._transportLongTriggered = False
        self._transportLongTimer = QTimer(self)
        self._transportLongTimer.setSingleShot(True)
        self._transportLongTimer.setInterval(650)
        self._transportLongTimer.timeout.connect(self._onTransportLongPress)
        self._seekRepeatTimer = QTimer(self)
        self._seekRepeatTimer.setInterval(250)
        self._seekRepeatTimer.timeout.connect(self._repeatSeek)
        self.setWindowTitle("Headunit")
        self.setFixedSize(800, 480)
        self.setStyleSheet(f"background-color: {Palette.Background.name()};")

        # -- background (black or a blurred version of the album art) -------- #
        self._backdropMode = "black"
        self._backdropCurrent: QPixmap | None = None

        # -- pages ----------------------------------------------------------- #
        self._pages = QStackedWidget()
        self._installPages(backend)

        # -- status bar ------------------------------------------------------ #
        self._status = StatusBar()

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._pages, 1)
        layout.addWidget(self._status)
        self.setCentralWidget(central)

        # -- volume overlay -------------------------------------------------- #
        self._volumeOverlay = VolumeOverlay(self)

        # -- connection-lost overlay ---------------------------------------- #
        self._connectionOverlay = ConnectionOverlay(self)
        self._positionConnectionOverlay()
        self._connectionTimer = QTimer(self)
        self._connectionTimer.setInterval(300)
        self._connectionTimer.timeout.connect(self._refreshConnection)
        self._connectionTimer.start()

        # -- idle cover caching --------------------------------------------- #
        self._cachingOverlay = CachingOverlay(self)
        self._cacheAlbums: list[Album] = []
        self._cacheIndex = 0
        self._cacheTotal = 0
        self._cacheActive = False
        self._cacheComplete = False
        self._cacheWaitStarted: float | None = None
        self._cacheTimer = QTimer(self)
        self._cacheTimer.setInterval(200)
        self._cacheTimer.timeout.connect(self._cacheStep)
        self._idleTimer = QTimer(self)
        self._idleTimer.setSingleShot(True)
        self._idleTimer.setInterval(10000)
        self._idleTimer.timeout.connect(self._onIdle)
        self._idleTimer.start()
        self._positionCachingOverlay()

        # -- state ------------------------------------------------------------ #
        self._statusCoverTrack = None
        self._statusCover = None
        startIndex = self.state.getInt("ui.section", 0)
        if not 0 <= startIndex < len(self._pageList):
            startIndex = 0
        self._pages.setCurrentIndex(startIndex)
        self._refreshStatus()

        self._connectBackendSignals(backend)
        self._started = False
        self.setBackgroundMode(self.state.getString("settings.background", "black"))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._started:
            return
        self._started = True
        self._updateBackdrop()
        # Populate the restored page after the window is on screen: building a
        # large library can take a moment and a visible window feels faster.
        QTimer.singleShot(0, self._currentPage().onActivated)

    def _installPages(self, backend: PlayerBackend) -> None:
        self._pageList: list[Page] = [
            NowPlayingPage(backend, None, self.state),
            AlbumsPage(backend, None, self.state),
            SongsPage(backend, None, self.state),
            ArtistsPage(backend, None, self.state),
            GenresPage(backend, None, self.state),
            PlaylistsPage(backend, None, self.state),
            SettingsPage(backend, None, self.state),
            QueuePage(backend, None, self.state),
        ]
        self._queueIndex = next(
            (
                index
                for index, page in enumerate(self._pageList)
                if isinstance(page, QueuePage)
            ),
            0,
        )
        for page in self._pageList:
            self._pages.addWidget(page)
            if hasattr(page, "playbackStarted"):
                page.playbackStarted.connect(self._showNowPlaying)
            if hasattr(page, "jumpRequested"):
                page.jumpRequested.connect(self._onJumpRequested)

    def _connectBackendSignals(self, backend: PlayerBackend) -> None:
        backend.volumeChanged.connect(self._onVolumeChanged)
        backend.trackChanged.connect(lambda _t: self._refreshStatus())
        backend.trackChanged.connect(lambda _t: self._updateBackdrop())
        backend.coverChanged.connect(self._updateBackdrop)
        backend.coverChanged.connect(self._onCoverChangedStatus)
        backend.playStateChanged.connect(lambda _p: self._refreshStatus())
        backend.playStateChanged.connect(lambda _p: self._updateBackdrop())
        backend.positionChanged.connect(lambda _p: self._refreshStatus())
        backend.positionTick.connect(self._onPositionTick)
        backend.starredChanged.connect(lambda _f: self._refreshStatus())
        backend.playlistsChanged.connect(self._refreshStatus)
        backend.playbackOptionsChanged.connect(self._refreshStatus)
        if hasattr(backend, "connectionRestored"):
            backend.connectionRestored.connect(self._onConnectionRestored)
        backend.libraryChanged.connect(self._resetCaching)
        backend.coverChanged.connect(self._onCoverChangedForCaching)
        if hasattr(backend, "connectionLost"):
            backend.connectionLost.connect(self._pauseCaching)

    # ---------------------------------------------------------------------- #
    # Navigation
    # ---------------------------------------------------------------------- #
    def _currentPage(self) -> Page:
        return self._pageList[self._pages.currentIndex()]

    def _switchTo(self, index: int) -> None:
        """Select a section directly (number keys)."""
        if index == self._pages.currentIndex():
            return
        # Leave no option menu open behind a section change.
        for page in self._pageList:
            if page.menuOpen():
                page.menuCancel()
        self._pages.setCurrentIndex(index)
        if self.state is not None:
            self.state.set("ui.section", index)
        self._currentPage().onActivated()
        self._refreshStatus()

    def _showNowPlaying(self) -> None:
        """Jump to Now Playing after a recording has been selected."""
        self._switchTo(0)
        self._currentPage().onActivated()

    def _onJumpRequested(self, kind: str, value: str) -> None:
        """Jump from Now Playing to the current album or artist."""
        if kind == "album":
            artist, _, title = value.partition("\x1f")
            self._switchTo(1)
            self._pageList[1].openAlbumNamed(artist, title)
        elif kind == "artist":
            self._switchTo(3)
            self._pageList[3].openArtistNamed(value)

    def reconnectBackend(self) -> None:
        """Reconnect the playback backend and refresh all pages."""
        try:
            self.backend.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.backend.connect()
        except Exception:  # noqa: BLE001
            pass
        for page in self._pageList:
            if isinstance(page, SettingsPage):
                page.onActivated()
            elif hasattr(page, "_buildRoot"):
                page._buildRoot()
            elif hasattr(page, "_refresh"):
                page._refresh()
        self._refreshStatus()

    # ---------------------------------------------------------------------- #
    # Transport (global)
    # ---------------------------------------------------------------------- #
    def _prevTrack(self) -> None:
        self.backend.previous()

    def _nextTrack(self) -> None:
        self.backend.next()

    def _togglePlay(self) -> None:
        self.backend.togglePlay()

    def _toggleRepeat(self) -> None:
        modes = [RepeatMode.Off, RepeatMode.All, RepeatMode.One]
        current = modes.index(self.backend.repeatMode())
        self.backend.setRepeatMode(modes[(current + 1) % len(modes)])
        self._refreshStatus()

    def _toggleShuffle(self) -> None:
        self.backend.setShuffle(not self.backend.shuffle())
        self._refreshStatus()

    # ---------------------------------------------------------------------- #
    # Volume knob
    # ---------------------------------------------------------------------- #
    def _onVolumeChanged(self, level: int, muted: bool) -> None:
        displayLevel = self.backend.volumeDisplayLevel()
        self._status.updateVolume(displayLevel, muted)
        self._volumeOverlay.present(displayLevel, muted)

    # ---------------------------------------------------------------------- #
    # Key routing (knob simulation)
    # ---------------------------------------------------------------------- #
    def keyPressEvent(self, event: QKeyEvent) -> None:
        self._noteActivity()
        key = event.key()
        page = self._currentPage()

        # An open option menu captures the main knob and the confirm key.
        if page.menuOpen():
            if key == Qt.Key.Key_Up:
                page.menuNavigate(-1)
                return
            if key == Qt.Key.Key_Down:
                page.menuNavigate(1)
                return
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                page.menuSelect()
                return
            if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Left, Qt.Key.Key_Escape):
                page.menuCancel()
                return
            if key == Qt.Key.Key_Backslash:
                page.menuCancel()
                return

        # Global transport: q previous, e next, w play/pause.
        if key in (Qt.Key.Key_Q, Qt.Key.Key_E):
            if event.isAutoRepeat() or self._transportPendingKey is not None:
                return
            self._transportPendingKey = key
            self._transportLongTriggered = False
            self._transportLongTimer.start()
            return
        if key == Qt.Key.Key_W:
            self._togglePlay()
            return

        # Global playback options: o repeat, p shuffle.
        if key == Qt.Key.Key_O:
            self._toggleRepeat()
            return
        if key == Qt.Key.Key_P:
            self._toggleShuffle()
            return

        # Star or unstar the current recording with the asterisk key.
        if key in (Qt.Key.Key_Asterisk, Qt.Key.Key_multiply) or (
            key == Qt.Key.Key_8
            and event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            track = self.backend.currentTrack()
            if track is not None and self.backend.supportsPlaylists():
                self.backend.toggleStarred(track)
            return

        # Option key: backslash
        if key == Qt.Key.Key_Backslash:
            page.onOption()
            return

        # Number keys select a section directly. Key 0 toggles between the
        # Now Playing and Up Next views when Now Playing is already active.
        # Pressing the key of the section already on screen returns to the
        # top of that section's list.
        if key in SECTION_KEYS:
            target = SECTION_KEYS[key]
            if key == Qt.Key.Key_0 and self._pages.currentIndex() == 0:
                target = self._queueIndex
            if target == self._pages.currentIndex():
                self._currentPage().onSectionKey()
            else:
                self._switchTo(target)
            return

        # Volume knob: left turn '-', right turn '=', press 'x'.
        if key == Qt.Key.Key_Minus:
            self.backend.adjustVolume(-1)
            return
        if key in (Qt.Key.Key_Equal, Qt.Key.Key_Plus):
            self.backend.adjustVolume(1)
            return
        if key == Qt.Key.Key_X:
            self.backend.toggleMute()
            return

        # Window-level keys.
        if key == Qt.Key.Key_Escape:
            self.close()
            return

        # Main knob: left turn Up, right turn Down, press Enter.
        if key == Qt.Key.Key_Up:
            page.navigate(-1)
            return
        if key == Qt.Key.Key_Down:
            page.navigate(1)
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if hasattr(page, "selectLong"):
                if not self._enterPending:
                    self._enterPending = True
                    self._enterLongTriggered = False
                    self._enterTimer.start()
                return
            page.select()
            return

        # Back navigation: pop a drill-down level, or fall back to Now Playing.
        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Left):
            if not page.back():
                self._switchTo(0)
            return

        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Q, Qt.Key.Key_E) and event.isAutoRepeat():
            return
        if event.key() == self._transportPendingKey:
            self._transportLongTimer.stop()
            self._seekRepeatTimer.stop()
            if not self._transportLongTriggered:
                if event.key() == Qt.Key.Key_Q:
                    self._prevTrack()
                else:
                    self._nextTrack()
            self._transportPendingKey = None
            self._transportLongTriggered = False
            return
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and event.isAutoRepeat()
        ):
            # Qt synthesizes release/press pairs while a key is held. Only
            # the physical release should end the queue's short/long press.
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._enterPending:
            self._enterTimer.stop()
            if not self._enterLongTriggered:
                self._currentPage().select()
            self._enterPending = False
            self._enterLongTriggered = False
            return
        super().keyReleaseEvent(event)

    def _onEnterLongPress(self) -> None:
        if self._enterPending and hasattr(self._currentPage(), "selectLong"):
            self._enterLongTriggered = True
            self._currentPage().selectLong()

    def _onTransportLongPress(self) -> None:
        key = self._transportPendingKey
        if key not in (Qt.Key.Key_Q, Qt.Key.Key_E):
            return
        self._transportLongTriggered = True
        self.backend.seekBy(-10 if key == Qt.Key.Key_Q else 10)
        self._seekRepeatTimer.start()

    def _repeatSeek(self) -> None:
        key = self._transportPendingKey
        if key in (Qt.Key.Key_Q, Qt.Key.Key_E):
            self.backend.seekBy(-5 if key == Qt.Key.Key_Q else 5)

    # ---------------------------------------------------------------------- #
    # Status bar
    # ---------------------------------------------------------------------- #
    def _refreshStatus(self) -> None:
        started = time.monotonic()
        track = self.backend.currentTrack()
        hasQueue = track is not None

        if hasQueue:
            text = f"{track.title} - {track.album}"
        else:
            text = "Stopped"

        # Cache the cover per track; lookups hit the backend so do them once.
        if track != self._statusCoverTrack:
            self._statusCoverTrack = track
            self._statusCover = (
                self.backend.trackThumbnail(track) if track is not None else None
            )

        self._status.updateState(
            playing=self.backend.isPlaying(),
            hasQueue=hasQueue,
            text=text,
            position=self.backend.positionNow(),
            duration=self.backend.duration(),
            queuePosition=self.backend.queuePosition(),
            queueLength=self.backend.queueLength(),
            cover=self._statusCover,
            stopped=self.backend.isStopped(),
        )
        self._status.updateOptions(
            shuffle=self.backend.shuffle(),
            repeatLabel=_REPEAT_LABELS[self.backend.repeatMode()],
        )
        self._status.updateVolume(
            self.backend.volumeDisplayLevel(), self.backend.isMuted()
        )
        supported = self.backend.supportsPlaylists()
        self._status.updateStarred(
            track is not None and supported and self.backend.isStarred(track),
            track is not None and supported,
        )
        # Now Playing already shows the current recording, but keep position/length visible.
        self._status.setTrackGroupVisible(self._pages.currentIndex() != 0)

        elapsedMs = (time.monotonic() - started) * 1000
        if elapsedMs >= 200:
            _log.warning("slow status refresh: %.0f ms", elapsedMs)
        else:
            _log.debug("status refresh: %.1f ms", elapsedMs)

    def _onCoverChangedStatus(self) -> None:
        """A cover arrived; pick it up if the status bar had none."""
        if self._statusCover is None:
            self._statusCoverTrack = None
            self._refreshStatus()

    def _onPositionTick(self, position: int) -> None:
        self._status.updatePosition(position)

    # ---------------------------------------------------------------------- #
    # Background
    # ---------------------------------------------------------------------- #
    def setBackgroundMode(self, mode: str) -> None:
        """Switch between a plain black background and blurred album art."""
        mode = "blurred" if mode == "blurred" else "black"
        self._backdropMode = mode
        transparent = mode == "blurred"
        for page in self._pageList:
            if hasattr(page, "_list"):
                page._list.setBackgroundTransparent(transparent)
            # Clear the app-wide black so the page backdrop can show through.
            page.setStyleSheet("QWidget { background: transparent; }" if transparent else "")
        if transparent:
            self._updateBackdrop()
        else:
            self._backdropCurrent = None
            for page in self._pageList:
                page.setBackdrop(None)

    def _updateBackdrop(self, *_args) -> None:
        if self._backdropMode != "blurred":
            return
        if not self.backend.isPlaying():
            # Nothing playing: a plain vertical grey-to-black gradient.
            pixmap = self._gradientPixmap()
        else:
            track = self.backend.currentTrack()
            if track is None:
                pixmap = self._gradientPixmap()
            else:
                data = self.backend.trackCover(track)
                if data:
                    pixmap = self._blurredPixmap(data) or self._gradientPixmap()
                elif self.backend.coverStatus(track) == "missing":
                    pixmap = self._gradientPixmap()
                else:
                    # Artwork is still loading; keep the current background and
                    # wait for coverChanged.
                    return
        if pixmap is None and self._backdropCurrent is None:
            return
        self._backdropCurrent = pixmap
        for page in self._pageList:
            page.setBackdrop(pixmap)

    def _backdropTargetSize(self) -> QSize:
        target = self._pages.size()
        if target.width() < 10 or target.height() < 10:
            target = QSize(800, 480 - StatusBar.HEIGHT)
        return target

    def _blurredPixmap(self, data: bytes) -> QPixmap | None:
        image = QImage.fromData(data)
        if image.isNull():
            return None
        # Downscale hard, then stretch back: a cheap, very strong blur.
        small = image.scaled(
            4,
            4,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled = small.scaled(
            self._backdropTargetSize(),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        pixmap = QPixmap.fromImage(scaled)
        # Darken the artwork so text stays readable, even on light covers.
        painter = QPainter(pixmap)
        painter.fillRect(pixmap.rect(), QColor(0, 0, 0, 190))
        painter.end()
        return pixmap

    def _gradientPixmap(self) -> QPixmap:
        """Placeholder background used when nothing is playing: #555 -> #000."""
        target = self._backdropTargetSize()
        pixmap = QPixmap(target)
        painter = QPainter(pixmap)
        gradient = QLinearGradient(0, 0, 0, target.height())
        gradient.setColorAt(0.0, QColor(85, 85, 85))
        gradient.setColorAt(1.0, QColor(0, 0, 0))
        painter.fillRect(pixmap.rect(), gradient)
        painter.end()
        return pixmap

    # ---------------------------------------------------------------------- #
    # Connection status
    # ---------------------------------------------------------------------- #
    def _refreshConnection(self) -> None:
        if self.backend.serverState() != "Disconnected":
            wasDown = self._connectionOverlay.isVisible()
            self._connectionOverlay.hide()
            if wasDown:
                self._onConnectionRestored()
            return
        attempt = max(1, self.backend.retryAttempt())
        latency = self.backend.latencyMs()
        if latency is not None:
            ping = "Ping <1 ms" if latency < 1 else f"Ping {latency:.0f} ms"
        else:
            state, hostMs = self.backend.hostPingState()
            if state == "timeout":
                ping = "Ping timeout"
            elif state == "ok" and hostMs is not None:
                ping = "Ping <1 ms" if hostMs < 1 else f"Ping {hostMs:.0f} ms"
            else:
                ping = "Ping -- ms"
        self._positionConnectionOverlay()
        self._connectionOverlay.present(attempt, ping)

    def _onConnectionRestored(self) -> None:
        """Refresh everything that may have gone stale while offline."""
        _log.info("connection restored; refreshing views")
        self._connectionOverlay.hide()
        self._statusCoverTrack = None
        self._statusCover = None
        self._refreshStatus()
        nowPlaying = self._pageList[0]
        if hasattr(nowPlaying, "reloadCover"):
            nowPlaying.reloadCover()
        page = self._currentPage()
        if isinstance(page, QueuePage):
            page._refresh()
        self._updateBackdrop()
        self._idleTimer.start()

    # ---------------------------------------------------------------------- #
    # Idle cover caching
    # ---------------------------------------------------------------------- #
    def _noteActivity(self) -> None:
        """Any knob/key input pauses caching and restarts the idle timer."""
        self._idleTimer.start()
        if self._cacheActive:
            self._pauseCaching()

    def _onIdle(self) -> None:
        if self._cacheComplete:
            return
        if self.backend.serverState() == "Disconnected":
            # Try again later; there is no point fetching covers offline.
            self._idleTimer.start()
            return
        self._startCaching()

    def _startCaching(self) -> None:
        if self._cacheActive or self._cacheComplete:
            return
        if not self._cacheAlbums:
            self._cacheAlbums = self.backend.albums()
            self._cacheTotal = len(self._cacheAlbums)
            self._cacheIndex = 0
        if not self._cacheAlbums:
            if not self.backend.libraryLoaded():
                # The snapshot could not be fetched; try again on the next
                # idle period instead of marking the pass complete.
                self._idleTimer.start()
                return
            self._cacheComplete = True
            return
        if self._cacheIndex >= self._cacheTotal:
            self._cacheComplete = True
            return
        self._cacheActive = True
        self._cacheWaitStarted = None
        _log.info(
            "idle cover caching started: %d albums remaining",
            self._cacheTotal - self._cacheIndex,
        )
        self._positionCachingOverlay()
        self._cachingOverlay.present(self._cacheIndex, self._cacheTotal)
        self._cacheTimer.start()
        self._cacheStep()

    def _cacheStep(self) -> None:
        if not self._cacheActive:
            return
        started = time.monotonic()
        # Walk over already-cached albums quickly, but never block the UI.
        while (
            self._cacheIndex < self._cacheTotal
            and time.monotonic() - started < 0.03
            and self._cacheActive
        ):
            album = self._cacheAlbums[self._cacheIndex]
            if self.backend.albumCoverCached(album):
                self._cacheIndex += 1
                self._cacheWaitStarted = None
                continue
            data = self.backend.albumCover(album)
            status = self.backend.albumCoverStatus(album)
            if data or status == "missing":
                self._cacheIndex += 1
                self._cacheWaitStarted = None
                continue
            now = time.monotonic()
            if self._cacheWaitStarted is None:
                self._cacheWaitStarted = now
            elif now - self._cacheWaitStarted > 10.0:
                # A cover that keeps timing out must not stall the whole run.
                self._cacheIndex += 1
                self._cacheWaitStarted = None
                continue
            break
        self._cachingOverlay.present(self._cacheIndex, self._cacheTotal)
        if self._cacheIndex >= self._cacheTotal:
            self._finishCaching()

    def _pauseCaching(self) -> None:
        if not self._cacheActive:
            return
        self._cacheActive = False
        self._cacheTimer.stop()
        self._cachingOverlay.hide()

    def _finishCaching(self) -> None:
        self._cacheActive = False
        self._cacheTimer.stop()
        self._cachingOverlay.hide()
        self._cacheComplete = True
        self._cacheWaitStarted = None
        _log.info("idle cover caching complete: %d albums", self._cacheTotal)

    def _resetCaching(self) -> None:
        """Library changed: start a fresh cache pass on the next idle period."""
        self._pauseCaching()
        self._cacheAlbums = []
        self._cacheIndex = 0
        self._cacheTotal = 0
        self._cacheComplete = False
        self._cacheWaitStarted = None

    def _onCoverChangedForCaching(self) -> None:
        if self._cacheActive:
            self._cacheStep()

    def startRecaching(self) -> None:
        """Clear artwork caches and start caching immediately."""
        if hasattr(self.backend, "clearCoverCache"):
            self.backend.clearCoverCache()
        self._resetCaching()
        self._statusCoverTrack = None
        self._statusCover = None
        for page in self._pageList:
            if hasattr(page, "_list"):
                page._list.invalidateCoverCache()
        nowPlaying = self._pageList[0]
        if hasattr(nowPlaying, "reloadCover"):
            nowPlaying.reloadCover()
        self._refreshStatus()
        self._updateBackdrop()
        if self.backend.serverState() != "Disconnected":
            self._startCaching()

    def reloadCaches(self) -> None:
        """Clear in-memory caches and reload views without restarting."""
        self._resetCaching()
        self._statusCoverTrack = None
        self._statusCover = None
        for page in self._pageList:
            if hasattr(page, "_list"):
                page._list.invalidateCoverCache()
        if hasattr(self.backend, "reload"):
            self.backend.reload()
        self._currentPage().onActivated()
        nowPlaying = self._pageList[0]
        if hasattr(nowPlaying, "reloadCover"):
            nowPlaying.reloadCover()
        self._refreshStatus()
        self._updateBackdrop()
        _log.info("memory caches reloaded")

    def _positionCachingOverlay(self) -> None:
        self._cachingOverlay.move(
            self.width() - self._cachingOverlay.WIDTH - 16, 12
        )

    def _positionConnectionOverlay(self) -> None:
        self._connectionOverlay.move(
            (self.width() - self._connectionOverlay.WIDTH) // 2,
            (self.height() - self._connectionOverlay.HEIGHT) // 2,
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Keep the volume overlay centred above the status bar.
        self._volumeOverlay.move(
            (self.width() - self._volumeOverlay.WIDTH) // 2,
            self.height() - self._volumeOverlay.HEIGHT - StatusBar.HEIGHT - 8,
        )
        self._positionConnectionOverlay()
        self._positionCachingOverlay()
