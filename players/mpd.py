"""MPD-backed implementation of the player interface.

The daemon is polled on a background timer; transport commands are sent
synchronously. Library queries are cached and invalidated on demand.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QTimer, Qt, Signal
from PySide6.QtGui import QImage

from debuglog import getLogger

from .base import (
    STARRED_PLAYLIST,
    Album,
    PlayerBackend,
    RepeatMode,
    Track,
)
from .mpdclient import MpdClient, MpdError

_log = getLogger("mpd")

_POLL_MS = 1000
# Cover art is fetched over its own connection. A slow disk or Wi-Fi can make
# a single chunk take longer than the control timeout, so allow more time here.
_COVER_TIMEOUT = 10.0
_COVER_WORKERS = 3
# Small pre-scaled artwork for list rows. Decoding a full 1 MPix cover in the
# paint path costs ~18 ms; a 72 px PNG decodes almost instantly.
_THUMB_SIZE = 72
# Loading the whole database is a one-off bulk transfer and must not use the
# short control timeout; a 20k-track listallinfo can take well over a second.
_SNAPSHOT_TIMEOUT = 20.0
# A status poll slower than this suggests the server or network is struggling.
_SLOW_POLL_MS = 250.0
_HEARTBEAT_TICKS = 60
_PING_TIMEOUT_MS = 1000
_PING_RE = re.compile(rb"([<=]?)\s*(\d+(?:[.,]\d+)?)\s*ms", re.IGNORECASE)


def _icmpPingMs(host: str) -> float | None:
    """Round-trip time to the host using the system ping command, in ms."""
    if os.name == "nt":
        command = ["ping", "-n", "1", "-w", str(_PING_TIMEOUT_MS), host]
    elif sys.platform == "darwin":
        command = ["ping", "-c", "1", "-W", str(_PING_TIMEOUT_MS), host]
    else:
        command = ["ping", "-c", "1", "-W", "1", host]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=_PING_TIMEOUT_MS / 1000 + 1.5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        # No reply (Windows/Linux print 0 ms statistics even on total loss).
        return None
    match = _PING_RE.search((result.stdout or b"") + (result.stderr or b""))
    if match is None:
        return None
    value = float(match.group(2).replace(b",", b"."))
    if match.group(1) == b"<":
        value = max(0.1, value / 2)
    return value


def _isCommandError(exc: BaseException) -> bool:
    """True for an MPD ACK: the command failed but the socket is healthy."""
    return isinstance(exc, MpdError) and str(exc).startswith("ACK ")


class MpdPlayerBackend(PlayerBackend):
    """Playback backend speaking the Music Player Daemon protocol."""

    connectionLost = Signal()
    connectionRestored = Signal()

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6600,
        password: str = "",
        timeout: float = 2.0,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._host = host
        self._port = int(port)
        self._password = password or None
        self._timeout = float(timeout) if timeout else 2.0
        self._client = MpdClient(host, self._port, timeout=self._timeout)
        self._connected = False
        self._latencyMs: float | None = None
        _log.info(
            "MPD backend %s:%s (timeout %.0f ms)",
            self._host,
            self._port,
            self._timeout * 1000,
        )
        try:
            self._client.connect()
            if self._password:
                self._client.password(self._password)
            self._connected = True
        except Exception as exc:  # noqa: BLE001
            _log.warning("initial MPD connect failed: %s", exc)

        self._playing = False
        self._state = "stop"
        self._track: Track | None = None
        self._position = 0
        self._duration = 0
        self._queuePos = 0
        self._queueLen = 0
        self._stopping = False
        self._ticks = 0
        # Host ping runs only while the daemon is unreachable.
        self._hostPingMs: float | None = None
        self._hostPingState = "unknown"
        self._pingStop = threading.Event()
        self._pingStop.set()
        self._pingThread: threading.Thread | None = None

        # Reconnect probes run on a background thread: connecting to a dead
        # host can block for the whole socket timeout, which must never happen
        # on the GUI thread while the connection overlay is showing.
        self._reconnectStop = threading.Event()
        self._reconnectStop.set()
        self._reconnectThread: threading.Thread | None = None
        self._reconnectReady = False
        self._retryAttempts = 0
        self._lossEmitted = False

        self._trackCache: dict[str, Track] = {}
        # Library snapshot: the whole collection is fetched once and reused
        # until it is explicitly invalidated.
        self._library: list[Track] | None = None
        self._albums: list[Album] | None = None
        self._artists: list[str] | None = None
        self._genres: list[str] | None = None
        self._starredUris: set[str] | None = None
        self._coverCache: dict[str, bytes | None] = {}
        self._coverPending: set[str] = set()
        self._coverMissing: set[str] = set()
        self._thumbCache: dict[str, bytes] = {}
        self._thumbPending: set[str] = set()
        self._thumbMissing: set[str] = set()
        self._thumbRetry: dict[str, float] = {}
        self._coverLock = threading.RLock()
        self._coverGeneration = 0
        self._coverExecutor: ThreadPoolExecutor | None = ThreadPoolExecutor(
            max_workers=_COVER_WORKERS, thread_name_prefix="mpd-cover"
        )
        self._albumFirstUri: dict[tuple[str, str], str] = {}

        self._poll = QTimer(self)
        self._poll.setInterval(_POLL_MS)
        self._poll.timeout.connect(self._tick)
        self._poll.start()

        self.setVolume(40)
        self._tick()

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #
    def isPlaying(self) -> bool:
        return self._playing

    def currentTrack(self) -> Track | None:
        return self._track

    def duration(self) -> int:
        return self._duration

    def play(self) -> None:
        self._send("play")

    def pause(self) -> None:
        self._send("pause 1")

    def stop(self) -> None:
        self._send("stop")

    def next(self) -> None:
        self._send("next")

    def previous(self) -> None:
        self._send("previous")

    def seek(self, seconds: int) -> None:
        self._send(f"seekcur {max(0, int(seconds))}")

    # ------------------------------------------------------------------ #
    # Volume; MPD stores 0..100, matching the interface.
    # ------------------------------------------------------------------ #
    def _applyVolume(self) -> None:
        self._send(f"setvol {self._volume}")

    # ------------------------------------------------------------------ #
    # Playback options
    # ------------------------------------------------------------------ #
    def setShuffle(self, enabled: bool) -> None:
        # MPD is the source of truth; _tick() syncs random/repeat back.
        self._send(f"random {1 if enabled else 0}")

    def setRepeatMode(self, mode: RepeatMode) -> None:
        self._batchCommands([
            f"repeat {0 if mode is RepeatMode.Off else 1}",
            f"single {1 if mode is RepeatMode.One else 0}",
        ])

    # ------------------------------------------------------------------ #
    # Library queries
    # ------------------------------------------------------------------ #
    def artists(self) -> list[str]:
        if self._artists is None:
            values = self._values("list AlbumArtist", "AlbumArtist")
            if values is None:
                return []
            self._artists = values
        return list(self._artists)

    def genres(self) -> list[str]:
        if self._genres is None:
            values = self._values("list Genre", "Genre")
            if values is None:
                return []
            self._genres = values
        return list(self._genres)

    def albums(self) -> list[Album]:
        if self._albums is not None:
            return list(self._albums)
        # Derive albums from track metadata, keyed by artist and title.
        albums: dict[tuple[str, str], Album] = {}
        trackCounts: dict[tuple[str, str], int] = {}
        addedTimes: dict[tuple[str, str], int] = {}
        tracks = self._allTracks()
        if self._library is None:
            # The snapshot failed to load; do not cache an empty result.
            return []
        for track in tracks:
            albumArtist = track.album_artist or track.artist
            key = (albumArtist, track.album)
            if track.album:
                trackCounts[key] = trackCounts.get(key, 0) + 1
                # An album counts as added when its newest track arrived.
                addedTimes[key] = max(addedTimes.get(key, 0), track.added)
            if track.album and key not in albums:
                albums[key] = Album(
                    title=track.album,
                    artist=albumArtist,
                    year=track.year,
                    genre=track.genre,
                    added=track.added,
                )
        albums = {
            key: Album(
                title=album.title,
                artist=album.artist,
                year=album.year,
                genre=album.genre,
                added=addedTimes.get(key, 0),
                track_count=trackCounts.get(key, 0),
            )
            for key, album in albums.items()
        }
        self._albums = sorted(albums.values(), key=lambda a: (a.artist, a.title))
        return list(self._albums)

    def songs(self) -> list[Track]:
        return sorted(self._allTracks(), key=lambda t: (t.title, t.artist))

    def albumsByArtist(self, artist: str) -> list[Album]:
        return [a for a in self.albums() if a.artist == artist]

    def albumsByGenre(self, genre: str) -> list[Album]:
        return [a for a in self.albums() if a.genre == genre]

    def tracksByAlbum(self, album: Album) -> list[Track]:
        tracks = [
            t for t in self._allTracks()
            if t.album == album.title
            and (t.album_artist or t.artist) == album.artist
        ]
        return sorted(tracks, key=lambda t: (t.disc_number, t.track_number))

    def playTrack(self, track: Track) -> None:
        self._batchCommands(
            ["clear", f'add {self._quote(self._uri(track))}', "play"]
        )

    def playAlbum(self, album: Album, startIndex: int = 0) -> None:
        tracks = self.tracksByAlbum(album)
        if not tracks:
            return
        commands = ["clear"]
        commands.extend(f"add {self._quote(self._uri(t))}" for t in tracks)
        # Sequential playback: MPD's random mode must be off.
        commands.append("random 0")
        commands.append(f"play {max(0, startIndex)}")
        # _batchCommands() ticks and syncs the shuffle state from MPD.
        self._batchCommands(commands)

    def addAlbumToQueue(self, album: Album) -> None:
        tracks = self.tracksByAlbum(album)
        if not tracks:
            return
        commands = [f"add {self._quote(self._uri(t))}" for t in tracks]
        if self._batchCommands(commands) and not self._playing:
            self._send("play")

    def shufflePlayAll(self) -> None:
        """Clear the queue, add the whole library and let MPD shuffle it.

        Adding every URI one by one meant tens of thousands of round trips on
        a large library. ``findadd`` with a match-all filter adds the database
        in one command, and MPD's own random mode determines play order.
        """
        commands = [
            "clear",
            "findadd \"(file != '')\"",
            "random 1",
            "play",
        ]
        # _batchCommands() ticks and syncs the shuffle state from MPD.
        self._batchCommands(commands)

    def queuePosition(self) -> int:
        return self._queuePos

    def queueLength(self) -> int:
        return self._queueLen

    def queueTracks(self) -> list[Track]:
        rows = self._commandList(["playlistinfo"])
        return self._tracksFromRows(rows)

    def queuePlayNext(self, index: int) -> None:
        current = self._queuePos - 1
        if index < 0 or index >= self._queueLen or index == current or current < 0:
            return
        self._send(f"move {index} {current + 1}")

    def queueRemove(self, index: int) -> None:
        if 0 <= index < self._queueLen:
            self._send(f"delete {index}")

    def queueClear(self) -> None:
        current = self._queuePos - 1
        if current < 0:
            commands = ["clear"] if self._queueLen else []
        else:
            # Remove tracks after the current one first. Then remove tracks
            # before it from right to left; each deletion shifts the current
            # position down, but leaves the playing track itself in place.
            commands = [
                *(f"delete {index}" for index in range(self._queueLen - 1, current, -1)),
                *(f"delete {index}" for index in range(current - 1, -1, -1)),
            ]
        if commands:
            self._commandList(commands)
            self._tick()

    # ------------------------------------------------------------------ #
    # Playlists and starreds
    # ------------------------------------------------------------------ #
    def supportsPlaylists(self) -> bool:
        return True

    def playlists(self) -> list[str]:
        rows = self._commandList(["listplaylists"])
        names = {
            row.split(":", 1)[1].strip()
            for row in rows
            if row.startswith("playlist:")
        }
        names.discard("")
        ordered = sorted(names, key=str.lower)
        if STARRED_PLAYLIST in ordered:
            ordered.remove(STARRED_PLAYLIST)
        return [STARRED_PLAYLIST, *ordered]

    def playlistTracks(self, name: str) -> list[Track]:
        if not name:
            return []
        rows = self._playlistRows(f'listplaylistinfo {self._quote(name)}')
        return self._tracksFromRows(rows)

    def playlistContains(self, name: str, track: Track) -> bool:
        if name == STARRED_PLAYLIST:
            return self.isStarred(track)
        uri = self._uri(track)
        return any(self._uri(candidate) == uri for candidate in self.playlistTracks(name))

    def addTrackToPlaylist(self, name: str, track: Track) -> bool:
        if not name:
            return False
        ok = self._playlistCommand(
            f'playlistadd {self._quote(name)} {self._quote(self._uri(track))}'
        )
        if ok:
            self._afterPlaylistEdit()
        return ok

    def removeTrackFromPlaylist(self, name: str, index: int) -> bool:
        if not name or index < 0:
            return False
        ok = self._playlistCommand(
            f'playlistdelete {self._quote(name)} {int(index)}'
        )
        if ok:
            self._afterPlaylistEdit()
        return ok

    def playPlaylist(self, name: str, startIndex: int = 0) -> None:
        if not name:
            return
        commands = [
            "clear",
            f'load {self._quote(name)}',
            # Sequential playback: MPD's random mode must be off.
            "random 0",
            f"play {max(0, int(startIndex))}",
        ]
        # _batchCommands() ticks and syncs the shuffle state from MPD.
        self._batchCommands(commands)

    def addPlaylistToQueue(self, name: str) -> None:
        tracks = self.playlistTracks(name)
        if not tracks:
            return
        commands = [
            f'add {self._quote(self._uri(track))}' for track in tracks
        ]
        if not self._batchCommands(commands):
            return
        if not self._playing:
            self._send("play")

    def createPlaylist(self, name: str) -> bool:
        name = name.strip()
        if not name or name == STARRED_PLAYLIST or name in self.playlists():
            return False
        # MPD has no command for an empty playlist. Saving the queue creates
        # the file without touching playback, then the copy is emptied.
        if not self._playlistCommand(f'save {self._quote(name)}'):
            return False
        if not self._playlistCommand(f'playlistclear {self._quote(name)}'):
            return False
        self._afterPlaylistEdit()
        return True

    def deletePlaylist(self, name: str) -> bool:
        if not name or name == STARRED_PLAYLIST:
            return False
        ok = self._playlistCommand(f'rm {self._quote(name)}')
        if ok:
            self._afterPlaylistEdit()
        return ok

    def isStarred(self, track: Track) -> bool:
        uri = self._uri(track)
        if self._starredUris is None:
            self._starredUris = {
                self._uri(candidate)
                for candidate in self.playlistTracks(STARRED_PLAYLIST)
            }
        return uri in self._starredUris

    def toggleStarred(self, track: Track) -> bool:
        uri = self._uri(track)
        if self.isStarred(track):
            tracks = self.playlistTracks(STARRED_PLAYLIST)
            matches = [
                index
                for index, candidate in enumerate(tracks)
                if self._uri(candidate) == uri
            ]
            for index in reversed(matches):
                self.removeTrackFromPlaylist(STARRED_PLAYLIST, index)
            starred = False
        else:
            starred = self.addTrackToPlaylist(STARRED_PLAYLIST, track)
        self.starredChanged.emit(starred)
        return starred

    def _playlistRows(self, command: str) -> list[str]:
        try:
            return self._client.command(command)
        except MpdError:
            return []
        except OSError:
            self.connectionLost.emit()
            return []

    def _playlistCommand(self, command: str) -> bool:
        try:
            self._client.command(command)
        except MpdError:
            return False
        except OSError:
            self.connectionLost.emit()
            return False
        self._tick()
        return True

    def _batchCommands(self, commands: list[str]) -> bool:
        if not commands:
            return False
        try:
            self._client.commandList(commands)
        except MpdError:
            return False
        except OSError:
            self.connectionLost.emit()
            return False
        self._tick()
        return True

    def _afterPlaylistEdit(self) -> None:
        self._starredUris = None
        self.playlistsChanged.emit()

    @staticmethod
    def _quote(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    # ------------------------------------------------------------------ #
    # Album art
    # ------------------------------------------------------------------ #
    def trackCover(self, track: Track) -> bytes | None:
        uri = self._uri(track)
        with self._coverLock:
            cached = self._coverCache.get(uri)
            if cached is not None:
                return cached
            if uri in self._coverPending or uri in self._coverMissing:
                return None
        cachePath = self._coverCachePath(uri)
        try:
            cached = cachePath.read_bytes()
        except OSError:
            cached = None
        if cached:
            with self._coverLock:
                self._coverCache[uri] = cached
            return cached
        with self._coverLock:
            if uri in self._coverPending or uri in self._coverMissing:
                return self._coverCache.get(uri)
            executor = self._coverExecutor
            if executor is None:
                return None
            self._coverPending.add(uri)
            generation = self._coverGeneration
            future = executor.submit(self._fetchCover, uri, cachePath, generation)
            future.add_done_callback(
                lambda done: self._onCoverLoaded(uri, generation, done)
            )
        return None

    def trackThumbnail(self, track: Track) -> bytes | None:
        """Small pre-scaled artwork for list rows.

        Full-size artwork is never decoded on the paint path: if only the large
        image exists, its thumbnail is generated in a worker and the row is
        refreshed through ``coverChanged`` when it is ready.
        """
        uri = self._uri(track)
        with self._coverLock:
            if uri in self._thumbMissing:
                return None
            cached = self._thumbCache.get(uri)
            if cached:
                return cached
            if uri in self._thumbPending:
                return None

        thumbPath = self._thumbPath(uri)
        try:
            data = thumbPath.read_bytes()
        except OSError:
            data = None
        if data:
            with self._coverLock:
                self._cacheThumbnail(uri, data)
            return data

        with self._coverLock:
            full = self._coverCache.get(uri)
            executor = self._coverExecutor
            if executor is None:
                return None
            if full is None and uri in self._coverPending:
                return None

        if full is None:
            try:
                full = self._coverCachePath(uri).read_bytes()
            except OSError:
                full = None
        if full is None:
            # Start the normal cover fetch; _fetchCover writes the thumbnail.
            # Do not retry a failing fetch on every repaint.
            now = time.monotonic()
            with self._coverLock:
                if self._thumbRetry.get(uri, 0.0) > now:
                    return None
                self._thumbRetry[uri] = now + 5.0
            self.trackCover(track)
            return None

        with self._coverLock:
            if uri in self._thumbPending:
                return None
            executor = self._coverExecutor
            if executor is None:
                return None
            self._thumbPending.add(uri)
            generation = self._coverGeneration
        future = executor.submit(
            self._makeThumbTask, uri, full, thumbPath, generation
        )
        future.add_done_callback(
            lambda done: self._onThumbnailLoaded(uri, generation, done)
        )
        return None

    def coverStatus(self, track: Track) -> str:
        """One of ``ready``, ``missing``, ``pending`` or ``unknown``.

        ``missing`` means the server explicitly reported no artwork, so
        callers should not retry. ``unknown`` may become available later.
        """
        uri = self._uri(track)
        with self._coverLock:
            if self._coverCache.get(uri):
                return "ready"
            if uri in self._coverMissing:
                return "missing"
            if uri in self._coverPending:
                return "pending"
        return "unknown"

    def albumCover(self, album: Album) -> bytes | None:
        uri = self._albumFirstUri.get((album.artist, album.title))
        if uri is None:
            return None
        track = self._trackCache.get(uri)
        if track is None:
            return None
        return self.trackCover(track)

    def albumThumbnail(self, album: Album) -> bytes | None:
        uri = self._albumFirstUri.get((album.artist, album.title))
        track = self._trackCache.get(uri) if uri else None
        if track is None:
            return None
        return self.trackThumbnail(track)

    def albumCoverStatus(self, album: Album) -> str:
        """Cover state for the album's first track (used by idle caching)."""
        uri = self._albumFirstUri.get((album.artist, album.title))
        track = self._trackCache.get(uri) if uri else None
        if track is None:
            return "missing"
        return self.coverStatus(track)

    def albumCoverCached(self, album: Album) -> bool:
        """True when the cover is already in memory or the disk cache."""
        uri = self._albumFirstUri.get((album.artist, album.title))
        if uri is None:
            return False
        with self._coverLock:
            if self._coverCache.get(uri):
                return True
        return self._coverCachePath(uri).exists()

    def libraryLoaded(self) -> bool:
        return self._library is not None

    def clearCoverCache(self) -> None:
        """Delete all cached artwork (memory and disk) for this server."""
        with self._coverLock:
            self._coverGeneration += 1
            self._coverCache.clear()
            self._coverPending.clear()
            self._coverMissing.clear()
            self._thumbCache.clear()
            self._thumbPending.clear()
            self._thumbMissing.clear()
            self._thumbRetry.clear()
        shutil.rmtree(self._coverCacheRoot(), ignore_errors=True)
        _log.info("cover cache cleared")
        self.coverChanged.emit()

    def coverCacheSize(self) -> int:
        """Bytes used by the artwork cache on disk for this server."""
        total = 0
        try:
            for path in self._coverCacheRoot().rglob("*"):
                if path.is_file():
                    total += path.stat().st_size
        except OSError:
            return total
        return total

    def reload(self) -> None:
        """Drop in-memory caches without restarting the application."""
        self._invalidateLibrary()
        self._trackCache.clear()
        self._albumFirstUri.clear()
        self._starredUris = None
        with self._coverLock:
            self._coverCache.clear()
            self._coverMissing.clear()
            self._thumbCache.clear()
            self._thumbMissing.clear()
            self._thumbRetry.clear()
        _log.info("in-memory caches reloaded")
        self.libraryChanged.emit()
        self.coverChanged.emit()

    def _coverCacheRoot(self) -> Path:
        appData = os.environ.get("APPDATA")
        root = (
            Path(appData) if appData else Path.home() / ".config"
        ) / "headunit" / "cover-cache" / "mpd"
        serverKey = hashlib.sha256(
            f"{self._host}:{self._port}".encode("utf-8")
        ).hexdigest()[:16]
        return root / serverKey

    def _coverCachePath(self, uri: str) -> Path:
        uriKey = hashlib.sha256(uri.encode("utf-8")).hexdigest()
        return self._coverCacheRoot() / f"{uriKey}.img"

    @staticmethod
    def _writeCoverCache(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)

    def _thumbPath(self, uri: str) -> Path:
        uriKey = hashlib.sha256(uri.encode("utf-8")).hexdigest()
        return self._coverCacheRoot() / "thumbs" / f"{uriKey}.png"

    def _cacheThumbnail(self, uri: str, data: bytes) -> None:
        """Keep the memory thumbnail cache bounded; disk keeps everything."""
        self._thumbCache[uri] = data
        while len(self._thumbCache) > 512:
            self._thumbCache.pop(next(iter(self._thumbCache)))

    @staticmethod
    def _makeThumbnail(data: bytes) -> bytes | None:
        image = QImage.fromData(data)
        if image.isNull():
            return None
        scaled = image.scaled(
            _THUMB_SIZE,
            _THUMB_SIZE,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        if scaled.width() > _THUMB_SIZE or scaled.height() > _THUMB_SIZE:
            scaled = scaled.copy(
                (scaled.width() - _THUMB_SIZE) // 2,
                (scaled.height() - _THUMB_SIZE) // 2,
                _THUMB_SIZE,
                _THUMB_SIZE,
            )
        payload = QByteArray()
        buffer = QBuffer(payload)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        ok = scaled.save(buffer, "PNG")
        buffer.close()
        return bytes(payload) if ok else None

    def _makeThumbTask(
        self, uri: str, full: bytes, thumbPath: Path, generation: int
    ) -> bytes | None:
        thumb = self._makeThumbnail(full)
        if thumb and generation == self._coverGeneration:
            try:
                self._writeCoverCache(thumbPath, thumb)
            except OSError:
                pass
        return thumb

    def _onThumbnailLoaded(self, uri: str, generation: int, future) -> None:
        try:
            data = future.result()
        except Exception:  # noqa: BLE001 - artwork is optional
            data = None
        with self._coverLock:
            self._thumbPending.discard(uri)
            if generation != self._coverGeneration:
                return
            if data:
                self._cacheThumbnail(uri, data)
            else:
                self._thumbMissing.add(uri)
        if data:
            self.coverChanged.emit()

    def _fetchCover(
        self, uri: str, cachePath: Path, generation: int
    ) -> tuple[str, bytes | None]:
        client = MpdClient(self._host, self._port, timeout=_COVER_TIMEOUT)
        started = time.monotonic()
        try:
            client.connect()
            if self._password:
                client.password(self._password)
            status, data = client.readPicture(uri)
            if status == "ok" and not data:
                # Not all files have embedded art; MPD's albumart also looks
                # for cover.jpg/png in the folder.
                status, data = client.albumArt(uri)
            if data and generation == self._coverGeneration:
                try:
                    self._writeCoverCache(cachePath, data)
                except OSError:
                    pass
                thumb = self._makeThumbnail(data)
                if thumb:
                    try:
                        self._writeCoverCache(self._thumbPath(uri), thumb)
                    except OSError:
                        pass
            _log.debug(
                "cover %s: %s in %.0f ms",
                uri,
                f"{len(data)} bytes" if data else status,
                (time.monotonic() - started) * 1000,
            )
            return status, data
        except (MpdError, OSError) as exc:
            _log.debug(
                "cover %s failed after %.0f ms: %s",
                uri,
                (time.monotonic() - started) * 1000,
                exc,
            )
            return "error", None
        finally:
            client.close()

    def _onCoverLoaded(self, uri: str, generation: int, future) -> None:
        try:
            status, data = future.result()
        except Exception:  # noqa: BLE001 - cover art is optional
            status, data = "error", None
        with self._coverLock:
            if generation != self._coverGeneration:
                return
            self._coverPending.discard(uri)
            self._thumbRetry.pop(uri, None)
            if data:
                self._coverCache[uri] = data
            elif status == "ok":
                # The server answered and the file has no artwork.
                self._coverMissing.add(uri)
            # On a transport error leave the state unknown so a later request
            # (or the Now Playing retry timer) can try again.
        if data or status == "ok":
            self.coverChanged.emit()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _send(self, command: str) -> None:
        _log.debug("transport command: %s", command)
        if not self._connected:
            self._beginReconnect()
            _log.debug("transport command skipped while offline: %s", command)
            return
        try:
            self._client.command(command)
        except (MpdError, OSError) as exc:
            _log.warning("transport command %r failed: %s", command, exc)
            if not _isCommandError(exc):
                self._markDisconnected()
            return
        self._tick()

    def _values(self, command: str, tag: str) -> list[str] | None:
        """Return the sorted tag values, or None when the query failed."""
        prefix = f"{tag}:"
        if not self._connected:
            self._beginReconnect()
            return None
        try:
            rows = self._client.commandList([command])
        except (MpdError, OSError) as exc:
            _log.warning("%s failed: %s", command, exc)
            if not _isCommandError(exc):
                self._markDisconnected()
            return None
        return sorted(
            row.split(":", 1)[1].strip()
            for row in rows
            if row.startswith(prefix)
        )

    def _commandList(self, commands: list[str]) -> list[str]:
        if not self._connected:
            self._beginReconnect()
            return []
        try:
            return self._client.commandList(commands)
        except (MpdError, OSError) as exc:
            _log.warning("command list %r failed: %s", commands[:3], exc)
            if not _isCommandError(exc):
                self._markDisconnected()
            return []

    def _allTracks(self) -> list[Track]:
        """Return the cached library snapshot, loading it on first use.

        The snapshot is what makes browsing usable on a large collection: a
        single ``listallinfo`` can return hundreds of thousands of lines, so
        fetching it once and reusing the parsed tracks keeps page switches
        responsive. :meth:`reload` drops it to pick up library changes.
        """
        if self._library is not None:
            return self._library
        if not self._connected:
            self._beginReconnect()
            return []
        started = time.monotonic()
        _log.debug("loading library snapshot (from %s)", _caller())
        previousTimeout = self._client.timeout
        self._client.setTimeout(max(previousTimeout, _SNAPSHOT_TIMEOUT))
        try:
            rows = self._client.commandList(["listallinfo"])
        except (MpdError, OSError) as exc:
            _log.warning(
                "library snapshot failed after %.0f ms: %s",
                (time.monotonic() - started) * 1000,
                exc,
            )
            if not _isCommandError(exc):
                self._markDisconnected()
            return []
        finally:
            self._client.setTimeout(previousTimeout)
        tracks = self._tracksFromRows(rows, requireTime=True)
        self._library = tracks
        _log.info(
            "library snapshot: %d tracks in %.0f ms",
            len(tracks),
            (time.monotonic() - started) * 1000,
        )
        return tracks

    def _invalidateLibrary(self) -> None:
        self._library = None
        self._albums = None
        self._artists = None
        self._genres = None

    def _tracksFromRows(
        self, rows: list[str], requireTime: bool = False
    ) -> list[Track]:
        tracks: list[Track] = []
        current: dict[str, str] | None = None

        def flush() -> None:
            nonlocal current
            if current and "file" in current:
                if not requireTime or "Time" in current:
                    tracks.append(self._trackFromFields(current))
            current = None

        for row in rows:
            if row.startswith("file:"):
                flush()
                current = {"file": row[5:].strip()}
            elif current is not None and ":" in row:
                key, value = row.split(":", 1)
                # Position and Id are queue metadata, not tag values.
                if key not in ("Pos", "Id"):
                    current[key] = value.strip()
            # Non-keyed rows (e.g. directory entries) are ignored.
        flush()
        return tracks

    def _trackFromFields(self, fields: dict[str, str]) -> Track:
        uri = fields.get("file", "")
        try:
            duration = int(fields.get("Time", "0") or 0)
        except ValueError:
            duration = 0
        try:
            trackNo = int(fields.get("Track", "0") or 0)
        except ValueError:
            trackNo = 0
        try:
            # MPD may report "1" or "1/2".
            discNo = int(str(fields.get("Disc", "0") or 0).split("/", 1)[0])
        except ValueError:
            discNo = 0
        year = 0
        date = fields.get("Date", "")
        if date[:4].isdigit():
            year = int(date[:4])
        track = Track(
            title=fields.get("Title", uri.rsplit("/", 1)[-1]),
            artist=fields.get("Artist", fields.get("AlbumArtist", "")),
            album=fields.get("Album", ""),
            duration=duration,
            track_number=trackNo,
            genre=fields.get("Genre", ""),
            year=year,
            added=_songAdded(fields),
            album_artist=fields.get("AlbumArtist", fields.get("Artist", "")),
            disc_number=discNo,
        )
        self._trackCache[uri] = track
        albumKey = (track.album_artist or track.artist, track.album)
        self._albumFirstUri.setdefault(albumKey, uri)
        return track

    def _uri(self, track: Track) -> str:
        for uri, cached in self._trackCache.items():
            if cached == track:
                return uri
        # Fall back to a search by title and album.
        rows = self._commandList([f'find title "{track.title}"'])
        for row in rows:
            if row.startswith("file:"):
                return row[5:].strip()
        return track.title

    def isStopped(self) -> bool:
        return self._state == "stop"

    def serverLabel(self) -> str:
        """The MPD address shown in Settings."""
        return f"{self._host}:{self._port}"

    def serverState(self) -> str:
        return "Connected" if self._connected else "Disconnected"

    def latencyMs(self) -> float | None:
        """Round-trip time of the last status poll, in milliseconds."""
        return self._latencyMs

    def hostPingState(self) -> tuple[str, float | None]:
        """Latest ICMP result for the host while the daemon is unreachable."""
        return self._hostPingState, self._hostPingMs

    def retryAttempt(self) -> int:
        """Connection attempts made since the last successful connect."""
        return self._retryAttempts

    def _startHostPing(self) -> None:
        if self._pingThread is not None and self._pingThread.is_alive():
            return
        self._hostPingState = "unknown"
        self._hostPingMs = None
        self._pingStop.clear()
        self._pingThread = threading.Thread(
            target=self._pingLoop, name="mpd-ping", daemon=True
        )
        self._pingThread.start()

    def _stopHostPing(self) -> None:
        self._pingStop.set()
        self._pingThread = None
        self._hostPingMs = None
        self._hostPingState = "unknown"

    def _beginReconnect(self) -> None:
        """Probe the server in the background and flag when it is reachable."""
        if self._reconnectThread is not None and self._reconnectThread.is_alive():
            return
        self._reconnectStop.clear()
        self._reconnectThread = threading.Thread(
            target=self._reconnectLoop, name="mpd-reconnect", daemon=True
        )
        self._reconnectThread.start()

    def _reconnectLoop(self) -> None:
        client = MpdClient(self._host, self._port, timeout=self._timeout)
        delay = max(0.5, min(2.0, self._timeout))
        try:
            while not self._reconnectStop.is_set():
                self._retryAttempts += 1
                try:
                    client.connect()
                except (MpdError, OSError) as exc:
                    _log.debug(
                        "reconnect attempt %d failed: %s",
                        self._retryAttempts,
                        exc,
                    )
                    if self._reconnectStop.wait(delay):
                        return
                    delay = min(10.0, delay * 2)
                    continue
                client.close()
                if not self._reconnectStop.is_set():
                    self._reconnectReady = True
                return
        finally:
            client.close()

    def _stopReconnect(self) -> None:
        self._reconnectStop.set()
        self._reconnectThread = None
        self._reconnectReady = False

    def _markDisconnected(self) -> None:
        """Enter offline mode: stop using the socket and probe in background."""
        self._connected = False
        self._latencyMs = None
        self._reconnectReady = False
        self._startHostPing()
        self._beginReconnect()
        if not self._lossEmitted:
            self._lossEmitted = True
            self.connectionLost.emit()

    def _pingLoop(self) -> None:
        while not self._pingStop.is_set():
            value = _icmpPingMs(self._host)
            if self._pingStop.is_set():
                return
            if value is None:
                # Keep reporting timeout; never fall back to an old value.
                self._hostPingState = "timeout"
                self._hostPingMs = None
            else:
                self._hostPingState = "ok"
                self._hostPingMs = value
            self._pingStop.wait(1.0)

    def close(self) -> None:
        if self._poll is not None:
            self._poll.stop()
        self._stopping = True
        self._connected = False
        self._latencyMs = None
        self._lossEmitted = False
        self._retryAttempts = 0
        self._stopHostPing()
        self._stopReconnect()
        _log.info("MPD backend closing")
        with self._coverLock:
            self._coverGeneration += 1
            self._coverPending.clear()
            executor = self._coverExecutor
            self._coverExecutor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    def connect(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass
        self._stopping = False
        self._lossEmitted = False
        self._retryAttempts = 0
        self._reconnectReady = False
        self._coverExecutor = ThreadPoolExecutor(
            max_workers=_COVER_WORKERS, thread_name_prefix="mpd-cover"
        )
        self._client = MpdClient(self._host, self._port, timeout=self._timeout)
        try:
            self._client.connect()
            if self._password:
                self._client.password(self._password)
            self._connected = True
        except Exception as exc:  # noqa: BLE001
            self._connected = False
            _log.warning("reconnect to MPD failed: %s", exc)
        self._ticks = 0
        self._trackCache.clear()
        self._invalidateLibrary()
        self._starredUris = None
        with self._coverLock:
            self._coverGeneration += 1
            self._coverCache.clear()
            self._coverPending.clear()
            self._coverMissing.clear()
        self._albumFirstUri.clear()
        if self._poll is None:
            self._poll = QTimer(self)
            self._poll.setInterval(_POLL_MS)
            self._poll.timeout.connect(self._tick)
        self._poll.start()
        self._tick()
        self.libraryChanged.emit()
        if self._connected:
            self.connectionRestored.emit()

    def _syncPlaybackOptions(self, fields: dict[str, str]) -> bool:
        """Adopt random/repeat/single from MPD's status as the source of truth."""
        shuffleOn = fields.get("random", "0") == "1"
        repeatOn = fields.get("repeat", "0") == "1"
        single = fields.get("single", "0")
        if repeatOn and single in ("1", "oneshot"):
            repeat = RepeatMode.One
        elif repeatOn:
            repeat = RepeatMode.All
        else:
            # single without repeat only stops after the current song; the UI
            # has no separate mode for it, so it reads as Off.
            repeat = RepeatMode.Off
        changed = False
        if shuffleOn != self._shuffle:
            self._shuffle = shuffleOn
            changed = True
        if repeat is not self._repeat:
            self._repeat = repeat
            changed = True
        return changed

    def _tick(self) -> None:
        if self._stopping:
            return
        if not self._connected:
            if not self._reconnectReady:
                self._beginReconnect()
                if not self._lossEmitted:
                    self._lossEmitted = True
                    self._startHostPing()
                    self.connectionLost.emit()
                return
            # A background probe found the server; reconnect on this thread
            # (the greeting is a fast exchange once the port is reachable).
            self._reconnectReady = False
            try:
                self._client.connect()
                if self._password:
                    self._client.password(self._password)
            except (MpdError, OSError) as exc:
                _log.warning("reconnect failed: %s", exc)
                self._beginReconnect()
                return
        started = time.monotonic()
        try:
            status = self._client.command("status")
        except (MpdError, OSError) as exc:
            _log.warning(
                "status poll failed after %.0f ms: %s",
                (time.monotonic() - started) * 1000,
                exc,
            )
            if not _isCommandError(exc):
                self._markDisconnected()
            return
        elapsedMs = (time.monotonic() - started) * 1000
        reconnected = not self._connected
        self._connected = True
        self._latencyMs = elapsedMs
        self._ticks += 1
        if reconnected:
            self._stopHostPing()
            self._stopReconnect()
            self._lossEmitted = False
            self._retryAttempts = 0
            _log.info("MPD connection restored")
        if elapsedMs >= _SLOW_POLL_MS:
            _log.warning("slow status poll: %.0f ms", elapsedMs)
        elif self._ticks % _HEARTBEAT_TICKS == 0:
            _log.debug("status poll ok: %.0f ms (tick %d)", elapsedMs, self._ticks)

        fields = self._parseFields(status)
        optionsChanged = self._syncPlaybackOptions(fields)
        state = fields.get("state", "stop")
        self._state = state
        playing = state == "play"
        position = 0
        duration = 0
        try:
            position = int(float(fields.get("elapsed", fields.get("time", "0:0").split(":")[0])))
        except (ValueError, IndexError):
            position = 0
        try:
            duration = int(float(fields.get("duration", "0") or 0))
        except ValueError:
            duration = 0

        # MPD keeps a current song even after stop (and restores the queue
        # across a restart), so read it in every state.
        track = self._currentSong()
        if state == "stop":
            position = 0
        if duration <= 0 and track is not None:
            duration = track.duration

        trackChanged = track != self._track
        stateChanged = playing != self._playing

        self._playing = playing
        self._track = track
        self._position = position
        self._duration = duration
        self._queuePos = int(fields.get("song", "0") or 0) + 1 if "song" in fields else 0
        self._queueLen = int(fields.get("playlistlength", "0") or 0)

        if trackChanged:
            _log.info(
                "track changed: %s",
                f"{track.title} - {track.album}" if track else "none",
            )
            self.trackChanged.emit(track)
        if stateChanged:
            _log.info("play state changed: %s", state)
            self.playStateChanged.emit(playing)
        self.positionChanged.emit(position)
        if optionsChanged:
            self.playbackOptionsChanged.emit()
        if reconnected:
            # A browser opened while offline built an empty root; let the
            # visible page rebuild from the now reachable library.
            self.libraryChanged.emit()
            self.connectionRestored.emit()

    def _currentSong(self) -> Track | None:
        try:
            rows = self._client.command("currentsong")
        except (MpdError, OSError) as exc:
            _log.warning("currentsong failed: %s", exc)
            return None
        fields = self._parseFields(rows)
        if "file" not in fields:
            return None
        return self._trackFromFields(fields)

    @staticmethod
    def _parseFields(rows: list[str]) -> dict[str, str]:
        fields: dict[str, str] = {}
        for row in rows:
            if ":" in row:
                key, value = row.split(":", 1)
                fields[key.strip()] = value.strip()
        return fields


def _songAdded(fields: dict[str, str]) -> int:
    """Return when a song entered the database, or 0 when MPD cannot say.

    ``added`` is the standard MPD field since 0.24; older servers only send
    the file modification time as ``Last-Modified``, which is the best
    available fallback. MPD uses a negative value for "unknown".
    """
    for key in ("added", "Last-Modified"):
        value = _parseTimestamp(fields.get(key, ""))
        if value:
            return value
    return 0


def _parseTimestamp(value: str) -> int:
    """Parse an ISO-8601 MPD timestamp into a unix timestamp; 0 on failure."""
    text = value.strip()
    if not text or text.startswith("-"):
        # MPD reports unknown (`added`) as a negative timestamp.
        return 0
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


def _caller(depth: int = 4) -> str:
    """Name the frames that asked for a library load, for the debug log."""
    frames = traceback.extract_stack()[:-2]
    return " <- ".join(frame.name for frame in frames[-depth:])
