"""Interface pages: Now Playing, library browsers, Settings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QPoint, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from players.base import (
    STARRED_PLAYLIST,
    Album,
    PlayerBackend,
    RepeatMode,
    Track,
    formatDuration,
)
from ui.state import AppState
from ui.theme import Palette, makeFont
from ui.widgets import CoverArt, FocusList, OptionMenu, ProgressBar, elide


def _formatBytes(count: int) -> str:
    """Format a byte count for compact display, e.g. ``12.4 MB``."""
    value = float(max(0, count))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


class _CacheSizeScan(QRunnable):
    """Measure the artwork cache without blocking the GUI thread.

    The result is written to a plain dict polled by the page, so no Qt object
    is touched from the worker thread.
    """

    def __init__(self, counter: Callable[[], int], result: dict[str, int]) -> None:
        super().__init__()
        self._counter = counter
        self._result = result

    def run(self) -> None:
        try:
            size = self._counter()
        except Exception:  # noqa: BLE001 - a failed scan must not crash the pool
            size = 0
        self._result["size"] = size



@dataclass
class Level:
    """One level of a drill-down browser."""

    header: str
    items: list[tuple[str, str]]
    handler: Callable[[int], None]
    coverProvider: Callable[[int], bytes | None] | None = None
    iconProvider: Callable[[int], str | None] | None = None
    # Maps a row index to a stable identity used to restore the cursor.
    keyProvider: Callable[[int], object] | None = None

    def keyFor(self, index: int) -> object:
        if self.keyProvider is None:
            return None
        return self.keyProvider(index)


class Page(QWidget):
    """Base class for all main-knob pages."""

    def __init__(
        self,
        backend: PlayerBackend,
        parent: QWidget | None = None,
        state: AppState | None = None,
    ) -> None:
        super().__init__(parent)
        self.backend = backend
        self.state = state
        self._menu: OptionMenu | None = None
        self._menuPick: Callable[[int], None] | None = None
        # Blurred-cover backdrop, already scaled to this page.
        self._backdrop: QPixmap | None = None

    # -- backdrop --------------------------------------------------------- #
    def setBackdrop(self, pixmap: QPixmap | None) -> None:
        """Set or clear the blurred background."""
        self._backdrop = pixmap
        self.update()

    def paintEvent(self, event) -> None:
        if self._backdrop is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(self.rect(), self._backdrop)

    def title(self) -> str:
        return ""

    def onActivated(self) -> None:
        """Called when the page becomes the active section."""

    def navigate(self, delta: int) -> None:
        """Main knob rotation."""

    def select(self) -> None:
        """Main knob press."""

    def back(self) -> bool:
        """Handle back navigation; False means already at the top level."""
        return False

    def onOption(self) -> None:
        """Handle the option key (backslash)."""

    def onSectionKey(self) -> None:
        """Handle the section key while this page is already active."""

    # -- option menu ------------------------------------------------------ #
    def openOptionMenu(
        self,
        title: str,
        options: list[str],
        current: int = 0,
        onPick: Callable[[int], None] | None = None,
        showCurrent: bool = True,
        sectionsBefore: dict[int, str] | None = None,
        selected: int | None = None,
    ) -> None:
        """Show a knob-driven overlay menu; the callback receives the index."""
        if self._menu is None:
            self._menu = OptionMenu(self)
        self._menu.setItems(
            title, options, current, showCurrent, sectionsBefore, selected
        )
        self._menuPick = onPick
        self._menu.move(self._menuPosition())
        self._menu.show()
        self._menu.raise_()

    def closeOptionMenu(self) -> None:
        if self._menu is not None:
            self._menu.hide()
        self._menuPick = None

    def menuOpen(self) -> bool:
        return self._menu is not None and self._menu.isVisible()

    def menuNavigate(self, delta: int) -> None:
        if self._menu is not None:
            self._menu.moveSelection(delta)

    def menuSelect(self) -> None:
        """Pick the highlighted option and close the menu."""
        if self._menu is None:
            return
        index = self._menu.selected()
        pick = self._menuPick
        self.closeOptionMenu()
        if pick is not None:
            pick(index)

    def menuCancel(self) -> None:
        self.closeOptionMenu()

    def openPlaylistPicker(self, tracks: list[Track], title: str = "Add to Playlist") -> None:
        """Offer existing playlists plus an automatically named new one."""
        if not tracks or not self.backend.supportsPlaylists():
            return
        names = self.backend.playlists()
        if not names:
            names = [STARRED_PLAYLIST]
        options = list(names)
        options.append("New Playlist")

        def pick(index: int) -> None:
            if index == len(names):
                name = self._nextPlaylistName(names)
                if not self.backend.createPlaylist(name):
                    return
            else:
                name = names[index]
            for track in tracks:
                self.backend.addTrackToPlaylist(name, track)

        self.openOptionMenu(title, options, 0, pick, showCurrent=False)

    def _nextPlaylistName(self, names: list[str]) -> str:
        used = {name.lower() for name in names}
        number = 1
        while f"playlist {number}" in used:
            number += 1
        return f"Playlist {number}"

    def starredLabel(self, track: Track) -> str:
        return "Unstar" if self.backend.isStarred(track) else "Star"

    def _menuPosition(self) -> QPoint:
        """Centre the overlay menu horizontally, near the top of the page."""
        menu = self._menu
        assert menu is not None
        x = max(0, (self.width() - menu.width()) // 2)
        y = max(0, (self.height() - menu.height()) // 3)
        return QPoint(x, y)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._menu is not None and self._menu.isVisible():
            self._menu.move(self._menuPosition())


# --------------------------------------------------------------------------- #
# Library browser
# --------------------------------------------------------------------------- #
class ListPage(Page):
    """Generic drill-down browser built on a stack of list levels.

    The knob position is remembered per level as a stable identity key
    rather than a row index, so the cursor survives a rebuild of the list
    (for example after the section is re-entered or the rows are re-sorted).
    The key of the root level is also written to the state file, so the
    cursor is still there after a restart.
    """

    playbackStarted = Signal()

    # Prefix of the state keys used by this page; empty means no state.
    statePrefix = ""

    def __init__(
        self,
        backend: PlayerBackend,
        pageTitle: str,
        parent: QWidget | None = None,
        state: AppState | None = None,
    ) -> None:
        super().__init__(backend, parent, state)
        self._pageTitle = pageTitle
        # Each level stores its header, row items and optional providers.
        self._stack: list[Level] = []
        # Remembered identity of the selected row, one entry per level.
        self._levelKeys: list[object] = []
        self._suppressKeyUpdate = False

        # Restore the cursor of the root level from a previous session.
        if state is not None and self.statePrefix:
            saved = state.getKey(f"{self.statePrefix}.cursor")
            if saved is not None:
                self._levelKeys.append(saved)

        self._header = QLabel(pageTitle)
        self._header.setFont(makeFont(14, bold=True))
        self._header.setStyleSheet(f"color: {Palette.TextDim.name()};")

        self._list = FocusList(rowHeight=48)
        self._list.activated.connect(lambda index: self._activate(index))
        self._list.selectionChanged.connect(self._onSelectionChanged)
        backend.libraryChanged.connect(self._onLibraryChanged)
        backend.coverChanged.connect(self._onCoversChanged)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 12, 20, 8)
        layout.setSpacing(8)
        layout.addWidget(self._header)
        layout.addWidget(self._list, 1)

    def _onLibraryChanged(self, *_args) -> None:
        """Refresh a visible browser root after its async library load completes."""
        if not self.isVisible():
            return
        if len(self._stack) == 1:
            self._buildRoot()

    def _onCoversChanged(self, *_args) -> None:
        if self.isVisible():
            self._list.invalidateCoverCache()

    # -- cursor memory ---------------------------------------------------- #
    def _onSelectionChanged(self, index: int) -> None:
        """Record the identity of the row the knob is now on."""
        if self._suppressKeyUpdate or not self._stack:
            return
        key = self._stack[-1].keyFor(index)
        if key is None:
            return
        level = len(self._stack) - 1
        while len(self._levelKeys) <= level:
            self._levelKeys.append(None)
        self._levelKeys[level] = key
        # The root level is the one that survives a restart.
        if level == 0 and self.state is not None and self.statePrefix:
            self.state.setKey(f"{self.statePrefix}.cursor", key)

    def _restoreLevelSelection(self, level: int) -> None:
        """Re-select the row matching the remembered key, if it still exists."""
        if level < 0 or level >= len(self._stack) or not self._list.items():
            return
        key = self._levelKeys[level] if level < len(self._levelKeys) else None
        if key is None:
            return
        for index in range(len(self._list.items())):
            if self._stack[-1].keyFor(index) == key:
                self._suppressKeyUpdate = True
                try:
                    self._list.setSelectedRow(index)
                finally:
                    self._suppressKeyUpdate = False
                return

    def _rememberLevelKey(self, level: int) -> None:
        """Snapshot the current row of a level before it goes off screen."""
        if level < 0 or level >= len(self._stack) or not self._list.items():
            return
        key = self._stack[level].keyFor(self._list.selected())
        if key is None:
            return
        while len(self._levelKeys) <= level:
            self._levelKeys.append(None)
        self._levelKeys[level] = key

    # -- level management ------------------------------------------------- #
    def pushLevel(
        self,
        header: str,
        items: list[tuple[str, str]],
        handler: Callable[[int], None],
        coverProvider: Callable[[int], bytes | None] | None = None,
        iconProvider: Callable[[int], str | None] | None = None,
        keyProvider: Callable[[int], object] | None = None,
    ) -> None:
        self._rememberLevelKey(len(self._stack) - 1)
        self._stack.append(
            Level(header, items, handler, coverProvider, iconProvider, keyProvider)
        )
        self._showTopLevel()

    def popLevel(self) -> bool:
        if len(self._stack) <= 1:
            return False
        self._stack.pop()
        self._showTopLevel()
        self._restoreLevelSelection(len(self._stack) - 1)
        return True

    def resetToRoot(
        self,
        header: str,
        items: list[tuple[str, str]],
        handler: Callable[[int], None],
        coverProvider: Callable[[int], bytes | None] | None = None,
        iconProvider: Callable[[int], str | None] | None = None,
        keyProvider: Callable[[int], object] | None = None,
    ) -> None:
        self._stack = [Level(header, items, handler, coverProvider, iconProvider, keyProvider)]
        self._showTopLevel()
        self._restoreLevelSelection(0)

    def _showTopLevel(self) -> None:
        level = self._stack[-1]
        self._header.setText(level.header)
        self._suppressKeyUpdate = True
        try:
            self._list.setItems(
                level.items,
                coverProvider=level.coverProvider,
                iconProvider=level.iconProvider,
            )
        finally:
            self._suppressKeyUpdate = False

    def _activate(self, index: int) -> None:
        self._stack[-1].handler(index)

    # -- shared level builders -------------------------------------------- #
    def _pushAlbumLevel(self, album: Album) -> None:
        """Push the level listing an album's tracks, preceded by actions."""
        tracks = self.backend.tracksByAlbum(album)
        multiDisc = len({t.disc_number for t in tracks}) > 1
        items = [("Add to Queue", ""), ("Play This Album", "")]
        for t in tracks:
            number = (
                f"{t.disc_number}-{t.track_number:02d}"
                if multiDisc
                else f"{t.track_number:02d}"
            )
            items.append(
                (
                    f"{number}   {t.title}",
                    f"{t.artist} \u00b7 {formatDuration(t.duration)}",
                )
            )

        albumCover = self.backend.albumThumbnail(album)

        def cover(_index: int) -> bytes | None:
            return albumCover

        def icon(index: int) -> str | None:
            if index == 0:
                return "plus"
            if index == 1:
                return "play"
            return None

        def activate(index: int) -> None:
            if index == 0:
                self.backend.addAlbumToQueue(album)
                self.popLevel()
                return
            if index == 1:
                self.backend.playAlbum(album, 0)
                self.playbackStarted.emit()
                return
            self.backend.playAlbum(album, index - 2)
            self.playbackStarted.emit()

        def key(index: int) -> object:
            if index < 2:
                return ("action", index)
            if 2 <= index < 2 + len(tracks):
                t = tracks[index - 2]
                return (
                    "track",
                    t.artist,
                    t.album,
                    t.title,
                    t.track_number,
                    t.disc_number,
                )
            return None

        self.pushLevel(album.title, items, activate, cover, icon, key)

    def _pushAlbumChoiceLevel(
        self,
        header: str,
        albums: list[Album],
        secondary: Callable[[Album], str] | None = None,
    ) -> None:
        """Push a level offering the albums of an artist or a genre.

        The first row queues every album; the remaining rows open an album.
        """
        items = [("Add to Queue", "")] + [
            (a.title, secondary(a) if secondary else str(a.year)) for a in albums
        ]

        def albumCover(index: int) -> bytes | None:
            if index == 0:
                return None
            return self.backend.albumThumbnail(albums[index - 1])

        def icon(index: int) -> str | None:
            return "plus" if index == 0 else None

        def activate(index: int) -> None:
            if index == 0:
                for album in albums:
                    self.backend.addAlbumToQueue(album)
                self.popLevel()
                return
            self._pushAlbumLevel(albums[index - 1])

        def key(index: int) -> object:
            if index == 0:
                return ("action", "add_all")
            if 0 < index <= len(albums):
                a = albums[index - 1]
                return ("album", a.artist, a.title)
            return None

        self.pushLevel(header, items, activate, albumCover, icon, key)

    # -- Page interface --------------------------------------------------- #
    def title(self) -> str:
        return self._pageTitle

    def onActivated(self) -> None:
        self._buildRoot()
        self._list.setFocus()

    def onSectionKey(self) -> None:
        """Re-pressing this section's key returns to the root list top."""
        self.resetToTop()

    def resetToTop(self) -> None:
        """Leave any drill-down level and select the first row."""
        if len(self._stack) > 1:
            self._stack = self._stack[:1]
            self._showTopLevel()
        self._list.setSelectedRow(0)
        self._list.setFocus()

    def _buildRoot(self) -> None:
        """Populate the root level; subclasses override."""

    def navigate(self, delta: int) -> None:
        self._list.moveSelection(delta)

    def select(self) -> None:
        self._list.activateCurrent()

    def back(self) -> bool:
        return self.popLevel()


