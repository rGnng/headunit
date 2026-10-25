"""Colour palette and font helpers for the head-unit interface."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont


class Palette:
    """Fixed dark palette suitable for an in-dash display."""

    Background = QColor("#000000")
    Panel = QColor("#000000")
    PanelAlt = QColor("#151515")
    Border = QColor("#2a2a2a")
    Text = QColor("#e6ebf0")
    TextDim = QColor("#8b98a5")
    Accent = QColor("#e8a33d")
    AccentText = QColor("#101418")
    Highlight = QColor("#4f7a9c")
    Danger = QColor("#d66a5e")


def makeFont(px: int, bold: bool = False, monospace: bool = False) -> QFont:
    """Build a font with the given pixel size."""
    family = "Cascadia Mono" if monospace else "Segoe UI"
    font = QFont(family)
    if monospace and font.family() != family:
        font = QFont("Consolas")
    font.setPixelSize(px)
    font.setBold(bold)
    return font
