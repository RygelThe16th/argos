"""Startup splash — logo, version, licence and honest loading progress.

Argos takes a couple of seconds to reach a usable window: the scientific
imports (numpy, astropy, pyqtgraph, alpyca) and then twenty-odd docks being
built. That used to happen behind a blank screen.

Two rules this module keeps:

**It never shows a percentage it cannot justify.** The stages below are real
checkpoints in :func:`main.main`, each marked as it completes. Anything that
cannot be instrumented would get an indeterminate bar rather than a number
invented to look busy.

**It always closes.** :meth:`Splash.fail` is wired to the exception path in
``main.py`` — a splash left on screen after a crash hides the traceback behind
a frameless always-on-top window with no way to dismiss it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication, QSplashScreen

from argos import __version__

logger = logging.getLogger(__name__)

_ASSETS = Path(__file__).parent / "assets"

#: Splash canvas, in logical pixels. Wide enough for the licence line at the
#: bottom without wrapping.
_WIDTH = 460
_HEIGHT = 280
_LOGO_PX = 96

# The splash is painted before the palette is applied — it cannot read
# ``theme`` without forcing that ordering — so it carries the brand colours
# directly. These are the logo's own gold on the icon's near-black ground.
_BG = "#131417"
_GOLD = "#c49a3c"
_TEXT = "#dedede"
_MUTED = "#8a8f96"
_TRACK = "#25272b"

#: Ordered loading checkpoints: ``(key, label, fraction complete)``.
#: The fractions are rough but monotonic and honest about ordering — the Shell
#: build genuinely dominates once imports are warm.
STAGES: tuple[tuple[str, str, float], ...] = (
    ("config", "Reading configuration…", 0.05),
    ("imports", "Loading scientific libraries…", 0.35),
    ("profile", "Resolving telescope profile…", 0.45),
    ("theme", "Applying theme…", 0.55),
    ("shell", "Building the workspace…", 0.90),
    ("layout", "Restoring layout…", 1.00),
)

_FRACTIONS = {key: fraction for key, _label, fraction in STAGES}
_LABELS = {key: label for key, label, _fraction in STAGES}


def _load_logo(size: int) -> QPixmap:
    """Render the logo at *size*², preferring the vector source.

    Falls back to the PNG, then to an empty pixmap — a missing asset must
    never be the reason Argos won't start.
    """
    svg = _ASSETS / "logo.svg"
    if svg.is_file():
        try:
            renderer = QSvgRenderer(str(svg))
            if renderer.isValid():
                pixmap = QPixmap(size, size)
                pixmap.fill(QColor(Qt.GlobalColor.transparent))
                painter = QPainter(pixmap)
                renderer.render(painter)
                painter.end()
                return pixmap
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Could not render logo.svg (%s), falling back to PNG", exc)

    png = _ASSETS / "logo.png"
    if png.is_file():
        return QPixmap(str(png)).scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    logger.warning("No splash logo found in %s", _ASSETS)
    return QPixmap()


class Splash(QSplashScreen):
    """The startup window: logo, wordmark, version, licence, progress."""

    def __init__(self) -> None:
        self._progress = 0.0
        self._status = "Starting…"
        self._logo = _load_logo(_LOGO_PX)

        canvas = QPixmap(_WIDTH, _HEIGHT)
        canvas.fill(QColor(_BG))
        super().__init__(canvas)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

    # -- painting --------------------------------------------------------

    def drawContents(self, painter: QPainter) -> None:  # noqa: N802 (Qt override)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if not self._logo.isNull():
            painter.drawPixmap((_WIDTH - _LOGO_PX) // 2, 30, self._logo)

        painter.setPen(QColor(_TEXT))
        title = QFont()
        title.setPointSize(26)
        title.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title)
        painter.drawText(0, 140, _WIDTH, 38, Qt.AlignmentFlag.AlignHCenter, "Argos")

        painter.setPen(QColor(_MUTED))
        small = QFont()
        small.setPointSize(10)
        painter.setFont(small)
        painter.drawText(
            0, 176, _WIDTH, 18, Qt.AlignmentFlag.AlignHCenter, f"Version {__version__}"
        )

        self._draw_progress(painter)

        painter.setPen(QColor(_MUTED))
        painter.drawText(
            0,
            _HEIGHT - 26,
            _WIDTH,
            18,
            Qt.AlignmentFlag.AlignHCenter,
            "GNU GPL v3 — free software, with no warranty - Developed by Jules Perret",
        )

    def _draw_progress(self, painter: QPainter) -> None:
        bar_x, bar_w, bar_h = 60, _WIDTH - 120, 4
        bar_y = 212

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(_TRACK))
        painter.drawRoundedRect(bar_x, bar_y, bar_w, bar_h, 2, 2)

        filled = int(bar_w * max(0.0, min(1.0, self._progress)))
        if filled:
            painter.setBrush(QColor(_GOLD))
            painter.drawRoundedRect(bar_x, bar_y, filled, bar_h, 2, 2)

        painter.setPen(QColor(_MUTED))
        status_font = QFont()
        status_font.setPointSize(9)
        painter.setFont(status_font)
        painter.drawText(0, bar_y + 12, _WIDTH, 18, Qt.AlignmentFlag.AlignHCenter, self._status)

    # -- driving ---------------------------------------------------------

    def stage(self, key: str) -> None:
        """Mark the checkpoint *key* reached and repaint.

        An unknown key advances the status text but not the bar: better a
        stalled bar than one that jumps to a number nothing measured.
        """
        self._status = _LABELS.get(key, key)
        if key in _FRACTIONS:
            self._progress = _FRACTIONS[key]
        self.repaint()
        QApplication.processEvents()

    def fail(self, message: str) -> None:
        """Close the splash so a failure is visible rather than hidden behind it."""
        logger.error("Startup failed during %r: %s", self._status, message)
        self.close()