class AlbumsPage(ListPage):
    """Browse albums, then the recordings on each album."""

    statePrefix = "albums"

    SORT_MODES = ("added", "name", "year", "artist")
    SORT_LABELS = {
        "name": "Name",
        "year": "Year",
        "artist": "Artist",
        "added": "Recently Added",
    }

    def __init__(
        self,
        backend: PlayerBackend,
        parent: QWidget | None = None,
        state: AppState | None = None,
    ) -> None:
        super().__init__(backend, "Albums", parent, state)
        self._albumsCache: list[Album] = []
        self._openAlbumRef: Album | None = None
        self._sortMode = self._restoreSortMode(state)

    def _restoreSortMode(self, state: AppState | None) -> str:
        """Read the stored sort order; unknown values fall back to Name."""
        mode = state.getString("albums.sort", "name") if state is not None else "name"
        return mode if mode in self.SORT_MODES else "name"

    def _sortedAlbums(self) -> list[Album]:
        """Fetch the albums and apply the current sort order."""
        albums = list(self.backend.albums())
        if self._sortMode == "year":
            albums.sort(key=lambda a: (a.year, a.title.lower()))
        elif self._sortMode == "artist":
            albums.sort(key=lambda a: (a.artist.lower(), a.title.lower()))
        elif self._sortMode == "added":
            # Newest additions first.
            albums.sort(key=lambda a: (-a.added, a.title.lower()))
        else:
            albums.sort(key=lambda a: a.title.lower())
        return albums

    def _buildRoot(self) -> None:
        self._albumsCache = self._sortedAlbums()
        albums = self._albumsCache
        items = [("Shuffle All", "")] + [
            (a.title, f"{a.artist} \u00b7 {a.track_count} tracks") for a in albums
        ]

        def cover(index: int) -> bytes | None:
            # Row 0 is the Shuffle All action and carries no album art.
            if index == 0:
                return None
            return self.backend.albumThumbnail(albums[index - 1])

        def icon(index: int) -> str | None:
            return "shuffle" if index == 0 else None

        def activate(index: int) -> None:
            if index == 0:
                self.backend.shufflePlayAll()
                self.playbackStarted.emit()
                return
            self._openAlbum(albums[index - 1])

        def key(index: int) -> object:
            if index == 0:
                return ("action", "shuffle_all")
            if 0 < index <= len(albums):
                a = albums[index - 1]
                return ("album", a.artist, a.title)
            return None

        self._header.setText(f"{self._pageTitle} ({len(albums)})")
        self.resetToRoot(f"{self._pageTitle} ({len(albums)})", items, activate, cover, icon, key)

    # -- option menu ------------------------------------------------------ #
    def onOption(self) -> None:
        if self.menuOpen():
            self.menuCancel()
            return
        actions: list[tuple[str, Callable[[], None]]] = []
        root = len(self._stack) == 1
        if self.backend.supportsPlaylists():
            if root:
                album = self._selectedAlbum()
                if album is not None:
                    tracks = self.backend.tracksByAlbum(album)
                    actions.append((
                        "Add Album to Playlist",
                        lambda: self.openPlaylistPicker(tracks, "Add Album to Playlist"),
                    ))
            elif self._openAlbumRef is not None:
                album = self._openAlbumRef
                tracks = self.backend.tracksByAlbum(album)
                row = self._list.selected() - 2
                if 0 <= row < len(tracks):
                    track = tracks[row]
                    actions.append((
                        self.starredLabel(track),
                        lambda: self.backend.toggleStarred(track),
                    ))
                    actions.append((
                        "Add to Playlist",
                        lambda: self.openPlaylistPicker([track]),
                    ))
                else:
                    actions.append((
                        "Add Album to Playlist",
                        lambda: self.openPlaylistPicker(tracks, "Add Album to Playlist"),
                    ))
        if root:
            labels = [label for label, _ in actions]
            sortLabels = [self.SORT_LABELS[mode] for mode in self.SORT_MODES]
            sortIndex = self.SORT_MODES.index(self._sortMode)
            sections = {len(labels): "Sort"} if labels else None

            def pick(index: int) -> None:
                if index < len(labels):
                    actions[index][1]()
                else:
                    self._applySort(index - len(labels))

            self.openOptionMenu(
                "Albums",
                labels + sortLabels,
                len(labels) + sortIndex,
                pick,
                sectionsBefore=sections,
                selected=0 if labels else None,
            )
        elif actions:
            labels = [label for label, _ in actions]

            def pickAction(index: int) -> None:
                actions[index][1]()

            self.openOptionMenu("Album", labels, 0, pickAction, showCurrent=False)

    def _selectedAlbum(self) -> Album | None:
        if len(self._stack) != 1:
            return self._openAlbumRef
        index = self._list.selected() - 1
        if 0 <= index < len(self._albumsCache):
            return self._albumsCache[index]
        return None

    def _applySort(self, index: int) -> None:
        self._sortMode = self.SORT_MODES[index]
        if self.state is not None:
            self.state.set("albums.sort", self._sortMode)
        # Rebuild the list; the cursor follows the album it was on.
        self._buildRoot()

    def _openAlbum(self, album: Album) -> None:
        self._openAlbumRef = album
        self._pushAlbumLevel(album)

    def openAlbumNamed(self, artist: str, title: str) -> bool:
        """Open an album by identity; used by the Now Playing jump."""
        for index, album in enumerate(self._albumsCache, start=1):
            if album.title == title and (not artist or album.artist == artist):
                # Select the album row before pushing its track level so the
                # normal level stack remembers this row when the user backs out.
                self._list.setSelectedRow(index)
                self._openAlbum(album)
                return True
        return False


