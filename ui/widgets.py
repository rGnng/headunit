"""Reusable UI widgets: focus list, progress bar, volume overlay."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from .theme import Palette, makeFont
from players.base import formatDuration
from ui import icons


def elide(text: str, font, maxWidth: int) -> str:
    """Elide text with an ellipsis so it fits the given width."""
    return QFontMetrics(font).elidedText(text, Qt.TextElideMode.ElideRight, maxWidth)


def fitText(text: str, metrics: QFontMetrics, maxWidth: int) -> str:
    """Return text unchanged when it fits, else elide it using cached metrics."""
    if maxWidth <= 0:
        return ""
    if metrics.horizontalAdvance(text) <= maxWidth:
        return text
    return metrics.elidedText(text, Qt.TextElideMode.ElideRight, maxWidth)


def scaledCover(
    pixmap: QPixmap, width: int, height: int, fill: bool = True
) -> QPixmap:
    """Downscale cover art with bilinear filtering.

    Very large covers are halved repeatedly first; a single bilinear pass from
    e.g. 3000 px to 32 px loses too much detail and looks rough. With
    ``fill=True`` the result exactly covers the target and is centre-cropped.
    """
    image = pixmap.toImage()
    if image.isNull():
        return pixmap
    while image.width() > width * 2 and image.height() > height * 2:
        image = image.scaled(
            max(1, image.width() // 2),
            max(1, image.height() // 2),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    mode = (
        Qt.AspectRatioMode.KeepAspectRatioByExpanding
        if fill
        else Qt.AspectRatioMode.KeepAspectRatio
    )
    image = image.scaled(
        width, height, mode, Qt.TransformationMode.SmoothTransformation
    )
    if fill and (image.width() > width or image.height() > height):
        image = image.copy(
            (image.width() - width) // 2,
            (image.height() - height) // 2,
            width,
            height,
        )
    return QPixmap.fromImage(image)


class FocusList(QWidget):
    """Scrollable single-column list driven by the main knob.

    Row items are (primary, secondary) text pairs; the secondary
    text is optional and drawn right-aligned in a dimmer colour. When a
    cover provider is supplied, a small album art thumbnail is drawn to the
    left of each row.
    """

    activated = Signal(int)
    selectionChanged = Signal(int)
    _THUMB = 36

    def __init__(self, rowHeight: int = 48, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rowHeight = rowHeight
        self._items: list[tuple[str, str]] = []
        self._selected = 0
        self._offset = 0
        self._placeholder = "No entries available."
        self._coverProvider: Callable[[int], bytes | None] | None = None
        self._coverCache: dict[int, QPixmap | None] = {}
        self._iconProvider: Callable[[int], str | None] | None = None
        self._layouts: dict[int, tuple] = {}
        self._transparentBackground = False
        self._primaryFont = makeFont(20)
        self._secondaryFont = makeFont(16)
        self._placeholderFont = makeFont(18)
        self._primaryMetrics = QFontMetrics(self._primaryFont)
        self._secondaryMetrics = QFontMetrics(self._secondaryFont)
        self._dimText = QColor(
            Palette.Text.red(), Palette.Text.green(), Palette.Text.blue(), 210
        )
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFont(self._primaryFont)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    # -- public API ------------------------------------------------------ #
    def setPlaceholder(self, text: str) -> None:
        self._placeholder = text
        self.update()

    def setItems(
        self,
        items: list[tuple[str, str]],
        keepSelection: bool = False,
        coverProvider: Callable[[int], bytes | None] | None = None,
        iconProvider: Callable[[int], str | None] | None = None,
    ) -> None:
        self._items = list(items)
        if not keepSelection or self._selected >= len(self._items):
            self._selected = 0
        self._offset = 0
        self._coverProvider = coverProvider
        self._coverCache = {}
        self._iconProvider = iconProvider
        self._layouts.clear()
        self.update()
        self.selectionChanged.emit(self._selected)

    def setBackgroundTransparent(self, transparent: bool) -> None:
        """Let the page's backdrop show through instead of filling black."""
        if transparent == self._transparentBackground:
            return
        self._transparentBackground = transparent
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, not transparent)
        self.setStyleSheet("background: transparent;" if transparent else "")
        self.update()

    def setItemText(
        self,
        index: int,
        primary: str | None = None,
        secondary: str | None = None,
    ) -> None:
        """Update one row's text in place, without resetting the selection."""
        if not 0 <= index < len(self._items):
            return
        oldPrimary, oldSecondary = self._items[index]
        self._items[index] = (
            oldPrimary if primary is None else primary,
            oldSecondary if secondary is None else secondary,
        )
        self._layouts.pop(index, None)
        self.update()

    def items(self) -> list[tuple[str, str]]:
        return list(self._items)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layouts.clear()
        self._ensureVisible()

    def invalidateCoverCache(self) -> None:
        """Retry rows that had no thumbnail, keeping decoded covers."""
        self._coverCache = {
            index: pixmap
            for index, pixmap in self._coverCache.items()
            if pixmap is not None
        }
        self.update()

    def _thumbnail(self, index: int) -> QPixmap | None:
        """Return the cached thumbnail for a row, loading it on first use."""
        if self._coverProvider is None:
            return None
        if index not in self._coverCache:
            data = self._coverProvider(index)
            pixmap = QPixmap()
            if data and pixmap.loadFromData(data):
                self._coverCache[index] = scaledCover(
                    pixmap, self._THUMB, self._THUMB
                )
            else:
                self._coverCache[index] = None
        return self._coverCache[index]

    def selected(self) -> int:
        return self._selected

    def setSelectedRow(self, index: int) -> None:
        """Place the selection on a specific row without activating it."""
        if not self._items:
            return
        oldSelected = self._selected
        oldOffset = self._offset
        self._selected = max(0, min(len(self._items) - 1, index))
        self._ensureVisible()
        if self._offset == oldOffset:
            self._updateRows(oldSelected, self._selected)
        else:
            self.update()
        self.selectionChanged.emit(self._selected)

    def moveSelection(self, delta: int) -> None:
        """Move the selection by delta rows, wrapping at both ends."""
        if not self._items:
            return
        oldSelected = self._selected
        oldOffset = self._offset
        count = len(self._items)
        self._selected = (self._selected + delta) % count
        self._ensureVisible()
        if self._offset == oldOffset:
            self._updateRows(oldSelected, self._selected)
        else:
            self.update()
        self.selectionChanged.emit(self._selected)

    def _updateRows(self, *indices: int) -> None:
        """Repaint only the given visible rows (selection-only changes)."""
        visible = self._visibleRows()
        for index in set(indices):
            if self._offset <= index < self._offset + visible:
                y = (index - self._offset) * self._rowHeight
                self.update(QRect(0, y, self.width(), self._rowHeight))

    def activateCurrent(self) -> None:
        if self._items:
            self.activated.emit(self._selected)

    # -- layout ---------------------------------------------------------- #
    def _visibleRows(self) -> int:
        return max(1, self.height() // self._rowHeight)

    def _ensureVisible(self) -> None:
        visible = self._visibleRows()
        if self._selected < self._offset:
            self._offset = self._selected
        elif self._selected >= self._offset + visible:
            self._offset = self._selected - visible + 1

    # -- painting -------------------------------------------------------- #
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        clip = event.rect()
        if not self._transparentBackground:
            painter.fillRect(clip, Palette.Background)

        if not self._items:
            painter.setFont(self._placeholderFont)
            painter.setPen(Palette.TextDim)
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, self._placeholder
            )
            return

        visible = self._visibleRows()
        self._ensureVisible()
        painter.setFont(self._primaryFont)

        for row in range(visible):
            index = self._offset + row
            if index >= len(self._items):
                break
            y = row * self._rowHeight
            rect = QRect(0, y, self.width(), self._rowHeight)
            if not clip.intersects(rect):
                continue
            self._paintRow(
                painter, rect, index == self._selected, index
            )

        # Scroll indicator on the right edge when content overflows.
        if len(self._items) > visible:
            self._paintScrollBar(painter, visible)

    def _paintRow(
        self,
        painter: QPainter,
        rect: QRect,
        selected: bool,
        index: int,
    ) -> None:
        if selected:
            painter.fillRect(rect, Palette.PanelAlt)
            painter.fillRect(QRect(rect.left(), rect.top(), 4, rect.height()), Palette.Accent)

        (
            textX,
            icon,
            primaryText,
            primaryWidth,
            valueX,
            valueWidth,
            lines,
            lineHeight,
        ) = self._rowLayout(index)

        thumbRect = QRect(
            16,
            rect.top() + (rect.height() - self._THUMB) // 2,
            self._THUMB,
            self._THUMB,
        )
        if icon is not None:
            self._paintGlyph(painter, thumbRect, icon, selected)
        elif self._coverProvider is not None:
            thumb = self._thumbnail(index)
            if thumb is not None:
                painter.drawPixmap(thumbRect, thumb)
            else:
                painter.setPen(QPen(Palette.Border, 1))
                painter.setBrush(Palette.PanelAlt)
                painter.drawRoundedRect(thumbRect.adjusted(1, 1, -1, -1), 4, 4)

        painter.setFont(self._primaryFont)
        painter.setPen(Palette.Text if selected else self._dimText)
        painter.drawText(
            QRect(textX, rect.top(), primaryWidth, rect.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            primaryText,
        )

        if lines:
            painter.setFont(self._secondaryFont)
            painter.setPen(Palette.TextDim)
            if len(lines) == 1:
                painter.drawText(
                    QRect(valueX, rect.top(), valueWidth, rect.height()),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                    lines[0],
                )
            else:
                # Multi-line values (e.g. server address plus ping) stack.
                top = rect.top() + (
                    rect.height() - lineHeight * len(lines)
                ) // 2
                for offset, line in enumerate(lines):
                    painter.drawText(
                        QRect(valueX, top + offset * lineHeight, valueWidth, lineHeight),
                        Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                        line,
                    )

    def _rowLayout(self, index: int) -> tuple:
        """Text metrics are static per row; compute them once until a resize."""
        cached = self._layouts.get(index)
        if cached is not None:
            return cached
        primary, secondary = self._items[index]
        icon = self._iconProvider(index) if self._iconProvider is not None else None
        hasArt = icon is not None or self._coverProvider is not None
        # 16 px margin + 36 px thumbnail + 11 px gap (matches the old layout).
        textX = 16 + (self._THUMB + 11 if hasArt else 0)
        textWidth = self.width() - 17 - textX
        primaryText = fitText(primary, self._primaryMetrics, textWidth)
        primaryWidth = textWidth
        valueX = 0
        valueWidth = 0
        lines: list[str] = []
        lineHeight = 0
        if secondary:
            rawLines = secondary.split("\n")
            secondaryWidth = min(
                max(
                    self._secondaryMetrics.horizontalAdvance(line)
                    for line in rawLines
                ),
                int(textWidth * 0.42),
            )
            gap = 24
            primaryWidth = max(0, textWidth - secondaryWidth - gap)
            primaryText = fitText(primary, self._primaryMetrics, primaryWidth)
            valueX = textX + primaryWidth + gap
            valueWidth = max(0, textWidth - primaryWidth - gap)
            lines = [
                fitText(line, self._secondaryMetrics, valueWidth)
                for line in rawLines
            ]
            lineHeight = self._secondaryMetrics.height()
        layout = (
            textX,
            icon,
            primaryText,
            primaryWidth,
            valueX,
            valueWidth,
            lines,
            lineHeight,
        )
        self._layouts[index] = layout
        return layout

    def _paintGlyph(self, painter: QPainter, rect: QRect, name: str, selected: bool) -> None:
        """Draw a small action icon: shuffle, play or plus."""
        color = Palette.Highlight if selected else Palette.TextDim
        if not icons.draw(painter, name, rect, color, size=int(self._THUMB * 0.55)):
            # Fall back to a plain plus when the icon is unknown.
            painter.setPen(QPen(color, 2))
            painter.drawLine(rect.left(), rect.center().y(), rect.right(), rect.center().y())
            painter.drawLine(rect.center().x(), rect.top(), rect.center().x(), rect.bottom())

    def _paintScrollBar(self, painter: QPainter, visible: int) -> None:
        total = len(self._items)
        trackHeight = self.height()
        thumbHeight = max(24, trackHeight * visible // total)
        travel = trackHeight - thumbHeight
        top = travel * self._offset // max(1, total - visible)
        painter.fillRect(
            QRect(self.width() - 3, top, 3, thumbHeight),
            Palette.Border,
        )

    def sizeHint(self) -> QSize:
        return QSize(200, self._rowHeight * 6)


class OptionMenu(QWidget):
    """Overlay menu of options, navigated with the main knob.

    The menu is a child of the page that opened it, so it floats above the
    list. The active option is marked with a check when the menu tracks a
    current value; the knob moves the highlight, the main-knob press picks
    the highlighted entry.
    """

    WIDTH = 380
    ROW = 44
    TITLE_H = 44
    PADDING = 14
    SECTION_H = 32

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = ""
        self._options: list[str] = []
        self._current = 0
        self._selected = 0
        self._showCurrent = True
        self.setFixedSize(self.WIDTH, self.TITLE_H + self.ROW + self.PADDING)
        self.hide()

    def setItems(
        self,
        title: str,
        options: list[str],
        current: int = 0,
        showCurrent: bool = True,
        sectionsBefore: dict[int, str] | None = None,
        selected: int | None = None,
    ) -> None:
        """Show the given options, highlighting `current` as the active one."""
        self._title = title
        self._options = list(options)
        self._current = current if 0 <= current < len(self._options) else 0
        if selected is not None and 0 <= selected < len(self._options):
            self._selected = selected
        else:
            self._selected = self._current
        self._showCurrent = showCurrent
        self._sectionsBefore = sectionsBefore or {}
        height = (
            self.TITLE_H
            + self.ROW * len(self._options)
            + self.SECTION_H * len(self._sectionsBefore)
            + self.PADDING
        )
        self.setFixedSize(self.WIDTH, height)
        self.update()

    def selected(self) -> int:
        return self._selected

    def moveSelection(self, delta: int) -> None:
        if not self._options:
            return
        self._selected = (self._selected + delta) % len(self._options)
        self.update()

    # -- painting -------------------------------------------------------- #
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(Palette.Border, 1))
        painter.setBrush(Palette.Panel)
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 10, 10)

        painter.setFont(makeFont(16, bold=True))
        painter.setPen(Palette.TextDim)
        painter.drawText(
            QRect(self.PADDING, 8, self.width() - 2 * self.PADDING, self.TITLE_H - 16),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self._title,
        )

        optionFont = makeFont(18)
        top = self.TITLE_H
        for row, label in enumerate(self._options):
            section = self._sectionsBefore.get(row)
            if section is not None:
                painter.setFont(makeFont(16, bold=True))
                painter.setPen(Palette.TextDim)
                painter.drawText(
                    QRect(self.PADDING, top, self.width() - 2 * self.PADDING, self.SECTION_H),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    section,
                )
                top += self.SECTION_H
            rect = QRect(self.PADDING, top, self.width() - 2 * self.PADDING, self.ROW)
            if row == self._selected:
                painter.fillRect(rect, Palette.PanelAlt)
                painter.fillRect(QRect(rect.left(), rect.top(), 4, rect.height()), Palette.Accent)

            painter.setFont(optionFont)
            painter.setPen(Palette.Text if row == self._selected else Palette.TextDim)
            painter.drawText(
                rect.adjusted(12, 0, -30, 0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                elide(label, optionFont, rect.width() - 42),
            )

            if row == self._current and self._showCurrent:
                icons.draw(
                    painter,
                    "check",
                    QRect(rect.right() - 34, rect.top(), 24, rect.height()),
                    Palette.Accent,
                    size=18,
                )
            top += self.ROW


class CoverArt(QWidget):
    """Displays album art scaled to fit, with a placeholder when absent."""

    def __init__(self, size: int = 220, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self._pixmap: QPixmap | None = None
        self.setFixedSize(size, size)

    def setImage(self, data: bytes | None) -> None:
        """Set the cover from PNG bytes, or clear it when data is None."""
        if data:
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                self._pixmap = scaledCover(
                    pixmap, self._size, self._size, fill=False
                )
                self.update()
                return
        self._pixmap = None
        self.update()

    def hasImage(self) -> bool:
        return self._pixmap is not None

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setPen(QPen(Palette.Border, 1))

        if self._pixmap is None:
            painter.setBrush(Palette.PanelAlt)
            painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 8, 8)
            painter.setPen(Palette.TextDim)
            painter.setFont(makeFont(16))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, ""
            )
            return

        scaled = self._pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)


