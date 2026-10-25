"""Bootstrap Icons support.

The icon font ships in `media/bootstrap-icons.ttf`. It is registered with
Qt once and rendered as ordinary text glyphs, so every icon shares the
same font pipeline as the rest of the interface.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase, QPainter, QPen

# Codepoints of the Bootstrap Icons that the interface uses.
# Source: bootstrap-icons 1.11.3 (font/bootstrap-icons.json).
_ICON_CODES = {
    "play": 0xF4F4,        # play-fill
    "pause": 0xF4C3,       # pause-fill
    "stop": 0xF592,        # stop-fill
    "shuffle": 0xF544,     # shuffle
    "plus": 0xF4FE,        # plus
    "repeat": 0xF813,      # repeat
    "repeat-one": 0xF812,  # repeat-1
    "volume-up": 0xF610,   # volume-up-fill
    "volume-mute": 0xF60C,  # volume-mute-fill
    "volume-down": 0xF60A,  # volume-down-fill
    "disc": 0xF2FF,        # disc
    "vinyl": 0xF608,       # vinyl
    "music": 0xF49E,       # music-note-beamed
    "list": 0xF475,        # list-ol
    "music-list": 0xF49F,  # music-note-list
    "settings": 0xF3E5,    # gear
    "search": 0xF52A,      # search
    "home": 0xF423,        # house-door
    "next": 0xF55D,        # skip-forward-fill
    "previous": 0xF551,    # skip-backward-fill
    "back": 0xF117,        # arrow-counterclockwise
    "queue": 0xF2CB,       # collection-play
    "sort": 0xF575,        # sort-down
    "sort-alpha": 0xF571,  # sort-alpha-down
    "sort-year": 0xF577,   # sort-numeric-down
    "clock": 0xF293,       # clock
    "calendar": 0xF1F6,    # calendar
    "check": 0xF633,       # check-lg
    "close": 0xF659,       # x-lg
    "more": 0xF5D3,        # three-dots-vertical
    "star": 0xF588,        # star
    "star-fill": 0xF586,   # star-fill
    "plug": 0xF4F7,        # plug (connection lost)
}

_FONT_FILE = Path(__file__).resolve().parent.parent / "media" / "bootstrap-icons.ttf"
_FAMILY = "bootstrap-icons"
_loaded = False
_fonts: dict[int, QFont] = {}


def loadFont() -> str:
    """Register the icon font with Qt; return the family name."""
    global _loaded
    if _loaded:
        return _FAMILY
    fontId = QFontDatabase.addApplicationFont(str(_FONT_FILE))
    families = QFontDatabase.applicationFontFamilies(fontId)
    _loaded = True
    return families[0] if families else _FAMILY


def iconFont(size: int) -> QFont:
    """Build a font that renders icon glyphs at the given pixel size."""
    font = _fonts.get(size)
    if font is None:
        font = QFont(loadFont())
        font.setPixelSize(size)
        _fonts[size] = font
    return font


def draw(
    painter: QPainter,
    name: str,
    rect,
    color,
    size: int | None = None,
) -> bool:
    """Draw a named icon inside `rect`, centred; return False if unknown."""
    code = _ICON_CODES.get(name)
    if code is None:
        return False
    font = iconFont(size if size is not None else rect.height())
    painter.save()
    painter.setFont(font)
    painter.setPen(QPen(color))
    painter.drawText(
        rect,
        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
        chr(code),
    )
    painter.restore()
    return True