class SongsPage(ListPage):
    """Flat list of all recordings in the library."""

    statePrefix = "songs"

    SORT_MODES = ("name", "artist", "added")
    SORT_LABELS = {
        "name": "Name",
        "artist": "Artist",
        "added": "Recently Added",
    }

    def __init__(
        self,
        backend: PlayerBackend,
        parent: QWidget | None = None,
        state: AppState | None = None,
    ) -> None:
        super().__init__(backend, "Songs", parent, state)
        self._songsCache: list[Track] = []
        self._sortMode = self._restoreSortMode(state)

    def _restoreSortMode(self, state: AppState | None) -> str:
        """Read the stored sort order; unknown values fall back to Name."""
        mode = state.getString("songs.sort", "name") if state is not None else "name"
        return mode if mode in self.SORT_MODES else "name"

    def _sortedSongs(self) -> list[Track]:
        songs = list(self.backend.songs())
        if self._sortMode == "artist":
            songs.sort(key=lambda t: (t.artist.lower(), t.title.lower()))
        elif self._sortMode == "added":
            songs.sort(key=lambda t: (-t.added, t.title.lower()))
        else:
            songs.sort(key=lambda t: t.title.lower())
        return songs

    def _buildRoot(self) -> None:
        self._songsCache = self._sortedSongs()
        songs = self._songsCache
        items = [("Shuffle All", "")] + [
            (t.title, f"{t.artist} \u00b7 {formatDuration(t.duration)}") for t in songs
        ]

        def play(index: int) -> None:
            if index == 0:
                self.backend.shufflePlayAll()
                self.playbackStarted.emit()
                return
            self.backend.playTrack(songs[index - 1])
            self.playbackStarted.emit()

        def cover(index: int) -> bytes | None:
            if index == 0:
                return None
            return self.backend.trackThumbnail(songs[index - 1])

        def icon(index: int) -> str | None:
            return "shuffle" if index == 0 else None

        def key(index: int) -> object:
            if index == 0:
                return ("action", "shuffle_all")
            if 0 < index <= len(songs):
                t = songs[index - 1]
                return ("track", t.artist, t.album, t.title, t.track_number)
            return None

        self._header.setText(f"{self._pageTitle} ({len(songs)})")
        self.resetToRoot(f"{self._pageTitle} ({len(songs)})", items, play, cover, icon, key)

    # -- option menu ------------------------------------------------------ #
    def onOption(self) -> None:
        if self.menuOpen():
            self.menuCancel()
            return
        actions: list[tuple[str, Callable[[], None]]] = []
        index = self._list.selected() - 1
        if (
            self.backend.supportsPlaylists()
            and 0 <= index < len(self._songsCache)
        ):
            track = self._songsCache[index]
            actions.append((
                self.starredLabel(track),
                lambda: self.backend.toggleStarred(track),
            ))
            actions.append((
                "Add to Playlist",
                lambda: self.openPlaylistPicker([track]),
            ))
        labels = [label for label, _ in actions]
        sortLabels = [self.SORT_LABELS[mode] for mode in self.SORT_MODES]
        sortIndex = self.SORT_MODES.index(self._sortMode)
        sections = {len(labels): "Sort"} if labels else None

        def pick(choice: int) -> None:
            if choice < len(labels):
                actions[choice][1]()
            else:
                self._applySort(choice - len(labels))

        self.openOptionMenu(
            "Songs",
            labels + sortLabels,
            len(labels) + sortIndex,
            pick,
            sectionsBefore=sections,
            selected=0 if labels else None,
        )

    def _applySort(self, index: int) -> None:
        self._sortMode = self.SORT_MODES[index]
        if self.state is not None:
            self.state.set("songs.sort", self._sortMode)
        self._buildRoot()


