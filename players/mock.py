"""Mock player backend with sample data for UI development.

Simulates transport, position ticking and a small library so the
interface can be exercised without an MPD server.
"""

from __future__ import annotations

import random

from PySide6.QtCore import QTimer

from .base import STARRED_PLAYLIST, Album, PlayerBackend, RepeatMode, Track

# (artist, album, year, genre, [(title, duration), ...])
_LIBRARY = [
    (
        "Aurora Ensemble",
        "Northern Lights",
        2019,
        "Classical",
        [
            ("Prelude in Amber", 214),
            ("Adagio for Quiet Hours", 302),
            ("Nocturne in Blue", 256),
            ("Finale: Homeward", 288),
        ],
    ),
    (
        "Aurora Ensemble",
        "Winter Correspondence",
        2022,
        "Classical",
        [
            ("Letters in Frost", 241),
            ("Allegretto", 198),
            ("The Long Thaw", 315),
        ],
    ),
    (
        "The Meridian Quartet",
        "Blue Meridian",
        2017,
        "Jazz",
        [
            ("Meridian Line", 327),
            ("Ballad for the Eastern Shore", 402),
            ("Crosscurrent", 275),
            ("Harbour Lights", 356),
        ],
    ),
    (
        "The Meridian Quartet",
        "Second Watch",
        2021,
        "Jazz",
        [
            ("Night Passage", 344),
            ("Standing Orders", 291),
            ("Morning Relief", 310),
        ],
    ),
    (
        "Elena Voss",
        "Piano Works",
        2015,
        "Piano",
        [
            ("Study in E Minor", 187),
            ("Berceuse", 224),
            ("Toccata", 166),
            ("Reverie", 251),
        ],
    ),
    (
        "Northbound Orchestra",
        "Cartography",
        2020,
        "Orchestral",
        [
            ("Overland", 385),
            ("Contours", 412),
            ("The Northern Route", 455),
            ("Atlas", 398),
        ],
    ),
    (
        "Julian Marlowe",
        "Chamber Days",
        2018,
        "Chamber",
        [
            ("Divertimento", 276),
            ("Air and Variations", 331),
            ("Serenade", 244),
        ],
    ),
]


