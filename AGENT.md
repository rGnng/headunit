# Headunit Project Summary

## Overview
Headunit is a PySide6 car head-unit style music player UI for MPD. It uses a
fixed 800x480 layout with dial/rotary-style navigation (keyboard events are the
knob simulation). Only the MPD backend is supported; the protocol baseline is
MPD 0.24, with `Last-Modified` fallbacks for older servers where relevant. A
mock backend is available for UI development without a server.

## Architecture

### Entry Point
- `main.py`: app bootstrap, `AppConfig`, MPD backend creation, `MainWindow`,
  debug-log setup, `--debug` console mirroring, GUI-thread heartbeat and stall
  watchdog. Supports `--mock` and `--smoke`.
- `debuglog.py`: rotating debug log and GUI-thread stall watchdog.

### Backends (`players/`)
- `base.py`: abstract `PlayerBackend` (transport, library, playlists, queue,
  server info, cover art, reload hooks), plus `Track`, `Album` and
  `formatDuration`.
- `mpd.py`: MPD backend. Keeps a library snapshot and derived album/artist/
  genre caches; polls `status` every second; parses MPD tags into `Track`;
  manages playlists (including the `Stared` playlist), queue edits, artwork and
  connection state.
- `mpdclient.py`: minimal synchronous MPD protocol client. Handles reconnects
  with backoff, command lists, binary artwork responses and connection-attempt
  counting.
- `mock.py`: in-memory sample backend for UI testing.

### UI (`ui/`)
- `window.py`: main window, view routing, key mapping, status bar, background,
  overlays, idle caching.
- `pages.py`: Now Playing, Albums, Songs, Artists, Genres, Playlists, Settings,
  Up Next/Queue pages.
- `widgets.py`: `FocusList`, `OptionMenu`, `CoverArt`, `ProgressBar`,
  `StatusBar`, `VolumeOverlay`, `ConnectionOverlay`, `CachingOverlay`, cover
  scaling helper.
- `config.py`: persistent MPD connection settings.
- `state.py`: persistent UI state (cursor, sort order, section, background).
- `theme.py`: colours and fonts.
- `icons.py`: Bootstrap Icons font loading and codepoints.

## Data Model
`Track` fields of note:
- `artist`: the track artist (`Artist` tag), shown in lists.
- `album_artist`: `AlbumArtist` (falls back to `artist`); used to group albums.
- `disc_number`: parsed from `Disc` (`1` or `1/2`); `0` when absent.
- `track_number`: track number within the disc.
- `added`: unix timestamp from MPD `added` (0.24+) or `Last-Modified`.

`Album` is derived per `(album_artist, album title)`; its `added` is the newest
track timestamp.

## MPD Integration

### Library snapshot
One `listallinfo` call builds `_library`; albums, artists and genres are derived
and cached. Browsing never re-fetches the library per row. The snapshot uses a
separate **20 s** timeout (even though the control socket timeout is 1 s).
The Settings `Reload` action or an explicit backend reconnect drops the
snapshot; it is rebuilt on demand.

### Playback and queue
- Play and queue changes are sent as **single command lists** (one round trip,
  one `status` tick); no per-track round trips.
- `playTrack`: `clear`, `add`, `play`.
- `playAlbum`: `clear`, `add` every album track, `random 0`, `play <index>`.
- `playPlaylist`: `clear`, `load`, `random 0`, `play <index>`.
- `addAlbumToQueue`: appends `add` commands; starts playback if stopped.
- `Shuffle All`: `clear`, `findadd "(file != '')"`, `random 1`, `play`.
  The filter expression must stay quoted or MPD matches zero songs.
- `addPlaylistToQueue` appends the playlist in one command list.
- Queue rows exclude the currently playing item; the queue view refreshes on
  track changes and playback-option changes.

### Shuffle / repeat
MPD is the source of truth. Every `status` poll reads `random`, `repeat` and
`single` into the local state and emits `playbackOptionsChanged` only on change.
Off = `repeat 0`; All = `repeat 1, single 0`; One = `repeat 1, single 1`.
Commands never update local flags optimistically, so a failed command and a
server restart stay consistent.

### Cover art
- Fetched on a separate connection: `readpicture` first, then `albumart` (folder
  art) when the file has no embedded picture.