class ArtistsPage(ListPage):
    """Browse artists, then albums, then recordings."""

    statePrefix = "artists"

    def __init__(
        self,
        backend: PlayerBackend,
        parent: QWidget | None = None,
        state: AppState | None = None,
    ) -> None:
        super().__init__(backend, "Artists", parent, state)
        self._artistsCache: list[str] = []

    def _buildRoot(self) -> None:
        self._artistsCache = self.backend.artists()
        artists = self._artistsCache
        items = [(name, f"{len(self.backend.albumsByArtist(name))} albums") for name in artists]

        def openArtist(index: int) -> None:
            artist = artists[index]
            albums = self.backend.albumsByArtist(artist)
            self._pushAlbumChoiceLevel(artist, albums)

        def key(index: int) -> object:
            if 0 <= index < len(artists):
                return ("artist", artists[index])
            return None

        self.resetToRoot(self._pageTitle, items, openArtist, None, None, key)

    def openArtistNamed(self, name: str) -> bool:
        """Open an artist by name; used by the Now Playing jump."""
        for index, artist in enumerate(self._artistsCache):
            if artist == name:
                # Preserve the jump target as the artist root cursor when the
                # user backs out of the artist's album list.
                self._list.setSelectedRow(index)
                self._stack[0].handler(index)
                return True
        return False


