"""Abstract playback backend layer.

The UI only talks to this interface. The MPD integration implements
PlayerBackend later without touching the UI code.
"""

from __future__ import annotations

import time
from abc import ABC, ABCMeta, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto

from PySide6.QtCore import QObject, QTimer, Signal

# Playlist holding starred recordings. The MPD server stores it as "Stared".
STARRED_PLAYLIST = "Stared"


class _BackendMeta(type(QObject), ABCMeta):
    """Merge QObject's metaclass with ABCMeta so abstract backends work."""


class RepeatMode(Enum):
    Off = auto()
    All = auto()
    One = auto()


@dataclass(frozen=True)
class Track:
    title: str
    artist: str
    album: str
    duration: int  # seconds
    track_number: int = 0
    genre: str = ""
    year: int = 0
    added: int = 0  # unix timestamp of when the file entered the library
    album_artist: str = ""  # groups albums; empty means use artist
    disc_number: int = 0  # 0 when the file has no disc tag


@dataclass(frozen=True)
class Album:
    title: str
    artist: str
    year: int = 0
    genre: str = ""
    added: int = 0  # unix timestamp of the most recent addition
    track_count: int = 0


@dataclass(frozen=True)
class Output:
    """One audio output reported by the backend."""

    id: int
    name: str
    enabled: bool


def formatDuration(seconds: int) -> str:
    """Format a duration as m:ss."""
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


