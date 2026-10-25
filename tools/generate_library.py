"""Generate a sample music library for local MPD testing.

Creates 10 albums, each with 15 silent tracks of random duration, tagged
metadata and a distinct generated cover image. Silent audio keeps the test
library small and unobtrusive.
"""

from __future__ import annotations

import random
import subprocess
import sys
from io import BytesIO
from pathlib import Path

from mutagen.flac import FLAC, Picture
from PIL import Image, ImageDraw, ImageFont

MUSIC_DIR = Path(__file__).resolve().parent.parent / "media" / "music"

# (artist, album, year, genre, palette base hue)
ALBUMS = [
    ("Aurora Ensemble", "Northern Lights", 2019, "Classical", 205),
    ("The Meridian Quartet", "Blue Meridian", 2017, "Jazz", 30),
    ("Elena Voss", "Piano Works", 2015, "Piano", 280),
    ("Northbound Orchestra", "Cartography", 2020, "Orchestral", 120),
    ("Julian Marlowe", "Chamber Days", 2018, "Chamber", 350),
    ("Sable Coast", "Tidal Lines", 2016, "Ambient", 185),
    ("Iris Lang", "Field Notes", 2021, "Folk", 95),
    ("The Vantage Trio", "Resonance", 2022, "Jazz", 15),
    ("Cassia Bloom", "Glass Hours", 2023, "Electronic", 300),
    ("Rowan Field", "Paper Lanterns", 2014, "Folk", 55),
]

TITLE_WORDS = [
    "Prelude", "Nocturne", "Elegy", "Reverie", "Motion", "Drift", "Embers",
    "Harbour", "Meridian", "Threshold", "Lantern", "Currents", "Vigil",
    "Prelude in Amber", "Second Light", "Standing Orders", "Tidal Line",
    "Quiet Hours", "The Long Thaw", "Overland", "Contours", "Atlas",
]


def make_cover(title: str, hue: int) -> bytes:
    """Render a distinct 600x600 cover image and return PNG bytes."""
    size = 600
    top = hsv_to_rgb(hue, 0.55, 0.30)
    bottom = hsv_to_rgb((hue + 40) % 360, 0.65, 0.12)

    image = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(image)
    for y in range(size):
        t = y / size
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (size, y)], fill=color)

    # Decorative concentric ring.
    cx = size // 2
    for radius in range(150, 40, -12):
        shade = tuple(max(0, c - 6) for c in top)
        draw.ellipse(
            [cx - radius, cx - radius, cx + radius, cx + radius],
            outline=shade,
            width=2,
        )

    try:
        titleFont = ImageFont.truetype("segoeuib.ttf", 46)
    except OSError:
        titleFont = ImageFont.load_default()

    draw.text((40, size - 140), title, fill=(240, 240, 240), font=titleFont)

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    """Convert HSV (h in degrees, s/v in 0..1) to an RGB tuple."""
    import colorsys

    r, g, b = colorsys.hsv_to_rgb((h % 360) / 360.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def make_silent_flac(path: Path, seconds: int) -> None:
    """Write a silent FLAC file of the given duration using ffmpeg."""
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", str(seconds),
            "-c:a", "flac",
            str(path),
        ],
        check=True,
    )


def tag_flac(
    path: Path,
    title: str,
    artist: str,
    album: str,
    year: int,
    genre: str,
    track: int,
    cover: bytes,
) -> None:
    """Write Vorbis comments and embed the cover art."""
    audio = FLAC(str(path))
    audio["title"] = title
    audio["artist"] = artist
    audio["album"] = album
    audio["date"] = str(year)
    audio["genre"] = genre
    audio["tracknumber"] = str(track)
    audio["albumartist"] = artist

    picture = Picture()
    picture.type = 3  # front cover
    picture.mime = "image/png"
    picture.desc = "Cover"
    picture.data = cover
    audio.add_picture(picture)
    audio.save()


def main() -> int:
    random.seed(20241021)
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)

    for albumIndex, (artist, album, year, genre, hue) in enumerate(ALBUMS):
        albumDir = MUSIC_DIR / f"{albumIndex + 1:02d} - {artist} - {album}"
        albumDir.mkdir(exist_ok=True)
        cover = make_cover(album, hue)
        (albumDir / "cover.png").write_bytes(cover)

        for track in range(1, 16):
            title = random.choice(TITLE_WORDS)
            seconds = random.randint(120, 360)
            fileName = f"{track:02d} - {title}.flac"
            filePath = albumDir / fileName
            print(f"[{albumIndex + 1:02d}/10] {artist} - {album} - {fileName} ({seconds}s)")
            make_silent_flac(filePath, seconds)
            tag_flac(filePath, title, artist, album, year, genre, track, cover)

    print(f"\nLibrary written to {MUSIC_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