class GenresPage(ListPage):
    """Browse genres, then albums, then recordings."""

    statePrefix = "genres"

    def __init__(
        self,
        backend: PlayerBackend,
        parent: QWidget | None = None,
        state: AppState | None = None,
    ) -> None:
        super().__init__(backend, "Genres", parent, state)
        self._genresCache: list[str] = []

    def _buildRoot(self) -> None:
        self._genresCache = self.backend.genres()
        genres = self._genresCache
        items = [(name, f"{len(self.backend.albumsByGenre(name))} albums") for name in genres]

        def openGenre(index: int) -> None:
            genre = genres[index]
            albums = self.backend.albumsByGenre(genre)
            self._pushAlbumChoiceLevel(genre, albums, secondary=lambda a: a.artist)

        def key(index: int) -> object:
            if 0 <= index < len(genres):
                return ("genre", genres[index])
            return None

        self.resetToRoot(self._pageTitle, items, openGenre, None, None, key)


# --------------------------------------------------------------------------- #
# Now Playing
# --------------------------------------------------------------------------- #
class NowPlayingPage(Page):
    """Displays the current recording and basic transport state."""

    # Emitted with ("album" | "artist", value) to jump to the current item.
    jumpRequested = Signal(str, str)

    def __init__(
        self,
        backend: PlayerBackend,
        parent: QWidget | None = None,
        state: AppState | None = None,
    ) -> None:
        super().__init__(backend, parent, state)
        self._coverTrack: Track | None = None
        self._coverAttempts = 0
        self._coverRetryTimer = QTimer(self)
        self._coverRetryTimer.setInterval(1500)
        self._coverRetryTimer.timeout.connect(self._retryCover)

        self._titleLabel = QLabel("")
        self._titleLabel.setFont(makeFont(28, bold=True))
        self._titleLabel.setStyleSheet(f"color: {Palette.Text.name()};")
        self._titleLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._metaLabel = QLabel("")
        self._metaLabel.setFont(makeFont(20))
        self._metaLabel.setStyleSheet(f"color: {Palette.TextDim.name()};")
        self._metaLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        titleRow = QWidget()
        titleLayout = QHBoxLayout(titleRow)
        titleLayout.setContentsMargins(0, 0, 0, 0)
        titleLayout.setSpacing(8)
        titleLayout.addStretch(1)
        titleLayout.addWidget(self._titleLabel)
        titleLayout.addStretch(1)

        self._progress = ProgressBar()
        self._progress.setFixedWidth(240)

        self._cover = CoverArt(size=240)

        # Cover centred on top, then track name, then artist - album, then progress bar.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 18, 40, 16)
        layout.setSpacing(10)
        layout.addStretch(1)
        layout.addWidget(self._cover, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(6)
        layout.addWidget(titleRow)
        layout.addWidget(self._metaLabel)
        layout.addSpacing(12)
        layout.addWidget(self._progress, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)

        backend.trackChanged.connect(self._refresh)
        backend.playStateChanged.connect(lambda _: self._refresh())
        backend.positionChanged.connect(self._refreshPosition)
        backend.positionTick.connect(self._refreshPosition)
        backend.coverChanged.connect(self._onCoverChanged)
        self._refresh()

    # -- Page interface --------------------------------------------------- #
    def title(self) -> str:
        return "Now Playing"

    def onActivated(self) -> None:
        self._refresh()
        # Artwork may have arrived while the page was hidden, and retries stop
        # when the page is not visible; pick both up when coming back.
        track = self.backend.currentTrack()
        if track is None or self._cover.hasImage():
            return
        data = self.backend.trackCover(track)
        if data:
            self._cover.setImage(data)
        elif track == self._coverTrack:
            self._coverAttempts = 0
            self._scheduleCoverRetry(track)

    def reloadCover(self) -> None:
        """Force a fresh cover lookup, e.g. after the connection returns."""
        self._coverAttempts = 0
        self._coverRetryTimer.stop()
        self._coverTrack = None
        self._refresh()

    # Transport is handled globally by the window; the knobs do nothing here.
    def navigate(self, delta: int) -> None:
        return

    def select(self) -> None:
        return

    def onOption(self) -> None:
        track = self.backend.currentTrack()
        if track is None:
            return
        if self.menuOpen():
            self.menuCancel()
            return
        actions: list[tuple[str, Callable[[], None]]] = []
        if self.backend.supportsPlaylists():
            actions.append((
                self.starredLabel(track),
                lambda: self.backend.toggleStarred(track),
            ))
            actions.append((
                "Add to Playlist",
                lambda: self.openPlaylistPicker([track]),
            ))
        labels = [label for label, _ in actions]
        options = labels + ["Album", "Artist"]

        def pick(index: int) -> None:
            if index < len(labels):
                actions[index][1]()
            else:
                self._jump(index - len(labels))

        self.openOptionMenu(
            "Now Playing",
            options,
            0,
            pick,
            showCurrent=False,
            sectionsBefore={len(labels): "Go To"} if labels else None,
        )

    def _jump(self, index: int) -> None:
        track = self.backend.currentTrack()
        if track is None:
            return
        album = self.backend.albumForTrack(track)
        if index == 0:
            artist = album.artist if album is not None else track.artist
            self.jumpRequested.emit("album", f"{artist}\x1f{track.album}")
        else:
            self.jumpRequested.emit("artist", album.artist if album is not None else track.artist)

    # -- refresh ---------------------------------------------------------- #
    def _refresh(self, *_args) -> None:
        track = self.backend.currentTrack()
        if track is None:
            self._titleLabel.setText("")
            self._metaLabel.setText("")
            self._progress.setValue(0.0)
            self._coverTrack = None
            self._cover.setImage(None)
            self._coverAttempts = 0
            self._coverRetryTimer.stop()
        else:
            width = self.width() - 120
            self._titleLabel.setText(elide(track.title, makeFont(28, bold=True), width))
            self._metaLabel.setText(
                elide(f"{track.artist} - {track.album}", makeFont(20), width)
            )
            self._refreshPosition(self.backend.positionNow())
            if track != self._coverTrack:
                self._coverTrack = track
                self._coverAttempts = 0
                self._coverRetryTimer.stop()
                # Drop the previous album's artwork immediately; otherwise a
                # pending or missing cover leaves the old cover on screen.
                self._cover.setImage(None)
            # Always retry missing artwork here: a reconnect re-emits
            # trackChanged even when the recording itself did not change.
            if not self._cover.hasImage():
                data = self.backend.trackCover(track)
                if data:
                    self._cover.setImage(data)
                else:
                    self._scheduleCoverRetry(track)

    # -- cover retry ------------------------------------------------------ #
    def _scheduleCoverRetry(self, track: Track) -> None:
        """Keep asking for artwork unless the server says there is none."""
        if self._cover.hasImage():
            return
        status = self.backend.coverStatus(track)
        if status in ("ready", "missing"):
            return
        if not self._coverRetryTimer.isActive():
            self._coverRetryTimer.start()

    def _retryCover(self) -> None:
        track = self.backend.currentTrack()
        if (
            track is None
            or track != self._coverTrack
            or not self.isVisible()
        ):
            self._coverRetryTimer.stop()
            return
        self._coverAttempts += 1
        data = self.backend.trackCover(track)
        if data:
            self._cover.setImage(data)
            self._coverRetryTimer.stop()
            return
        if (
            self.backend.coverStatus(track) in ("ready", "missing")
            or self._coverAttempts >= 5
        ):
            self._coverRetryTimer.stop()

    def _onCoverChanged(self) -> None:
        track = self.backend.currentTrack()
        if track is None or track != self._coverTrack or not self.isVisible():
            return
        data = self.backend.trackCover(track)
        if data:
            self._cover.setImage(data)
            self._coverAttempts = 0
            self._coverRetryTimer.stop()
        elif self.backend.coverStatus(track) == "missing":
            self._coverRetryTimer.stop()

    def _refreshPosition(self, position: float) -> None:
        duration = self.backend.duration()
        self._progress.setValue(position / duration if duration else 0.0)


# --------------------------------------------------------------------------- #
# Queue
# --------------------------------------------------------------------------- #
class QueuePage(Page):
    """Shows editable queue entries, excluding the current recording."""

    jumpRequested = Signal(str, str)

    def __init__(
        self,
        backend: PlayerBackend,
        parent: QWidget | None = None,
        state: AppState | None = None,
    ) -> None:
        super().__init__(backend, parent, state)
        # Visible row under the knob, saved across queue refreshes.
        self._cursor: int | None = None
        self._suppressQueueSelection = False

        # Restore the row the knob was on in a previous session.
        if state is not None:
            saved = state.getInt("queue.cursor", -1)
            if saved >= 0:
                self._cursor = saved

        self._listHeader = QLabel("Up Next")
        self._listHeader.setFont(makeFont(14, bold=True))
        self._listHeader.setStyleSheet(f"color: {Palette.TextDim.name()};")

        self._list = FocusList(rowHeight=48)
        self._list.selectionChanged.connect(self._onSelectionChanged)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 8)
        layout.setSpacing(8)
        layout.addWidget(self._listHeader)
        layout.addWidget(self._list, 1)

        backend.trackChanged.connect(self._refresh)
        backend.playStateChanged.connect(lambda _: self._refresh())
        backend.libraryChanged.connect(self._refresh)
        backend.playbackOptionsChanged.connect(self._onPlaybackOptionsChanged)
        self._refresh()

    # -- Page interface --------------------------------------------------- #
    def title(self) -> str:
        return "Queue"

    def onActivated(self) -> None:
        self._refresh()

    def _onPlaybackOptionsChanged(self) -> None:
        """Shuffle/repeat changes can move the current item; redraw the queue."""
        if self.isVisible():
            self._refresh()

    def navigate(self, delta: int) -> None:
        self._cursor = None
        self._list.moveSelection(delta)
        self._cursor = self._list.selected()
        self._saveCursor()

    def select(self) -> None:
        index = self._list.selected()
        if not 0 <= index < len(self._queueIndices):
            return
        self.backend.queuePlayNext(self._queueIndices[index])
        self._cursor = index
        self._refresh()

    def selectLong(self) -> None:
        index = self._list.selected()
        if not 0 <= index < len(self._queueIndices):
            return
        self.backend.queueRemove(self._queueIndices[index])
        queueLength = len(self._queueIndices) - 1
        self._cursor = min(index, queueLength - 1) if queueLength else None
        self._refresh()

    def onOption(self) -> None:
        if self.menuOpen():
            self.menuCancel()
            return
        actions: list[tuple[str, Callable[[], None]]] = []
        track = self._selectedTrack()
        if track is not None and self.backend.supportsPlaylists():
            actions.append((
                self.starredLabel(track),
                lambda: self.backend.toggleStarred(track),
            ))
            actions.append((
                "Add to Playlist",
                lambda: self.openPlaylistPicker([track]),
            ))
        labels = [label for label, _ in actions]
        options = ["Clear Queue"] + labels + ["Album", "Artist"]
        sections = {}
        if labels:
            sections[1] = "Selected Track"
        sections[1 + len(labels)] = "Go To"

        def pick(index: int) -> None:
            if index == 0:
                self._cursor = None
                self.backend.queueClear()
            elif index <= len(labels):
                actions[index - 1][1]()
            else:
                self._applyQueueOption(index - 1 - len(labels))

        self.openOptionMenu(
            "Queue",
            options,
            0,
            pick,
            showCurrent=False,
            sectionsBefore=sections,
        )

    def _selectedTrack(self) -> Track | None:
        selected = self._list.selected()
        if not 0 <= selected < len(self._queueIndices):
            return None
        queue = self.backend.queueTracks()
        queueIndex = self._queueIndices[selected]
        if not 0 <= queueIndex < len(queue):
            return None
        return queue[queueIndex]

    def _applyQueueOption(self, index: int) -> None:
        track = self._selectedTrack()
        if track is None:
            return
        album = self.backend.albumForTrack(track)
        if index == 0:
            artist = album.artist if album is not None else track.artist
            self.jumpRequested.emit("album", f"{artist}\x1f{track.album}")
        elif index == 1:
            self.jumpRequested.emit("artist", album.artist if album is not None else track.artist)

    def _onSelectionChanged(self, index: int) -> None:
        if not self._suppressQueueSelection:
            self._cursor = index
            self._saveCursor()

    def _saveCursor(self) -> None:
        """Remember the row the knob is on across a restart."""
        if self.state is None or self._cursor is None:
            return
        self.state.set("queue.cursor", self._cursor)

    # -- refresh ---------------------------------------------------------- #
    def _refresh(self, *_args) -> None:
        queue = self.backend.queueTracks()
        currentIndex = self.backend.queueIndex()
        self._queueIndices = [
            i for i in range(len(queue)) if i != currentIndex
        ]
        upcoming = [queue[i] for i in self._queueIndices]
        rows = [
            (
                f"{i + 1:02d}   {t.title}",
                f"{t.artist} \u00b7 {formatDuration(t.duration)}",
            )
            for i, t in enumerate(upcoming)
        ]

        def cover(index: int) -> bytes | None:
            if 0 <= index < len(upcoming):
                return self.backend.trackThumbnail(upcoming[index])
            return None

        self._suppressQueueSelection = True
        try:
            self._list.setItems(rows, coverProvider=cover)
            # Keep the knob where the user left it, otherwise follow the
            # recording that is currently playing.
            target = 0
            if self._cursor is not None and 0 <= self._cursor < len(rows):
                target = self._cursor
            if target is not None and 0 <= target < len(rows):
                self._list.setSelectedRow(target)
        finally:
            self._suppressQueueSelection = False
        self._listHeader.setText(
            f"Up Next ({len(upcoming)} in queue)" if upcoming else "Up Next"
        )


