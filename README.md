# Headunit

Headunit is a car head-unit style remote control for MPD. It also works with
MPD-compatible servers, such as the optional MPD server in
[Tracks](https://github.com/rGnng/tracks), which exposes that macOS player's
library and playback.

The interface targets a fixed 800x480 display and rotary-knob navigation; on a
desktop, keyboard events simulate the knob.

MPD is the only real backend. A built-in mock library with generated album
artwork is available for UI work with `--mock`.

## Screenshots

Captured with `python main.py --mock`:

![Now Playing](docs/screenshots/now-playing.png)

![Albums](docs/screenshots/albums.png)

![Up Next](docs/screenshots/queue.png)

## Features

- MPD backend targeting MPD 0.24: one library snapshot, cached artist, genre
  and album lookups, native shuffle/repeat state, the `Stared` playlist and
  multi-disc albums.
- Album, song, artist, genre and playlist browsers with context menus for
  sorting, queueing and playlist edits.
- Now Playing with large cover art, optional blurred-cover background and a
  progress bar; Up Next shows the queue without the currently playing item.
- Fast lists: layout metrics are cached, selection moves repaint only the
  affected rows, and rows use pre-scaled 72 px thumbnails instead of decoding
  full covers on the paint path.
- Asynchronous cover caching with an idle "Caching" pass, `Rebuild Cache`
  (deletes memory and disk artwork, shows the on-disk cache size) and `Reload`
  (drops in-memory caches only).
- Audio output selection from a context menu in Settings.
- Configurable volume grid: 16 steps (6.25% each) or 64 steps (1.5625% each),
  with off-grid server levels snapped to the nearest step.
- Connection-loss overlay with retry count and ICMP ping; reconnects are
  probed on a background thread so the UI stays responsive.

See [`AGENT.md`](AGENT.md) for the full architecture and behaviour notes.

## Requirements

- Python 3.10+
- MPD 0.24+

```bash
python -m pip install -r requirements.txt
```

## Running

```bash
python main.py           # Use the configured MPD server
python main.py --mock    # In-memory sample library, no server
python main.py --smoke   # Automated key-routing smoke test
python main.py --debug   # Mirror the debug log to stderr
```

Mock mode is only enabled by `--mock`. If the MPD server is unreachable the
MPD backend stays active and the connection overlay is shown; there is no
automatic fallback to the mock library.

## Controls

| Key | Action |
|---|---|
| `0` | Now Playing; when already there, Up Next |
| `1`–`6` | Albums, Songs, Artists, Genres, Playlists, Settings |
| same section key again | return to the top of that section |
| Up / Down | move the selection (main knob) |
| Enter | select; long press (650 ms) opens the long action |
| Backspace / Left | back one level; from a root view, go to Now Playing |
| `q` / `e` | previous / next track; long press seeks ±10 s, repeat ±5 s |
| `w` | play / pause |
| `o` / `p` | cycle repeat / toggle shuffle |
| `*` | star / unstar the current recording |
| `\` | context menu |
| `-` / `=` | volume down / up |
| `x` | mute / unmute |
| Escape | close the application |

## Settings

Settings contains `Background`, `Rebuild Cache`, `Reload`, `Output` and
`MPD Server`.

- `Background`: `Black` or `Blurred Cover`.
- `Rebuild Cache`: clears artwork caches and immediately starts a caching pass.
  The value shows the current on-disk cache size.
- `Reload`: drops in-memory library and artwork caches and rebuilds the visible
  views without restarting.
- `Output`: opens a context menu of MPD audio outputs and enables exactly one.
- `MPD Server`: shows address/state and the latest ping; selecting it
  reconnects.

Shuffle and repeat are not Settings rows; they remain global controls on `p`
and `o`, and their state is read back from MPD.

## Configuration

The config is created automatically at `%APPDATA%/headunit/config.json`
(Windows) or `~/.config/headunit/config.json` (elsewhere):

```json
{
  "debug": true,
  "volume_steps": 16,
  "mpd": {
    "host": "127.0.0.1",
    "port": 6600,
    "password": "",
    "timeout": 1000
  }
}
```

- `volume_steps`: `16` for 6.25% per notch or `64` for 1.5625%; invalid values
  fall back to 16.
- `mpd.timeout`: control socket timeout in milliseconds. Bulk library loads and
  artwork use longer timeouts internally.

MPD has no mute command: the app sends `setvol 0` while remembering the current
level, and restores it when unmuted. The status bar shows a full-scale volume
as `Hi`; the volume overlay still shows `100`.

The debug log is written to `headunit.log` next to the config. UI state lives
in `state.json`, and artwork is cached under `cover-cache/mpd/<server-hash>/`.

## Development media

`media/` contains the icon font and a sample MPD configuration. Generated music,
playlists and MPD runtime state are not committed; create them with
`tools/generate_library.py` (requires Pillow, mutagen and ffmpeg), then start
the development server with:

```bash
mpd media/mpd.conf
```

Adjust the paths in `media/mpd.conf` for your machine. The sample configuration
binds `0.0.0.0:6600`, uses a silent null output and a software mixer so the
volume controls work without audio hardware.