class PlayerBackend(QObject, ABC, metaclass=_BackendMeta):
    """Interface every playback integration must provide."""

    trackChanged = Signal(object)       # Track | None
    playStateChanged = Signal(bool)     # True while playing
    positionChanged = Signal(float)     # elapsed seconds as reported by the remote
    positionTick = Signal(int)          # interpolated elapsed seconds, ~4 times a second
    volumeChanged = Signal(int, bool)   # level 0..100, muted
    playbackOptionsChanged = Signal()  # shuffle and repeat state
    libraryChanged = Signal()
    coverChanged = Signal()
    playlistsChanged = Signal()
    starredChanged = Signal(bool)     # current recording was starred or unstarred

    _CLOCK_MS = 250
    VOLUME_STEPS = 16

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._volume = 40
        self._muted = False
        self._volumeSteps = self.VOLUME_STEPS
        self._shuffle = False
        self._repeat = RepeatMode.Off

        # Remote positions arrive about once a second and a poll can repeat
        # the previous value when the remote has no newer one. Run a local
        # clock between those readings so the display keeps advancing, and
        # re-anchor it whenever the remote reports a different position.
        self._clockPos = 0.0
        self._clockAt = time.monotonic()
        self._clockPlaying = False
        self._clockRaw: int | None = None
        self.positionChanged.connect(self._clockOnReading)
        self.playStateChanged.connect(self._clockOnPlayState)
        self.trackChanged.connect(self._clockOnTrack)
        self._clockTimer = QTimer(self)
        self._clockTimer.setInterval(self._CLOCK_MS)
        self._clockTimer.timeout.connect(self._clockTick)
        self._clockTimer.start()

    # -- lifecycle -------------------------------------------------------- #
    def connect(self) -> None:
        """Open the connection to the backend; backends override."""

    def close(self) -> None:
        """Close the connection; backends override."""

    def isStopped(self) -> bool:
        """True when playback is stopped (as opposed to paused)."""
        return False

    # -- server information ----------------------------------------------- #
    def serverLabel(self) -> str:
        """Address of the connected server, e.g. ``host:port``; empty if none."""
        return ""

    def serverState(self) -> str:
        """Connection state shown in Settings: ``Connected`` or ``Disconnected``."""
        return "Disconnected"

    def latencyMs(self) -> float | None:
        """Round-trip time of the last exchange in milliseconds, or None."""
        return None

    def hostPingState(self) -> tuple[str, float | None]:
        """ICMP host ping while offline: ``("ok", ms)``, ``("timeout", None)``
        or ``("unknown", None)`` before the first attempt finishes."""
        return ("unknown", None)

    def retryAttempt(self) -> int:
        """How many reconnects have been attempted since the last success."""
        return 0

    def outputs(self) -> list[Output]:
        """Available audio outputs; empty when the backend has none."""
        return []

    def selectOutput(self, outputId: int) -> bool:
        """Enable one output and disable the others."""
        return False

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #
    @abstractmethod
    def isPlaying(self) -> bool: ...

    @abstractmethod
    def currentTrack(self) -> Track | None: ...

    @abstractmethod
    def duration(self) -> int: ...

    @abstractmethod
    def play(self) -> None: ...

    @abstractmethod
    def pause(self) -> None: ...

    def togglePlay(self) -> None:
        self.pause() if self.isPlaying() else self.play()

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def next(self) -> None: ...

    @abstractmethod
    def previous(self) -> None: ...

    @abstractmethod
    def seek(self, seconds: int) -> None: ...

    def seekBy(self, delta: int) -> None:
        self.seek(max(0, min(self.duration(), self.positionNow() + delta)))

    def positionNow(self) -> int:
        """Elapsed seconds including time since the last remote reading."""
        pos = self._clockPos
        if self._clockPlaying:
            pos += time.monotonic() - self._clockAt
        duration = self.duration()
        if duration > 0:
            pos = min(pos, float(duration))
        return max(0, int(pos))

    # ------------------------------------------------------------------ #
    # Volume; state lives in the base class, backends hook _applyVolume
    # ------------------------------------------------------------------ #
    def volume(self) -> int:
        return self._volume

    def volumeMaximum(self) -> int:
        """Maximum value returned by volume() for this backend."""
        return 100

    def volumeSteps(self) -> int:
        """Number of notches across the full volume scale (16 or 64)."""
        return self._volumeSteps

    def volumeDisplayLevel(self) -> int:
        """Return a common 0..100 value for volume indicators."""
        maximum = max(1, self.volumeMaximum())
        return max(0, min(100, (self.volume() * 100 + maximum // 2) // maximum))

    def _snapVolume(self, level: int) -> int:
        """Snap a volume to the nearest configured step."""
        maximum = max(1, self.volumeMaximum())
        steps = self._volumeSteps
        index = int(max(0, min(maximum, int(level))) * steps / maximum + 0.5)
        return int(maximum * index / steps + 0.5)

    def setVolume(self, level: int) -> None:
        level = self._snapVolume(level)
        if level == self._volume and not self._muted:
            return
        self._volume = level
        self._muted = False  # Adjusting the volume always unmutes.
        self._applyVolume()
        self.volumeChanged.emit(self._volume, self._muted)

    def adjustVolume(self, delta: int) -> None:
        """Move whole volume steps, snapping off-grid levels first."""
        if delta == 0:
            return
        maximum = max(1, self.volumeMaximum())
        steps = self._volumeSteps
        index = int(self._volume * steps / maximum + 0.5)
        index = max(0, min(steps, index + delta))
        self.setVolume(int(maximum * index / steps + 0.5))

    def isMuted(self) -> bool:
        return self._muted

    def setMuted(self, muted: bool) -> None:
        if muted == self._muted:
            return
        self._muted = muted
        self._applyVolume()
        self.volumeChanged.emit(self._volume, self._muted)

    def toggleMute(self) -> None:
        self.setMuted(not self._muted)

    def _applyVolume(self) -> None:
        """Push volume/mute state to the remote device; optional hook."""

    # ------------------------------------------------------------------ #
    # Playback options
    # ------------------------------------------------------------------ #
    def shuffle(self) -> bool:
        return self._shuffle

    def setShuffle(self, enabled: bool) -> None:
        self._shuffle = bool(enabled)
        self.playbackOptionsChanged.emit()

    def repeatMode(self) -> RepeatMode:
        return self._repeat

    def setRepeatMode(self, mode: RepeatMode) -> None:
        self._repeat = mode
        self.playbackOptionsChanged.emit()

    # ------------------------------------------------------------------ #
    # Library queries
    # ------------------------------------------------------------------ #
    @abstractmethod
    def artists(self) -> list[str]: ...

    @abstractmethod
    def albums(self) -> list[Album]: ...

    @abstractmethod
    def songs(self) -> list[Track]: ...

    @abstractmethod
    def genres(self) -> list[str]: ...

    @abstractmethod
    def albumsByArtist(self, artist: str) -> list[Album]: ...

    @abstractmethod
    def albumsByGenre(self, genre: str) -> list[Album]: ...

    @abstractmethod
    def tracksByAlbum(self, album: Album) -> list[Track]: ...

    def albumForTrack(self, track: Track) -> Album | None:
        """Resolve a playing track to the album identity used by the library."""
        for album in self.albums():
            if album.title == track.album and album.artist == (
                track.album_artist or track.artist
            ):
                return album
        matches = [album for album in self.albums() if album.title == track.album]
        return matches[0] if len(matches) == 1 else None

    @abstractmethod
    def playTrack(self, track: Track) -> None: ...

    @abstractmethod
    def playAlbum(self, album: Album, startIndex: int = 0) -> None: ...

    def addAlbumToQueue(self, album: Album) -> None:
        """Append every recording on an album to the current queue."""
        raise NotImplementedError

    def shufflePlayAll(self) -> None:
        """Replace the queue with the whole library and play it shuffled."""
        raise NotImplementedError

    @abstractmethod
    def queuePosition(self) -> int:
        """1-based position of the current recording in the queue; 0 if none."""

    @abstractmethod
    def queueLength(self) -> int:
        """Number of recordings in the current queue."""

    def queueTracks(self) -> list[Track]:
        """Return the current queue, in order. Empty when unsupported."""
        return []

    def queuePlayNext(self, index: int) -> None:
        """Move the queued item at zero-based index directly after the current item."""

    def queueRemove(self, index: int) -> None:
        """Remove the queued item at zero-based index when supported."""

    def queueClear(self) -> None:
        """Clear queued items while keeping the current recording intact."""

    def queueIndex(self) -> int:
        """0-based index of the current recording in the queue; -1 if none."""
        position = self.queuePosition()
        return position - 1 if position > 0 else -1

    # ------------------------------------------------------------------ #
    # Playlists and starred tracks (optional; backends without storage
    # return supportsPlaylists() == False)
    # ------------------------------------------------------------------ #
    def supportsPlaylists(self) -> bool:
        return False

    def playlists(self) -> list[str]:
        return []

    def playlistTracks(self, name: str) -> list[Track]:
        return []

    def playlistContains(self, name: str, track: Track) -> bool:
        return any(candidate == track for candidate in self.playlistTracks(name))

    def addTrackToPlaylist(self, name: str, track: Track) -> bool:
        return False

    def removeTrackFromPlaylist(self, name: str, index: int) -> bool:
        return False

    def playPlaylist(self, name: str, startIndex: int = 0) -> None:
        """Replace the queue with a playlist and start playing at a row."""

    def addPlaylistToQueue(self, name: str) -> None:
        """Append every recording in a stored playlist to the current queue."""
        raise NotImplementedError

    def createPlaylist(self, name: str) -> bool:
        """Create an empty playlist; False when the backend cannot."""
        return False

    def deletePlaylist(self, name: str) -> bool:
        return False

    def isStarred(self, track: Track) -> bool:
        return self.playlistContains(STARRED_PLAYLIST, track)

    def toggleStarred(self, track: Track) -> bool:
        """Add or remove a recording from the Stared playlist."""
        if track is None or not self.supportsPlaylists():
            return False
        if self.isStarred(track):
            matches = [
                index
                for index, candidate in enumerate(
                    self.playlistTracks(STARRED_PLAYLIST)
                )
                if candidate == track
            ]
            for index in reversed(matches):
                self.removeTrackFromPlaylist(STARRED_PLAYLIST, index)
            starred = False
        else:
            starred = self.addTrackToPlaylist(STARRED_PLAYLIST, track)
        self.starredChanged.emit(starred)
        return starred

    # ------------------------------------------------------------------ #
    # Album art
    # ------------------------------------------------------------------ #
    def albumCover(self, album: Album) -> bytes | None:
        """Return PNG cover art for an album, or None when unavailable."""
        return None

    def albumThumbnail(self, album: Album) -> bytes | None:
        """Small pre-scaled cover for list rows; defaults to the full cover."""
        return self.albumCover(album)

    def albumCoverStatus(self, album: Album) -> str:
        """One of ``ready``, ``missing``, ``pending`` or ``unknown``."""
        return "missing"

    def albumCoverCached(self, album: Album) -> bool:
        """True when the album cover can be served without fetching."""
        return False

    def libraryLoaded(self) -> bool:
        """True when a library snapshot is available for caching/browsing."""
        return True

    def clearCoverCache(self) -> None:
        """Drop cached artwork (memory and disk); backends may override."""

    def coverCacheSize(self) -> int:
        """Bytes used by the on-disk artwork cache; 0 when unknown."""
        return 0

    def reload(self) -> None:
        """Drop in-memory caches without restarting the program."""

    def trackCover(self, track: Track) -> bytes | None:
        """Return PNG cover art for a recording, or None when unavailable."""
        return None

    def trackThumbnail(self, track: Track) -> bytes | None:
        """Small pre-scaled cover for list rows; defaults to the full cover."""
        return self.trackCover(track)

    def coverStatus(self, track: Track) -> str:
        """One of ``ready``, ``missing``, ``pending`` or ``unknown``.

        ``missing`` means the server explicitly has no artwork, so the UI
        should not retry loading it.
        """
        return "missing"

    # ------------------------------------------------------------------ #
    # Playback clock
    # ------------------------------------------------------------------ #
    def _clockOnReading(self, position: float) -> None:
        """Re-anchor on a new remote position; a repeated value is stale."""
        position = max(0.0, float(position))
        if position == self._clockRaw:
            return
        self._clockRaw = position
        self._clockPos = position
        self._clockAt = time.monotonic()

    def _clockOnPlayState(self, playing: bool) -> None:
        now = time.monotonic()
        if self._clockPlaying and not playing:
            self._clockPos += now - self._clockAt
        self._clockAt = now
        self._clockPlaying = bool(playing)

    def _clockOnTrack(self, _track: Track | None) -> None:
        self._clockRaw = None
        self._clockPos = 0.0
        self._clockAt = time.monotonic()

    def _clockTick(self) -> None:
        if self._clockPlaying:
            self.positionTick.emit(self.positionNow())