# --------------------------------------------------------------------------- #
# Playlists
# --------------------------------------------------------------------------- #
class PlaylistsPage(ListPage):
    """Browse stored playlists and the recordings they contain."""

    statePrefix = "playlists"

    def __init__(
        self,
        backend: PlayerBackend,
        parent: QWidget | None = None,
        state: AppState | None = None,
    ) -> None:
        super().__init__(backend, "Playlists", parent, state)
        self._playlistNames: list[str] = []
        self._openPlaylistName: str | None = None
        backend.playlistsChanged.connect(self._onPlaylistsChanged)

    def _buildRoot(self) -> None:
        self._openPlaylistName = None
        if not self.backend.supportsPlaylists():
            self._playlistNames = []
            self.resetToRoot(self._pageTitle, [], lambda _index: None)
            self._list.setPlaceholder("Playlists are unavailable for this backend.")
            return
        self._playlistNames = self.backend.playlists()
        names = self._playlistNames
        items = [
            (name, "Stared" if name == STARRED_PLAYLIST else "")
            for name in names
        ]

        def icon(index: int) -> str | None:
            if 0 <= index < len(names):
                return "star-fill" if names[index] == STARRED_PLAYLIST else "music-list"
            return None

        def openPlaylist(index: int) -> None:
            self._openPlaylist(names[index])

        def key(index: int) -> object:
            if 0 <= index < len(names):
                return ("playlist", names[index])
            return None

        self.resetToRoot(self._pageTitle, items, openPlaylist, None, icon, key)
        self._list.setPlaceholder("No playlists yet. Use Add to Playlist.")

    def _openPlaylist(self, name: str) -> None:
        self._openPlaylistName = name
        tracks = self.backend.playlistTracks(name)
        # Two action rows sit above the recordings, mirroring the album view.
        items: list[tuple[str, str]] = [
            ("Add to Queue", f"{len(tracks)} tracks"),
            ("Play This Playlist", formatDuration(sum(t.duration for t in tracks))),
        ]
        items.extend(
            (
                f"{track.track_number:02d}   {track.title}"
                if track.track_number
                else track.title,
                f"{track.artist} \u00b7 {formatDuration(track.duration)}",
            )
            for track in tracks
        )

        def play(index: int) -> None:
            if index == 0:
                self.backend.addPlaylistToQueue(name)
                return
            if index == 1:
                self.backend.playPlaylist(name, 0)
                self.playbackStarted.emit()
                return
            self.backend.playPlaylist(name, index - 2)
            self.playbackStarted.emit()

        def cover(index: int) -> bytes | None:
            trackIndex = index - 2
            if 0 <= trackIndex < len(tracks):
                return self.backend.trackThumbnail(tracks[trackIndex])
            return None

        def icon(index: int) -> str | None:
            if index == 0:
                return "plus"
            if index == 1:
                return "play"
            return None

        def key(index: int) -> object:
            if index == 0:
                return ("action", "playlist_add_queue", name)
            if index == 1:
                return ("action", "playlist_play", name)
            trackIndex = index - 2
            if 0 <= trackIndex < len(tracks):
                track = tracks[trackIndex]
                return (
                    "playlist-track",
                    name,
                    track.artist,
                    track.album,
                    track.title,
                    track.track_number,
                    trackIndex,
                )
            return None

        self.pushLevel(name, items, play, cover, icon, key)
        self._list.setPlaceholder("This playlist is empty.")

    def onSectionKey(self) -> None:
        """Re-pressing the Playlists key returns to the playlist list top."""
        self._openPlaylistName = None
        self.resetToTop()

    def _onPlaylistsChanged(self) -> None:
        if not self.isVisible():
            return
        if len(self._stack) == 1:
            self._buildRoot()
            return
        if self._openPlaylistName is not None:
            name = self._openPlaylistName
            self._stack.pop()
            self._openPlaylist(name)

    def selectLong(self) -> None:
        """Remove the selected recording from the open playlist."""
        if len(self._stack) <= 1 or self._openPlaylistName is None:
            return
        index = self._list.selected() - 2
        if index < 0:
            return
        self.backend.removeTrackFromPlaylist(self._openPlaylistName, index)

    def onOption(self) -> None:
        if self.menuOpen():
            self.menuCancel()
            return
        if not self.backend.supportsPlaylists():
            return
        if len(self._stack) == 1:
            index = self._list.selected()
            name = self._playlistNames[index] if 0 <= index < len(self._playlistNames) else ""
            options = ["New Playlist"]
            if name and name != STARRED_PLAYLIST:
                options.append("Delete Playlist")
            self.openOptionMenu(
                "Playlists",
                options,
                0,
                lambda choice: self._applyRootOption(choice, name),
                showCurrent=False,
            )
        elif self._openPlaylistName is not None:
            self.openOptionMenu(
                self._openPlaylistName,
                ["Remove from Playlist"],
                0,
                lambda _choice: self.selectLong(),
                showCurrent=False,
            )

    def _applyRootOption(self, choice: int, name: str) -> None:
        if choice == 0:
            names = self.backend.playlists()
            candidate = self._nextPlaylistName(names)
            self.backend.createPlaylist(candidate)
        elif choice == 1 and name:
            self.backend.deletePlaylist(name)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