class MockPlayerBackend(PlayerBackend):
    """In-memory backend with a simulated clock and sample library."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._albums: list[Album] = []
        self._tracksByAlbum: dict[tuple[str, str], list[Track]] = {}
        self._allTracks: list[Track] = []
        self._buildLibrary()

        self._queue: list[Track] = []
        self._index = -1
        self._playing = False
        self._position = 0
        self._playlists: dict[str, list[Track]] = {STARRED_PLAYLIST: []}

        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._tick)

    # ------------------------------------------------------------------ #
    # Library construction
    # ------------------------------------------------------------------ #
    def _buildLibrary(self) -> None:
        # Timestamps increase per album so "date added" ordering is stable
        # and distinct from the other sort orders.
        base = 1_700_000_000
        for index, (artist, albumTitle, year, genre, titles) in enumerate(_LIBRARY):
            added = base + index * 86400
            album = Album(
                title=albumTitle, artist=artist, year=year, genre=genre, added=added,
                track_count=len(titles),
            )
            self._albums.append(album)
            tracks = [
                Track(
                    title=title,
                    artist=artist,
                    album=albumTitle,
                    duration=duration,
                    track_number=no,
                    genre=genre,
                    year=year,
                    added=added + no * 60,
                )
                for no, (title, duration) in enumerate(titles, start=1)
            ]
            self._tracksByAlbum[(artist, albumTitle)] = tracks
            self._allTracks.extend(tracks)

    def serverLabel(self) -> str:
        return "Mock library"

    def serverState(self) -> str:
        return "Connected"

    def latencyMs(self) -> float | None:
        return None

    def isPlaying(self) -> bool:
        return self._playing

    def currentTrack(self) -> Track | None:
        if 0 <= self._index < len(self._queue):
            return self._queue[self._index]
        return None

    def position(self) -> int:
        return self._position

    def duration(self) -> int:
        track = self.currentTrack()
        return track.duration if track else 0

    def play(self) -> None:
        if self.currentTrack() is None and self._queue:
            self._index = 0
            self._emitTrackChanged()
        if self.currentTrack() is None:
            return
        if self._playing:
            return
        self._playing = True
        self._clock.start()
        self.playStateChanged.emit(True)

    def pause(self) -> None:
        if not self._playing:
            return
        self._playing = False
        self._clock.stop()
        self.playStateChanged.emit(False)

    def stop(self) -> None:
        self.pause()
        self._position = 0
        self.positionChanged.emit(0)

    def next(self) -> None:
        if not self._queue:
            return
        if self.repeatMode() is RepeatMode.One:
            self._seekTo(0)
            return
        self._moveTo(self._index + 1)

    def previous(self) -> None:
        if not self._queue:
            return
        # Restart the current recording when more than three seconds in.
        if self._position > 3:
            self._seekTo(0)
            return
        self._moveTo(self._index - 1)

    def seek(self, seconds: int) -> None:
        if not self._queue:
            return
        self._seekTo(max(0, min(self.duration(), int(seconds))))

    # ------------------------------------------------------------------ #
    # Queue / playback entry points
    # ------------------------------------------------------------------ #
    def playTrack(self, track: Track) -> None:
        self._queue = list(self._allTracks)
        try:
            self._index = self._queue.index(track)
        except ValueError:
            self._queue = [track]
            self._index = 0
        self._seekTo(0)
        self._emitTrackChanged()
        self.play()

    def playAlbum(self, album: Album, startIndex: int = 0) -> None:
        tracks = self.tracksByAlbum(album)
        if not tracks:
            return
        self._queue = tracks
        self._index = max(0, min(len(tracks) - 1, startIndex))
        self._seekTo(0)
        self._emitTrackChanged()
        self.play()

    def addAlbumToQueue(self, album: Album) -> None:
        tracks = self.tracksByAlbum(album)
        if not tracks:
            return
        if not self._queue:
            self._queue = list(tracks)
            self._index = 0
            self._seekTo(0)
            self._emitTrackChanged()
            self.play()
            return
        self._queue.extend(tracks)

    def shufflePlayAll(self) -> None:
        tracks = list(self._allTracks)
        if not tracks:
            return
        random.shuffle(tracks)
        self._queue = tracks
        self._index = 0
        self._seekTo(0)
        self._emitTrackChanged()
        self.play()

    def queuePosition(self) -> int:
        if 0 <= self._index < len(self._queue):
            return self._index + 1
        return 0

    def queueLength(self) -> int:
        return len(self._queue)

    def queueTracks(self) -> list[Track]:
        return list(self._queue)

    def queuePlayNext(self, index: int) -> None:
        if not 0 <= index < len(self._queue) or self._index < 0 or index == self._index:
            return
        track = self._queue.pop(index)
        if index < self._index:
            self._index -= 1
        self._queue.insert(self._index + 1, track)
        self.libraryChanged.emit()

    def queueRemove(self, index: int) -> None:
        if not 0 <= index < len(self._queue):
            return
        self._queue.pop(index)
        if not self._queue:
            self._index = -1
            self._playing = False
            self.playStateChanged.emit(False)
        elif index < self._index:
            self._index -= 1
        elif index == self._index:
            self._index %= len(self._queue)
            self._seekTo(0)
            self._emitTrackChanged()
        self.libraryChanged.emit()

    def queueClear(self) -> None:
        if 0 <= self._index < len(self._queue):
            self._queue = [self._queue[self._index]]
            self._index = 0
        else:
            self._queue.clear()
            self._index = -1
        self.libraryChanged.emit()

    # ------------------------------------------------------------------ #
    # Playlists
    # ------------------------------------------------------------------ #
    def supportsPlaylists(self) -> bool:
        return True

    def playlists(self) -> list[str]:
        names = sorted(
            (name for name in self._playlists if name != STARRED_PLAYLIST),
            key=str.lower,
        )
        return [STARRED_PLAYLIST, *names]

    def playlistTracks(self, name: str) -> list[Track]:
        return list(self._playlists.get(name, []))

    def addTrackToPlaylist(self, name: str, track: Track) -> bool:
        if not name:
            return False
        if name not in self._playlists:
            self._playlists[name] = []
        self._playlists[name].append(track)
        self.playlistsChanged.emit()
        return True

    def removeTrackFromPlaylist(self, name: str, index: int) -> bool:
        tracks = self._playlists.get(name)
        if tracks is None or not 0 <= index < len(tracks):
            return False
        tracks.pop(index)
        self.playlistsChanged.emit()
        return True

    def playPlaylist(self, name: str, startIndex: int = 0) -> None:
        tracks = self._playlists.get(name, [])
        if not tracks:
            return
        self._queue = list(tracks)
        self._index = max(0, min(len(tracks) - 1, startIndex))
        self._seekTo(0)
        self._emitTrackChanged()
        self.play()

    def addPlaylistToQueue(self, name: str) -> None:
        tracks = self._playlists.get(name, [])
        if not tracks:
            return
        if not self._queue:
            self._queue = list(tracks)
            self._index = 0
            self._seekTo(0)
            self._emitTrackChanged()
            self.play()
            return
        self._queue.extend(tracks)

    def createPlaylist(self, name: str) -> bool:
        name = name.strip()
        if not name or name in self._playlists:
            return False
        self._playlists[name] = []
        self.playlistsChanged.emit()
        return True

    def deletePlaylist(self, name: str) -> bool:
        if name == STARRED_PLAYLIST or name not in self._playlists:
            return False
        del self._playlists[name]
        self.playlistsChanged.emit()
        return True

    def renamePlaylist(self, name: str, newName: str) -> bool:
        newName = newName.strip()
        if (
            name == STARRED_PLAYLIST
            or newName == STARRED_PLAYLIST
            or name not in self._playlists
            or not newName
            or newName in self._playlists
        ):
            return False
        self._playlists[newName] = self._playlists.pop(name)
        self.playlistsChanged.emit()
        return True

    # ------------------------------------------------------------------ #
    # Library queries
    # ------------------------------------------------------------------ #
    def artists(self) -> list[str]:
        return sorted({a.artist for a in self._albums})

    def albums(self) -> list[Album]:
        return sorted(self._albums, key=lambda a: (a.artist, a.title))

    def songs(self) -> list[Track]:
        return sorted(self._allTracks, key=lambda t: (t.title, t.artist))

    def genres(self) -> list[str]:
        return sorted({a.genre for a in self._albums})

    def albumsByArtist(self, artist: str) -> list[Album]:
        return [a for a in self.albums() if a.artist == artist]

    def albumsByGenre(self, genre: str) -> list[Album]:
        return [a for a in self.albums() if a.genre == genre]

    def tracksByAlbum(self, album: Album) -> list[Track]:
        return list(self._tracksByAlbum.get((album.artist, album.title), []))

    def tracksByArtist(self, artist: str) -> list[Track]:
        return [t for t in self.songs() if t.artist == artist]

    def tracksByGenre(self, genre: str) -> list[Track]:
        return [t for t in self.songs() if t.genre == genre]

    # ------------------------------------------------------------------ #
    # Simulation internals
    # ------------------------------------------------------------------ #
    def _tick(self) -> None:
        if not self._playing:
            return
        track = self.currentTrack()
        if track is None:
            self.pause()
            return
        self._position += 1
        if self._position >= track.duration:
            if self.repeatMode() is RepeatMode.One:
                self._seekTo(0)
                return
            if self._index + 1 >= len(self._queue):
                if self.repeatMode() is RepeatMode.All:
                    self._moveTo(0)
                else:
                    self.stop()
                return
            self._moveTo(self._index + 1)
            return
        self.positionChanged.emit(self._position)

    def _moveTo(self, index: int) -> None:
        if not self._queue:
            return
        if self.shuffle() and index not in (0, len(self._queue) - 1):
            index = random.randrange(len(self._queue))
        index %= len(self._queue)
        self._index = index
        self._seekTo(0)
        self._emitTrackChanged()

    def _seekTo(self, seconds: int) -> None:
        self._position = int(seconds)
        self.positionChanged.emit(self._position)

    def _emitTrackChanged(self) -> None:
        self.trackChanged.emit(self.currentTrack())