- `binarylimit 262144` (256 KiB instead of MPD's 8 KiB) and a **10 s** timeout;
  three worker threads and a per-server disk cache.
- `coverStatus()` reports `ready` / `missing` / `pending` / `unknown`;
  `missing` is only set when the server explicitly says there is no artwork.
- Now Playing retries a missing cover every 1500 ms up to five times, unless the
  server reported `missing`.
- List rows use pre-scaled 72 px thumbnails, stored under
  `cover-cache/.../thumbs/`; the full image is only decoded for Now Playing and
  the blurred background. Thumbnails are produced in the cover worker, so
  painting a new list row never decodes a full-size cover (which costs ~18 ms
  per row on a large JPEG).
- Idle caching (see below) prefetches album covers; covers already in memory or
  on disk are skipped without fetching.

### Connection handling
- Control socket timeout comes from `mpd.timeout` (default 1000 ms). A failed
  command closes the socket; reconnects back off 1 s → 2 s → 4 s → 8 s → 10 s
  and continue until success.
- While offline, reconnects are probed on a background thread: a dead host can
  make `connect()` block for the full socket timeout, which would freeze the
  GUI if attempted on the status-poll thread.
- While offline, a background thread pings the host with the system `ping`
  command every second. The overlay shows the attempt count and the ICMP ping;
  no reply shows `timeout`.
- `connectionRestored` makes the window hide the overlay and refresh the status
  bar, Now Playing/artwork, the visible queue and the background. The backend
  also emits `libraryChanged`, so a browser opened while offline fills in.
- MPD keeps a current song while stopped (e.g. after a server restart), so the
  client still shows its title, artist, cover and duration with a stop icon.

### Starred recordings
The MPD playlist named `Stared` (`STARRED_PLAYLIST`) backs the star feature.
It is listed first in Playlists and cannot be deleted/renamed. The `*` key (or
keypad `*`, Shift+8) toggles the current recording; context menus say `Star` /
`Unstar`. The status bar shows the star; Now Playing does not.

## UI Behaviour

### Sections and keys
| Key | Action |
|---|---|
| `0` | Now Playing; when already there, Up Next/Queue |
| `1`..`6` | Albums, Songs, Artists, Genres, Playlists, Settings |
| same section key again | return to the top of that section's root list |
| Up / Down | move selection (main knob) |
| Enter | select; long press (650 ms) selects long action |
| Backspace / Left | back; from a root view, go to Now Playing |
| `q` / `e` | previous / next; long press seeks ±10 s, repeat ±5 s |
| `w` | play/pause |
| `o` / `p` | cycle repeat / toggle shuffle |
| `*` | star/unstar current recording |
| `\` | context/option menu |
| `-` / `=` | volume down/up |
| `x` | mute |
| Escape | close the app |

### Lists
Track rows show the track artist and duration in the dim right column. Album
tracks are sorted by `(disc, track)` and multi-disc albums show `disc-track`
prefixes (e.g. `1-01`). Playlists and the queue keep MPD's order.

For smooth browsing on low-end hardware, row text metrics are cached per width
and a selection-only move repaints just the affected rows; the whole list is
redrawn only when the viewport scrolls. Cover thumbnails are decoded from the
small thumbnail cache, never from full artwork.

### Background
Settings offers `Background`: `Black` or `Blurred Cover`. Blurred cover scales
the album art to 4x4, stretches it back (unrecognisable) and darkens it heavily.
With nothing playing it shows a vertical gradient from `rgb(85,85,85)` to black.
The state is stored as `settings.background`.

### Now Playing
Shows cover, title, artist - album and progress. Cover retries as described
above; no star indicator is shown here (the status bar has it).

### Settings
Rows: `Shuffle`, `Repeat`, `Background`, `Rebuild Cache`, `Reload`,
`MPD Server`. The server row has two lines, `address · state` and the current
ping, refreshed every 300 ms while visible; selecting it reconnects.
`Rebuild Cache` deletes artwork caches (memory and disk) and starts a caching
pass immediately; its value shows the current on-disk cache size, refreshed
every 2 s while visible. `Reload` drops the in-memory library/artwork caches and
rebuilds the visible views without restarting.

### Idle cover caching
After 10 s without input the client prefetches missing album covers. A top-right
gadget with the `disc` icon shows `Caching`, `processed/total` and a progress
bar. Any key pauses the pass immediately; it resumes after another 10 idle
seconds from the same position. Connection loss pauses it; a library change
starts a fresh pass. A cover that keeps timing out is skipped after 10 s so the
rest of the pass can proceed.

### Overlays

All status gadgets share one layout: a dim icon and bold title on the left, a
dim value on the right and, where applicable, a rounded progress bar below.
- Volume: bottom centre above the status bar. Shows `Volume` / `nn%`, the
  volume-up icon, and a bar; muted replaces icon/value with the red mute icon
  and `Muted`.
- Connection lost: compact centred gadget. Shows the red plug icon,
  `Disconnected` and `Retry n · Ping …`; no progress bar.
- Caching: top-right disc gadget. Shows `Caching`, `processed/total` and a
  progress bar.

## Configuration and State
Config file (`%APPDATA%/headunit/config.json` on Windows, otherwise
`~/.config/headunit/config.json`):
```json
{
  "debug": true,
  "mpd": {
    "host": "127.0.0.1",
    "port": 6600,
    "password": "",
    "timeout": 1000
  }
}
```
`mpd.timeout` is the **control** socket timeout in milliseconds. Bulk library
loads and artwork use their own longer timeouts. Missing keys are filled from
the defaults; obsolete backend keys (`backend`, `itunes`, `paired_itunes`) are
dropped on load.

UI state (`%APPDATA%/headunit/state.json`) stores the active section, sort modes
(`albums.sort`, `songs.sort`), browser cursor keys, queue cursor and
`settings.background`. State writes are debounced.

Artwork disk cache lives in `%APPDATA%/headunit/cover-cache/mpd/<server-hash>/`
and is separate from the JSON configuration.

## Logging
`debuglog.py` writes a rotating log to `%APPDATA%/headunit/headunit.log`
(2 MB × 3 backups). Every MPD command is timed; commands and status polls over
250 ms log a warning. `--debug` mirrors records to stderr. The stall watchdog
dumps the GUI thread stack if the event loop stops responding for more than two
seconds. `sys.excepthook` and `threading.excepthook` log uncaught exceptions.

## Running
```bash
python main.py           # Normal run (configured MPD server)
python main.py --mock    # In-memory sample library
python main.py --smoke   # Automated key-routing smoke test
python main.py --debug   # Mirror the debug log to stderr
```

Mock mode is only enabled by `--mock`; an unreachable MPD server does not fall
back to the mock backend.

## Dependencies
See `requirements.txt`: PySide6 at runtime; fonttools/brotli only for icon
tooling (the generated icon font is committed).

## Current Backend
MPD only, targeting protocol features from MPD 0.24. Connection details come
from `AppConfig`.