class ProgressBar(QWidget):
    """Horizontal progress indicator for playback position."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._value = 0.0
        self.setFixedHeight(10)

    def setValue(self, value: float) -> None:
        self._value = max(0.0, min(1.0, value))
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Palette.PanelAlt)
        painter.drawRoundedRect(self.rect(), 5, 5)
        if self._value > 0:
            width = max(10, int(self.width() * self._value))
            painter.setBrush(Palette.Progress)
            painter.drawRoundedRect(QRect(0, 0, width, self.height()), 5, 5)


class VolumeOverlay(QWidget):
    """Transient volume/mute indicator shown above the footer."""

    WIDTH = 380
    HEIGHT = 64

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self._level = 0
        self._muted = False
        self._hideTimer = QTimer(self)
        self._hideTimer.setSingleShot(True)
        self._hideTimer.setInterval(1800)
        self._hideTimer.timeout.connect(self.hide)
        self.hide()

    def present(self, level: int, muted: bool) -> None:
        self._level = max(0, min(100, level))
        self._muted = muted
        self._hideTimer.start()
        self.show()
        self.raise_()
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Palette.PanelAlt)
        painter.setPen(QPen(Palette.Border, 1))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 10, 10)

        labelFont = makeFont(16)
        valueFont = makeFont(18, bold=True)
        margin = 16

        painter.setFont(labelFont)
        painter.setPen(Palette.TextDim)
        painter.drawText(
            QRect(margin, 10, 100, 20),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            "Volume",
        )

        painter.setFont(valueFont)
        painter.setPen(Palette.Danger if self._muted else Palette.Text)
        value = f"{0 if self._muted else self._level:02d}"
        painter.drawText(
            QRect(self.WIDTH - margin - 80, 10, 80, 20),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
            value,
        )

        barRect = QRect(margin, 40, self.WIDTH - 2 * margin, 10)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Palette.Background)
        painter.drawRoundedRect(barRect, 5, 5)
        fillWidth = int(barRect.width() * (0 if self._muted else self._level) / 100)
        if fillWidth > 0:
            painter.setBrush(Palette.Danger if self._muted else Palette.Accent)
            painter.drawRoundedRect(QRect(barRect.left(), barRect.top(), fillWidth, barRect.height()), 5, 5)


class ConnectionOverlay(QWidget):
    """Centered box shown while the MPD connection is down."""

    WIDTH = 300
    HEIGHT = 176

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self._attempt = 1
        self._ping = "Ping -- ms"
        self.hide()

    def present(self, attempt: int, ping: str) -> None:
        self._attempt = max(1, int(attempt))
        self._ping = ping
        self.show()
        self.raise_()
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Palette.PanelAlt)
        painter.setPen(QPen(Palette.Border, 1))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 12, 12)

        iconSize = 48
        icons.draw(
            painter,
            "plug",
            QRect((self.width() - iconSize) // 2, 24, iconSize, iconSize),
            Palette.TextDim,
            size=iconSize,
        )

        painter.setFont(makeFont(18, bold=True))
        painter.setPen(Palette.Text)
        painter.drawText(
            QRect(0, 84, self.width(), 26),
            Qt.AlignmentFlag.AlignCenter,
            f"Retry attempt #{self._attempt}",
        )

        painter.setFont(makeFont(15))
        painter.setPen(Palette.TextDim)
        painter.drawText(
            QRect(0, 116, self.width(), 22),
            Qt.AlignmentFlag.AlignCenter,
            self._ping,
        )


class CachingOverlay(QWidget):
    """Top-right progress gadget shown while idle cover caching runs."""

    WIDTH = 250
    HEIGHT = 84

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self._current = 0
        self._total = 0
        self.hide()

    def present(self, current: int, total: int) -> None:
        self._current = max(0, int(current))
        self._total = max(0, int(total))
        self.show()
        self.raise_()
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Palette.PanelAlt)
        painter.setPen(QPen(Palette.Border, 1))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 10, 10)

        iconSize = 26
        icons.draw(
            painter,
            "disc",
            QRect(14, 15, iconSize, iconSize),
            Palette.TextDim,
            size=iconSize,
        )

        painter.setFont(makeFont(16, bold=True))
        painter.setPen(Palette.Text)
        painter.drawText(
            QRect(50, 12, 110, 28),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            "Caching",
        )

        painter.setFont(makeFont(14, monospace=True))
        painter.setPen(Palette.TextDim)
        painter.drawText(
            QRect(self.WIDTH - 104, 14, 90, 24),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
            f"{self._current}/{self._total}",
        )

        bar = QRect(14, 58, self.WIDTH - 28, 8)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Palette.Background)
        painter.drawRoundedRect(bar, 4, 4)
        if self._total > 0:
            width = int(bar.width() * self._current / self._total)
            if width > 0:
                painter.setBrush(Palette.Accent)
                painter.drawRoundedRect(
                    QRect(bar.left(), bar.top(), width, bar.height()), 4, 4
                )


class StatusBar(QWidget):
    """Bottom status bar: transport state, track info, queue and options.

    Layout, left to right:
      [state icon]  [cover] track name - album  0:00/0:00  [cd icon] n/m  [shuffle] [repeat]  [vol icon] nn %

    The cover and "track name - album" form one group which is hidden when
    the active page already shows the current recording (Now Playing).
    """

    HEIGHT = 44
    _ICON = 22
    _COVER = 32

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self._playing = False
        self._stopped = False
        self._hasQueue = False
        self._text = "Playlist is empty"
        self._position = 0
        self._duration = 0
        self._queuePosition = 0
        self._queueLength = 0
        self._shuffle = False
        self._repeatLabel = ""
        self._volume = 0
        self._muted = False
        self._cover: QPixmap | None = None
        self._showTrackGroup = True
        self._starred = False
        self._starredSupported = False

    def updateState(
        self,
        playing: bool,
        hasQueue: bool,
        text: str,
        position: int,
        duration: int,
        queuePosition: int,
        queueLength: int,
        cover: bytes | None = None,
        stopped: bool = False,
    ) -> None:
        """Set the transport and track portion of the bar."""
        self._playing = playing
        self._hasQueue = hasQueue
        self._text = text
        self._position = position
        self._duration = duration
        self._queuePosition = queuePosition
        self._queueLength = queueLength
        self._stopped = stopped
        self._setCover(cover)
        self.update()

    def updatePosition(self, position: float) -> None:
        """Advance the elapsed time between full status refreshes."""
        if position != self._position:
            self._position = position
            self.update()

    def _setCover(self, data: bytes | None) -> None:
        if data:
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                self._cover = scaledCover(pixmap, self._COVER, self._COVER)
                return
        self._cover = None

    def setTrackGroupVisible(self, visible: bool) -> None:
        """Show or hide the cover plus track text group."""
        if visible != self._showTrackGroup:
            self._showTrackGroup = visible
            self.update()

    def updateOptions(self, shuffle: bool, repeatLabel: str) -> None:
        """Set the shuffle and repeat indicators."""
        self._shuffle = shuffle
        self._repeatLabel = repeatLabel
        self.update()

    def updateVolume(self, level: int, muted: bool) -> None:
        """Set the volume indicator."""
        self._volume = max(0, min(100, level))
        self._muted = muted
        self.update()

    def updateStarred(self, starred: bool, supported: bool) -> None:
        """Set the current recording's star."""
        self._starred = starred
        self._starredSupported = supported
        self.update()

    # -- painting --------------------------------------------------------- #
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), Palette.Panel)
        painter.setPen(QPen(Palette.Border, 1))
        painter.drawLine(0, 0, self.width(), 0)

        y = self.height() // 2
        x = 14

        # Transport state icon.
        self._paintStateIcon(painter, QRect(x, y - self._ICON // 2, self._ICON, self._ICON))
        x += self._ICON + 10

        # Position / length comes right after the state icon and is always visible.
        timeText = self._timeText()
        timeFont = makeFont(14, monospace=True)
        timeWidth = QFontMetrics(timeFont).horizontalAdvance(timeText)
        painter.setFont(timeFont)
        painter.setPen(Palette.TextDim)
        painter.drawText(
            QRect(x, 0, timeWidth, self.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            timeText,
        )
        x += timeWidth + 14

        # Right-hand cluster is laid out first so the text has a bound.
        repeatWidth = self._measureRepeat(painter)
        shuffleWidth = 22
        volumeWidth = self._measureVolume()
        queueWidth = self._measureQueue()
        starredWidth = 22 if self._starredSupported else 0
        gap = 12

        rightWidth = (
            queueWidth + gap + shuffleWidth + gap + repeatWidth + gap + volumeWidth
        )
        if starredWidth:
            rightWidth += starredWidth + gap
        regionWidth = self.width() - x - rightWidth - 24

        if self._showTrackGroup:
            # Cover thumbnail, then track text on a shared row.
            coverRect = QRect(x, y - self._COVER // 2, self._COVER, self._COVER)
            self._paintCover(painter, coverRect)
            textX = x + self._COVER + 10
            textWidth = max(0, regionWidth - (self._COVER + 10))

            painter.setFont(makeFont(15, bold=self._hasQueue))
            painter.setPen(Palette.Text if self._hasQueue else Palette.TextDim)
            painter.drawText(
                QRect(textX, 0, textWidth, self.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                elide(self._text, makeFont(15), textWidth),
            )

        rx = self.width() - 14

        # Volume icon and value.
        rx = self._paintVolume(painter, rx, y)
        rx -= gap

        # Repeat indicator.
        rx = self._paintRepeat(painter, rx, y)
        rx -= gap

        # Shuffle indicator.
        rx = self._paintShuffle(painter, rx, y)
        rx -= gap

        # Queue position with CD icon.
        rx = self._paintQueue(painter, rx, y)
        rx -= gap

        # Stared star for the current recording.
        if self._starredSupported:
            self._paintStarred(painter, rx, y)

    def _timeText(self) -> str:
        if not self._hasQueue or self._duration <= 0:
            return "0:00/0:00"
        return f"{formatDuration(self._position)}/{formatDuration(self._duration)}"

    def _paintCover(self, painter: QPainter, rect: QRect) -> None:
        if self._cover is None or self._cover.isNull():
            painter.setPen(QPen(Palette.Border, 1))
            painter.setBrush(Palette.Background)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 4, 4)
            return
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Palette.Border)
        painter.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), 5, 5)
        painter.drawPixmap(rect, self._cover)

    def _paintStateIcon(self, painter: QPainter, rect: QRect) -> None:
        if not self._hasQueue:
            icons.draw(painter, "stop", rect, Palette.TextDim, size=self._ICON)
            return
        if self._playing:
            icons.draw(painter, "play", rect, Palette.TextDim, size=self._ICON)
            return
        if self._stopped:
            icons.draw(painter, "stop", rect, Palette.TextDim, size=self._ICON)
            return
        icons.draw(painter, "pause", rect, Palette.TextDim, size=self._ICON)

    def _queueText(self) -> str:
        return f"{self._queuePosition:02d}/{self._queueLength:02d}" if self._hasQueue else "--/--"

    def _measureQueue(self) -> int:
        """Width of the queue icon plus its position text."""
        metrics = QFontMetrics(makeFont(15, monospace=True))
        return self._ICON + 6 + metrics.horizontalAdvance(self._queueText())

    def _paintQueue(self, painter: QPainter, right: int, y: int) -> int:
        """Draw the queue icon and the position text; return the left edge."""
        iconSize = self._ICON
        text = self._queueText()
        metrics = QFontMetrics(makeFont(15, monospace=True))
        textWidth = metrics.horizontalAdvance(text)
        totalWidth = iconSize + 6 + textWidth
        left = right - totalWidth

        discRect = QRect(left, y - iconSize // 2, iconSize, iconSize)
        icons.draw(painter, "music-list", discRect, Palette.TextDim, size=iconSize)

        painter.setFont(makeFont(15, monospace=True))
        painter.setPen(Palette.TextDim)
        painter.drawText(
            QRect(left + iconSize + 6, 0, textWidth, self.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            text,
        )
        return left

    def _paintStarred(self, painter: QPainter, right: int, y: int) -> int:
        """Draw the current recording's star; return the left edge."""
        size = 22
        left = right - size
        icons.draw(
            painter,
            "star-fill" if self._starred else "star",
            QRect(left, y - size // 2, size, size),
            Palette.TextDim,
            size=size,
        )
        return left

    def _paintShuffle(self, painter: QPainter, right: int, y: int) -> int:
        """Draw the shuffle icon; return the left edge."""
        size = 22
        left = right - size
        color = Palette.Accent if self._shuffle else Palette.TextDim
        icons.draw(painter, "shuffle", QRect(left, y - size // 2, size, size), color, size=size)
        return left

    def _measureRepeat(self, painter: QPainter) -> int:
        """Width needed for the repeat icon plus its mode label."""
        return 22

    def _paintRepeat(self, painter: QPainter, right: int, y: int) -> int:
        """Draw the repeat icon; return the left edge."""
        size = 22
        label = self._repeatLabel
        left = right - size

        active = self._repeatLabel != "Off"
        color = Palette.Accent if active else Palette.TextDim
        iconName = "repeat-one" if label == "One" else "repeat"
        icons.draw(painter, iconName, QRect(left, y - size // 2, size, size), color, size=size)
        return left

    def _volumeText(self) -> str:
        """Two-character status-bar volume; 100 is shown as ``Hi``."""
        if self._muted:
            return "00"
        return "Hi" if self._volume >= 100 else f"{self._volume:02d}"

    def _measureVolume(self) -> int:
        """Width of the volume icon plus its value."""
        metrics = QFontMetrics(makeFont(15, monospace=True))
        return 22 + 6 + metrics.horizontalAdvance(self._volumeText())

    def _paintVolume(self, painter: QPainter, right: int, y: int) -> int:
        """Draw the volume icon and value; return the left edge."""
        iconSize = 22
        text = self._volumeText()
        metrics = QFontMetrics(makeFont(15, monospace=True))
        textWidth = metrics.horizontalAdvance(text)
        total = iconSize + 6 + textWidth
        left = right - total

        iconRect = QRect(left, y - iconSize // 2, iconSize, iconSize)
        color = Palette.Danger if self._muted else Palette.TextDim
        if self._muted:
            iconName = "volume-mute"
        elif self._volume <= 33:
            iconName = "volume-down"
        else:
            iconName = "volume-up"
        icons.draw(painter, iconName, iconRect, color, size=iconSize)

        painter.setFont(makeFont(15, monospace=True))
        painter.setPen(color)
        painter.drawText(
            QRect(left + iconSize + 6, 0, textWidth, self.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            text,
        )
        return left