class SettingsPage(Page):
    """Application settings plus live MPD server information."""

    SERVER_ROW = 5
    CACHE_ROW = 3
    REFRESH_MS = 300
    CACHE_REFRESH_MS = 2000

    def __init__(
        self,
        backend: PlayerBackend,
        parent: QWidget | None = None,
        state: AppState | None = None,
    ) -> None:
        super().__init__(backend, parent, state)

        self._header = QLabel("Settings")
        self._header.setFont(makeFont(14, bold=True))
        self._header.setStyleSheet(f"color: {Palette.TextDim.name()};")

        self._list = FocusList()
        self._list.activated.connect(self._cycle)
        backend.playbackOptionsChanged.connect(
            lambda: self._refreshItems(keepSelection=True)
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 12, 20, 8)
        layout.setSpacing(8)
        layout.addWidget(self._header)
        layout.addWidget(self._list, 1)

        # The server row shows a live address/state and ping while visible.
        self._serverTimer = QTimer(self)
        self._serverTimer.setInterval(self.REFRESH_MS)
        self._serverTimer.timeout.connect(self._refreshServerRow)
        self._serverTimer.start()

        # The cache row reports the on-disk size while caching progresses. The
        # measurement runs off the GUI thread and is polled here, so a large
        # cache never stalls the interface.
        self._cacheSizeLabel = "..."
        self._cacheScans: list[tuple[_CacheSizeScan, dict[str, int], int]] = []
        self._cacheToken = 0
        self._cacheTimer = QTimer(self)
        self._cacheTimer.setInterval(self.CACHE_REFRESH_MS)
        self._cacheTimer.timeout.connect(self._requestCacheSize)
        self._cacheTimer.start()
        self._cachePoll = QTimer(self)
        self._cachePoll.setInterval(200)
        self._cachePoll.timeout.connect(self._collectCacheSize)
        self._cachePoll.start()

        self._refreshItems()

    # -- Page interface --------------------------------------------------- #
    def title(self) -> str:
        return "Settings"

    def onActivated(self) -> None:
        self._refreshItems(keepSelection=True)
        self._requestCacheSize()
        self._refreshServerRow()
        self._list.setFocus()

    def onSectionKey(self) -> None:
        """Re-pressing the Settings key returns to the first row."""
        self._list.setSelectedRow(0)
        self._list.setFocus()

    def navigate(self, delta: int) -> None:
        self._list.moveSelection(delta)

    def select(self) -> None:
        self._list.activateCurrent()

    # -- MPD server information ------------------------------------------- #
    def _serverText(self) -> str:
        """Two lines: address and state, then the latest ping."""
        label = self.backend.serverLabel()
        state = self.backend.serverState()
        latency = self.backend.latencyMs()
        if latency is not None:
            ping = "<1 ms" if latency < 1 else f"{latency:.0f} ms"
        else:
            pingState, hostMs = self.backend.hostPingState()
            if pingState == "timeout":
                ping = "timeout"
            elif pingState == "ok" and hostMs is not None:
                ping = "<1 ms" if hostMs < 1 else f"{hostMs:.0f} ms"
            else:
                ping = "-- ms"
        first = f"{label} \u00b7 {state}" if label else state
        return f"{first}\n{ping}"

    def _refreshServerRow(self) -> None:
        if not self.isVisible():
            return
        self._list.setItemText(self.SERVER_ROW, secondary=self._serverText())

    def _requestCacheSize(self) -> None:
        """Start an off-thread size scan; the result updates the row."""
        if not self.isVisible() or self._cacheScans:
            return
        self._cacheToken += 1
        result: dict[str, int] = {}
        scan = _CacheSizeScan(self.backend.coverCacheSize, result)
        self._cacheScans.append((scan, result, self._cacheToken))
        QThreadPool.globalInstance().start(scan)

    def _collectCacheSize(self) -> None:
        """Apply a finished scan, dropping results from before a rebuild."""
        for entry in list(self._cacheScans):
            scan, result, token = entry
            size = result.get("size")
            if size is None:
                continue
            self._cacheScans.remove(entry)
            if token != self._cacheToken:
                continue
            self._cacheSizeLabel = _formatBytes(size)
            if self.isVisible():
                self._list.setItemText(
                    self.CACHE_ROW, secondary=self._cacheSizeLabel
                )

    # -- settings model ---------------------------------------------------- #
    def _backgroundMode(self) -> str:
        mode = (
            self.state.getString("settings.background", "black")
            if self.state is not None
            else "black"
        )
        return "blurred" if mode == "blurred" else "black"

    def _backgroundLabel(self) -> str:
        return "Blurred Cover" if self._backgroundMode() == "blurred" else "Black"

    def _refreshItems(self, keepSelection: bool = False) -> None:
        shuffle = "On" if self.backend.shuffle() else "Off"
        repeat = {
            RepeatMode.Off: "Off",
            RepeatMode.All: "All",
            RepeatMode.One: "One",
        }[self.backend.repeatMode()]
        items = [
            ("Shuffle", shuffle),
            ("Repeat", repeat),
            ("Background", self._backgroundLabel()),
            ("Rebuild Cache", self._cacheSizeLabel),
            ("Reload", "Memory"),
            ("MPD Server", self._serverText()),
        ]
        self._list.setItems(items, keepSelection=keepSelection)

    def _cycle(self, index: int) -> None:
        items = self._list.items()
        if index < 0 or index >= len(items):
            self._refreshItems(keepSelection=True)
            return
        label = items[index][0]
        if label == "Shuffle":
            self.backend.setShuffle(not self.backend.shuffle())
        elif label == "Repeat":
            modes = [RepeatMode.Off, RepeatMode.All, RepeatMode.One]
            current = modes.index(self.backend.repeatMode())
            self.backend.setRepeatMode(modes[(current + 1) % len(modes)])
        elif label == "Background":
            mode = "black" if self._backgroundMode() == "blurred" else "blurred"
            if self.state is not None:
                self.state.set("settings.background", mode)
            window = self.window()
            if window is not None and hasattr(window, "setBackgroundMode"):
                window.setBackgroundMode(mode)
        elif label == "Rebuild Cache":
            window = self.window()
            if window is not None and hasattr(window, "startRecaching"):
                window.startRecaching()
            # The cache is empty now; ignore any scan started before the clear.
            self._cacheToken += 1
            self._cacheSizeLabel = "0 B"
            self._requestCacheSize()
        elif label == "Reload":
            window = self.window()
            if window is not None and hasattr(window, "reloadCaches"):
                window.reloadCaches()
        elif label == "MPD Server":
            window = self.window()
            if window is not None and hasattr(window, "reconnectBackend"):
                window.reconnectBackend()
        self._refreshItems(keepSelection=True)
